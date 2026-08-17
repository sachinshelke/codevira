"""
cli_reconcile.py — ``codevira reconcile``: find decisions that say the same
thing, and pairs that contradict each other.

Tier-0 (``repair-ids``) resolves records that collide on an **id** — a
structural problem from two machines minting ``max+1`` independently. Tier-1,
this command, resolves records that say the same **thing** in different words:
two engineers recording the same decision in one repo, each with their own id.
Neither is a duplicate of the other; they are different failure modes.

REPORT-ONLY, deliberately.

``reconcile.cluster_store()`` returns a *plan* — which records would merge,
which one would win, and how ``[[Dxxxx]]`` references would be rewritten.
Executing that plan means rewriting ``decisions.jsonl`` with supersessions and
alias rewrites, which is a separate piece of work with its own failure modes,
on the most precious data in the product. A command that can only read cannot
corrupt anything, and the report is useful by itself.

CONFLICTS ARE NEVER MERGED. A pair where one side negates the other
("never do X" vs "do X") scores as a near-perfect textual duplicate — Jaccard
0.83 — which is exactly why merging on similarity alone is dangerous. Those
come back as conflicts for a human to resolve and never enter a merge plan.
"""

from __future__ import annotations

from typing import Any


def reconcile_report(
    *, max_records: int = 2000, project_root: Any = None
) -> dict[str, Any]:
    """Cluster the live decision store; return ``cluster_store``'s plan.

    Reads only the decisions that currently surface — ``list_all`` excludes
    superseded and outdated rows by default, and clustering a record that was
    already retired would propose re-merging history.

    Args:
        max_records: bound on the O(n^2) pairing, passed through.
        project_root: which project's store to read (None = current).

    Returns:
        ``{merges, conflicts, scanned, truncated}`` from ``cluster_store``.
    """
    from mcp_server.storage import decisions_store, reconcile

    page = decisions_store.list_all(
        limit=max_records, full=True, project_root=project_root
    )
    records = page.get("decisions", []) if page else []
    return reconcile.cluster_store(records, max_records=max_records)


def cmd_reconcile(*, verbose: bool = False, max_records: int = 2000) -> int:
    """Report duplicate clusters and conflicts in this project's decisions.

    Args:
        verbose: print every member id and the winning text, not just counts.
        max_records: bound on the pairing scan.

    Returns POSIX exit code. 0 even when findings exist — this is a report,
    not a gate; a non-zero exit would make it unusable in a shell pipeline
    and imply the store is broken when it is merely untidy.
    """
    from mcp_server.storage import paths

    try:
        rep = reconcile_report(max_records=max_records)
    except Exception as exc:  # noqa: BLE001 — a report must not traceback
        print(f"  ✗ reconcile failed: {exc}")
        return 1

    merges = rep.get("merges") or []
    conflicts = rep.get("conflicts") or []
    scanned = rep.get("scanned", 0)

    print()
    print(f"  Codevira — Reconcile ({scanned} decision(s) scanned)")
    print(f"  Project: {paths.get_project_root()}")
    print("  " + "─" * 60)
    print()

    if rep.get("truncated"):
        # Report the bound rather than applying it silently: a user who sees
        # "nothing to reconcile" over a truncated scan has been misled.
        print(
            f"  ⚠ scan truncated at {max_records} records — findings below are "
            "partial. Raise with --max-records."
        )
        print()

    if not merges and not conflicts:
        print("  ✓ nothing to reconcile — no duplicate clusters, no conflicts.")
        print()
        return 0

    if merges:
        print(f"  {len(merges)} duplicate cluster(s) — same decision, different words:")
        for m in merges:
            members = m.get("members") or []
            canonical = m.get("canonical_id") or "?"
            others = [i for i in members if i != canonical]
            flag = "  ⚠ AMBIGUOUS" if m.get("ambiguous") else ""
            print(f"    {canonical}  ← {', '.join(others) or '(alone)'}{flag}")
            if verbose:
                text = (m.get("canonical") or {}).get("decision") or ""
                print(f"        keeps: {text[:88]}")
        print()

    if conflicts:
        print(f"  {len(conflicts)} conflict(s) — these CONTRADICT, never merged:")
        for c in conflicts:
            pair = c.get("ids") or []
            sim = c.get("similarity")
            sim_s = f"  (similarity {sim:.2f})" if isinstance(sim, (int, float)) else ""
            print(f"    {' vs '.join(str(p) for p in pair)}{sim_s}")
        print()
        print("    A conflict needs a human verdict — supersede_decision the one")
        print("    that lost, or mark_decision_outdated it. Nothing is auto-applied.")
        print()

    print("  Report only — no changes were written.")
    print()
    return 0
