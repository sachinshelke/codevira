"""4.0 Step 9 · S2 — a record identity that is the same on every machine.

``id`` is minted as ``max(id)+1``, so it is unique only within one store,
and ``id_repair`` renumbers it during a merge. It is not a name anything
can hold onto. ``uid`` is ``sha256`` of the record's content: two machines
holding the same decision compute the same value with no coordination, and
it survives renumbering.

The load-bearing test in this file is the uuid4 one. The plan asserted a
random uid "measurably breaks cherry-pick dedup"; this reproduces it,
because an assertion in a plan is not evidence.
"""

from __future__ import annotations

import copy
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, id_repair, paths, uid


ALICE = {"ide": "claude_code", "device_id": "aaaa1111"}
REC = {
    "id": "D000120",
    "ts": "2026-07-13T09:00:00+00:00",
    "decision": "Pin the cache TTL at 30s",
    "context": "Deploys served stale reads for 90s",
    "origin": ALICE,
}


class TestItIsTheSameEverywhere:
    def test_identical_content_yields_an_identical_uid(self) -> None:
        assert uid.compute(dict(REC)) == uid.compute(dict(REC))

    def test_different_content_yields_a_different_uid(self) -> None:
        other = dict(REC, decision="Pin the cache TTL at 5s")
        assert uid.compute(other) != uid.compute(REC)

    def test_key_order_does_not_matter(self) -> None:
        """Two machines serialise dicts in whatever order they built them."""
        shuffled = dict(reversed(list(REC.items())))
        assert uid.compute(shuffled) == uid.compute(REC)

    def test_the_same_words_on_two_days_are_two_decisions(self) -> None:
        """ts is deliberately IN the hash."""
        later = dict(REC, ts="2026-08-01T09:00:00+00:00")
        assert uid.compute(later) != uid.compute(REC)

    def test_two_people_recording_the_same_thing_are_two_decisions(self) -> None:
        """origin is deliberately IN the hash."""
        bob = dict(REC, origin={"ide": "cursor", "device_id": "bbbb2222"})
        assert uid.compute(bob) != uid.compute(REC)


class TestItSurvivesWhatIdCannot:
    """Every excluded field is excluded because id_repair rewrites it."""

    def test_renumbering_does_not_change_the_uid(self) -> None:
        assert uid.compute(dict(REC, id="Dabc123def456")) == uid.compute(REC)

    def test_an_amendment_pointer_does_not_change_the_uid(self) -> None:
        assert uid.compute(dict(REC, _amendment_to_id="D000999")) == uid.compute(REC)

    def test_repair_flags_do_not_change_the_uid(self) -> None:
        flagged = dict(REC, _amendment_ambiguous=True, _reference_ambiguous=True)
        assert uid.compute(flagged) == uid.compute(REC)

    def test_a_repointed_edge_does_not_change_the_uid(self) -> None:
        """S3a repoints superseded_by when its TARGET is renumbered. A
        record's identity must not change because something it points at
        moved."""
        assert uid.compute(dict(REC, superseded_by="D000200")) == uid.compute(REC)
        assert uid.compute(dict(REC, supersedes="D000100")) == uid.compute(REC)

    def test_it_is_stable_across_a_real_repair(self) -> None:
        """The property, end to end: normalize() renumbers a record and the
        uid it carries still identifies it."""
        bob = dict(REC, ts="2026-01-01T00:00:00+00:00", decision="Bob's")
        alice = dict(REC)
        alice["uid"] = uid.compute(alice)
        out = id_repair.normalize([bob, alice])

        renumbered = next(r for r in out["records"] if r["id"] != "D000120")
        assert renumbered["uid"] == uid.compute(REC)


class TestNotUuid4:
    """The plan's central claim, reproduced rather than trusted."""

    def test_a_random_uid_turns_one_decision_into_two(self) -> None:
        pair = [copy.deepcopy(REC), copy.deepcopy(REC)]
        assert id_repair.normalize(copy.deepcopy(pair))["deduped"] == 1

        randomised = copy.deepcopy(pair)
        for r in randomised:
            r["uid"] = uuid.uuid4().hex
        out = id_repair.normalize(randomised)
        assert out["deduped"] == 0
        assert len(out["records"]) == 2, "this is the failure uuid4 would cause"

    def test_a_content_uid_keeps_the_dedup(self) -> None:
        pair = [copy.deepcopy(REC), copy.deepcopy(REC)]
        for r in pair:
            r["uid"] = uid.compute(r)
        assert id_repair.normalize(pair)["deduped"] == 1


class TestNoBackfillIsNeeded:
    def test_a_pre_4_0_record_gets_the_same_uid_a_4_0_one_would(self) -> None:
        """Why no file is ever rewritten: the uid is recoverable from
        content that is already on disk."""
        legacy = {k: v for k, v in REC.items()}  # no stored uid
        modern = dict(REC, uid=uid.compute(REC))
        assert uid.uid_of(legacy) == uid.uid_of(modern)

    def test_a_stored_uid_wins_over_recomputation(self) -> None:
        """A hand-edited record keeps the identity its edges point at
        rather than silently becoming a different record."""
        edited = dict(REC, uid="pinned0000000000", decision="edited by hand")
        assert uid.uid_of(edited) == "pinned0000000000"

    @pytest.mark.parametrize("bad", [None, "str", 7, []])
    def test_garbage_does_not_raise(self, bad) -> None:
        assert uid.uid_of(bad) == ""

    def test_unserialisable_values_degrade(self) -> None:
        assert uid.compute({"decision": object()})


class TestOnTheWritePath:
    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "proj"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
        monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
        monkeypatch.chdir(root)
        paths.ensure_dirs(root)
        (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
        decisions_store.invalidate_merged_cache()
        return root

    def _rows(self, root: Path) -> list[dict]:
        p = root / ".codevira" / "decisions.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def test_a_recorded_decision_carries_a_uid(self, project: Path) -> None:
        decisions_store.record("something worth remembering")
        assert all(len(r["uid"]) == uid.UID_WIDTH for r in self._rows(project))

    def test_amendments_carry_one_too(self, project: Path) -> None:
        did = decisions_store.record("subject")
        decisions_store.mark_protected(did)
        decisions_store.supersede(did, "successor", reason="r")
        assert all(r.get("uid") for r in self._rows(project))

    def test_every_uid_in_a_store_is_distinct(self, project: Path) -> None:
        """A collision would merge two real decisions, so this is the
        property that matters more than the width."""
        for i in range(25):
            decisions_store.record(f"decision number {i}")
        uids = [r["uid"] for r in self._rows(project)]
        assert len(set(uids)) == len(uids)

    def test_the_uid_matches_what_a_reader_would_recompute(self, project: Path) -> None:
        """Stamped-at-write and derived-on-read must agree, or the
        no-backfill guarantee is hollow."""
        decisions_store.record("round trip")
        for r in self._rows(project):
            assert r["uid"] == uid.compute(r)

    def test_the_size_cost_stays_within_the_budget(self, project: Path) -> None:
        """D00012K caps committed state at 2MB/project. uid adds ~25 bytes
        per record; assert the order of magnitude rather than trusting it."""
        for i in range(100):
            decisions_store.record(f"decision {i}")
        rows = self._rows(project)
        overhead = sum(len(f'"uid":"{r["uid"]}",') for r in rows)
        assert overhead / len(rows) < 40
