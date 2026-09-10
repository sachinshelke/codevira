"""The write path and the read path must not drift apart again.

WHY THIS FILE EXISTS
--------------------
4.1.0 was cut FOR the negation bug and shipped without fixing it, through a
full green suite and every release gate.

``reconcile.classify()`` had the negation guard. But ``record_decision`` does
not go through ``classify``; it goes through ``tools/check_conflict``, which
re-implements the same rule INLINE. The guard was added to the read path, the
write path kept the old rule, and "never use pnpm" was filed as a *duplicate*
of "use pnpm" — Jaccard 0.83 — landing it in the ``duplicates`` list that
supersede-on-write consumes. The contradiction did not just go unreported; it
silently retired the decision it contradicted.

Every existing test here is example-based: it asserts a specific pair gets a
specific verdict. That is what let the divergence through — both paths passed
their own examples. What nobody asserted was that the two paths AGREE.

WHAT THIS ASSERTS
-----------------
A differential sweep. Every ordered pair in a corpus is run through the real
write path (record into a store, call ``check_conflict``) and through
``classify()``, and the verdicts must match — except for divergences pinned
below WITH a reason. A new divergence fails.

The two paths share ``_tokenize``, ``_jaccard``, ``_overlap_coefficient``,
``negation_disagrees`` and the thresholds. They do NOT share the decision
rule. Until they do, this file is what stands in for that.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, paths, reconcile

# Chosen to land in every regime the rule distinguishes: symmetric duplicate,
# negated near-duplicate, terse-vs-long asymmetric overlap, and unrelated.
CORPUS = [
    "use pnpm as the package manager for all workspaces",
    "never use pnpm as the package manager for all workspaces",
    "use pnpm as the package manager for every workspace",
    # A TRUE duplicate of the first entry (Jaccard ~0.86, no negation). Without
    # it the sweep never produces a `duplicate` verdict at all, and the whole
    # duplicate branch — including the pinned divergence below — goes
    # unexercised while the test still reports green.
    "use pnpm as the package manager for all workspaces in the monorepo",
    "adopt bcrypt hashing for passwords across the auth service",
    "do not adopt bcrypt hashing for passwords across the auth service",
    "bcrypt hashing",
    "cache the invalidation path aggressively in the edge worker",
    "cache the invalidation path",
    "deploy the worker fleet on fargate behind an application load balancer",
    "postgres is the primary datastore for all tenant records",
]

# The ONE place the paths legitimately differ, pinned so it cannot grow.
#
# A non-negated near-duplicate (Jaccard >= threshold) of a PROTECTED decision:
#   classify()      -> "duplicate"  (b_protected gates only the ASYMMETRIC branch)
#   check_conflict  -> "conflict"   (`if is_protected or is_negation_conflict`)
#
# Both are right for their own job, and the write path's answer is the
# load-bearing one: `duplicates` is what supersede-on-write draws from, so
# filing a near-duplicate of a LOCKED decision there would let the write path
# retire a do_not_revert decision. It must be a conflict at write time.
# classify() is a reconcile PLANNER, where a duplicate is a merge candidate.
PINNED_DIVERGENCES = {
    ("duplicate", "conflict", True): (
        "non-negated near-duplicate of a PROTECTED decision: the write path "
        "must not file it under `duplicates`, which supersede-on-write "
        "consumes — that would retire a locked decision"
    ),
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    decisions_store.invalidate_merged_cache()
    return root


def _seed(protected: bool) -> dict[str, str]:
    """Record the corpus; return {id: text}.

    supersede-on-write is OFF: it would retire corpus members as near-
    duplicates land, and a sweep over a store that ate half its own corpus
    proves much less than it appears to.
    """
    os.environ["CODEVIRA_SUPERSEDE_ON_RECORD"] = "0"
    try:
        return {
            decisions_store.record(decision=t, do_not_revert=protected): t
            for t in CORPUS
        }
    finally:
        os.environ.pop("CODEVIRA_SUPERSEDE_ON_RECORD", None)


def _write_path_verdicts(query: str, self_id: str) -> dict[str, str]:
    """Run the REAL write path; return {candidate_id: verdict}.

    Only ids check_conflict actually RETRIEVED are returned. Candidates are
    narrowed by FTS5 first, so a retrieval miss is indistinguishable from
    "classified distinct" — reporting it as distinct would invent agreement
    the sweep never observed.
    """
    from mcp_server.tools.check_conflict import check_conflict

    retrieved = {
        c["id"] for c in decisions_store.search(query, limit=20) if c.get("id")
    }
    r = check_conflict(query)
    verdicts = {i: "distinct" for i in retrieved if i != self_id}
    for c in r.get("conflicts") or []:
        verdicts[c["decision_id"]] = "conflict"
    for d in r.get("duplicates") or []:
        verdicts[d["decision_id"]] = "duplicate"
    verdicts.pop(self_id, None)
    return verdicts


# What each run must actually exercise. The protected run can never produce a
# `duplicate`: with a locked candidate, `if is_protected or is_negation_conflict`
# sends every hit to `conflicts`, so that branch is structurally unreachable
# there. Stating it per-run keeps the coverage floor honest instead of
# demanding something impossible and then being relaxed until it passes.
REQUIRED_VERDICTS = {
    False: {"duplicate", "conflict", "distinct"},
    True: {"conflict", "distinct"},
}


@pytest.mark.parametrize("protected", [False, True], ids=["unprotected", "protected"])
def test_write_path_agrees_with_the_shared_classifier(project, protected):
    ids = _seed(protected)
    seen: set[str] = set()
    pinned_hits = 0
    divergences: list[str] = []

    for qid, qtext in ids.items():
        for cid, verdict in _write_path_verdicts(qtext, qid).items():
            expected = reconcile.classify(qtext, ids[cid], b_protected=protected)[
                "kind"
            ]
            seen.add(verdict)
            if verdict == expected:
                continue
            if (expected, verdict, protected) in PINNED_DIVERGENCES:
                pinned_hits += 1
                continue
            divergences.append(
                f"    {qtext!r}\n      vs {ids[cid]!r}\n"
                f"      classify()={expected}  check_conflict={verdict}  "
                f"protected={protected}"
            )

    # A count floor is arbitrary; what matters is that every BRANCH of the
    # rule was exercised. Candidates are narrowed by FTS5 first, so a corpus
    # that never retrieves a duplicate pair leaves the duplicate branch
    # untested while the sweep still reports green — which is what the first
    # cut of this file did.
    assert seen == REQUIRED_VERDICTS[protected], (
        f"the sweep produced {sorted(seen)} verdicts, expected "
        f"{sorted(REQUIRED_VERDICTS[protected])}. Every reachable branch of "
        "the rule must be exercised or the agreement it reports is partial; "
        "add a corpus pair that lands in the missing one."
    )
    assert not divergences, (
        "the write path and the shared classifier disagree:\n"
        + "\n".join(divergences)
        + "\n\n  record_decision goes through check_conflict, NOT through "
        "classify. A rule that lives only in classify never runs on a real "
        "write — that is exactly how 4.1.0 shipped without its own fix.\n"
        "  If a divergence is intentional, add it to PINNED_DIVERGENCES with "
        "the reason."
    )

    # An exemption that never fires is dead config, and dead config is exactly
    # where a future real divergence would hide unnoticed. The protected run is
    # the one that must hit it.
    if protected:
        assert pinned_hits > 0, (
            "PINNED_DIVERGENCES was never exercised — either the corpus no "
            "longer contains a non-negated near-duplicate, or the divergence "
            "is gone and the entry should be deleted."
        )


def test_the_negation_rule_runs_on_the_real_write_path(project):
    """The 4.1.0 bug itself, asserted end-to-end rather than on classify().

    "never use pnpm" vs "use pnpm" is Jaccard 0.83 — a near-perfect textual
    duplicate. It must reach `conflicts`. Landing in `duplicates` is not a
    reporting nit: that list is what supersede-on-write consumes.
    """
    from mcp_server.tools.check_conflict import check_conflict

    os.environ["CODEVIRA_SUPERSEDE_ON_RECORD"] = "0"
    try:
        kept = decisions_store.record(
            decision="use pnpm as the package manager for all workspaces"
        )
    finally:
        os.environ.pop("CODEVIRA_SUPERSEDE_ON_RECORD", None)

    r = check_conflict("never use pnpm as the package manager for all workspaces")

    assert kept in [c["decision_id"] for c in r["conflicts"]], (
        f"a negated near-duplicate was NOT reported as a conflict: {r}"
    )
    assert kept not in [d["decision_id"] for d in r["duplicates"]], (
        "a contradiction reached the `duplicates` list, which supersede-on-"
        "write consumes — this silently retires the decision it contradicts"
    )
    assert r["status"] == "conflict", r["status"]


def test_the_negation_vocabulary_has_exactly_one_home():
    """A copied token set is how the rule diverges without any test noticing.

    check_conflict must IMPORT negation_disagrees, never restate the
    vocabulary. This is a source-level check because the divergence it
    prevents is invisible at runtime until the two lists drift.
    """
    src = Path(__file__).resolve().parent.parent
    cc = (src / "mcp_server" / "tools" / "check_conflict.py").read_text()

    assert "negation_disagrees" in cc, "check_conflict stopped using the shared rule"
    assert '"never"' not in cc and "'never'" not in cc, (
        "check_conflict appears to restate the negation vocabulary. It lives "
        "in reconcile._NEGATIONS and reaches here through negation_disagrees()."
    )

    owners = [
        p
        for p in (src / "mcp_server").rglob("*.py")
        if "_NEGATIONS = " in p.read_text()
    ]
    assert len(owners) == 1, (
        f"negation vocabulary defined in {len(owners)} places: {owners}"
    )
