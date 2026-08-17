"""``codevira reconcile`` — the surface Tier-1 reconcile never had.

``reconcile.cluster_store()`` and ``reconcile.pick_canonical()`` shipped in
4.1.0 complete and tested, and called by nothing: no MCP tool, no CLI
subcommand. 294 lines a user could not reach. This is the surface.

REPORT-ONLY by design. ``cluster_store`` returns a *plan*; executing it means
rewriting ``decisions.jsonl`` with supersessions and alias rewrites, which is
a separate piece of work on the most precious data in the product. A command
that can only read cannot corrupt anything, and knowing "you have N duplicate
clusters and M conflicts" is useful on its own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, paths


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


def _seed_duplicate_and_conflict() -> None:
    """One near-duplicate PAIR and one NEGATION conflict, nothing else.

    Recorded with supersede-on-write OFF so the store keeps both halves of the
    duplicate — otherwise record_decision would retire one on the way in and
    there would be nothing left to cluster.
    """
    import os

    os.environ["CODEVIRA_SUPERSEDE_ON_RECORD"] = "0"
    try:
        # duplicate pair: same decision, different words
        decisions_store.record(decision="adopt bcrypt hashing for passwords")
        decisions_store.record(decision="adopt bcrypt hashing for passwords everywhere")
        # negation conflict: one side negates, similarity is high
        decisions_store.record(decision="cache the invalidation path aggressively")
        decisions_store.record(
            decision="never cache the invalidation path aggressively"
        )
    finally:
        os.environ.pop("CODEVIRA_SUPERSEDE_ON_RECORD", None)


class TestReconcileReports:
    def test_reports_a_duplicate_cluster_and_a_conflict(self, project, capsys):
        from mcp_server.cli_reconcile import cmd_reconcile

        _seed_duplicate_and_conflict()
        rc = cmd_reconcile()
        out = capsys.readouterr().out

        assert rc == 0, out
        assert "1 duplicate cluster" in out or "1 merge" in out, out
        assert "1 conflict" in out, out

    def test_a_clean_store_reports_nothing_to_do(self, project, capsys):
        decisions_store.record(decision="use postgres for the primary datastore")
        decisions_store.record(decision="deploy the worker fleet on fargate")
        rc = cmd_reconcile_import()(project)
        out = capsys.readouterr().out
        assert rc == 0
        assert "nothing to reconcile" in out.lower(), out

    def test_it_never_writes(self, project, capsys):
        """The command is report-only. Assert the store is byte-identical
        after a run — a reconcile that silently mutated the decision log
        would be the worst possible bug in this product."""
        from mcp_server.cli_reconcile import cmd_reconcile

        _seed_duplicate_and_conflict()
        p = paths.decisions_path(project)
        before = p.read_bytes()
        cmd_reconcile()
        assert p.read_bytes() == before, "reconcile must not write to the store"

    def test_conflicts_are_never_offered_as_merges(self, project, capsys):
        """A negated pair must be surfaced for a human, never planned for a
        merge — that is the whole reason 4.1.0 exists."""
        from mcp_server.cli_reconcile import reconcile_report

        _seed_duplicate_and_conflict()
        rep = reconcile_report()
        merged_ids = {i for m in rep["merges"] for i in m.get("members", [])}
        conflict_ids = {i for c in rep["conflicts"] for i in (c.get("ids") or [])}
        assert conflict_ids, "expected the negation pair to be reported"
        assert not (merged_ids & conflict_ids), (
            f"a conflicting decision appeared in a merge plan: "
            f"{merged_ids & conflict_ids}"
        )


def cmd_reconcile_import():
    """Late import so the clean-store test fails loudly if the module is gone."""
    from mcp_server.cli_reconcile import cmd_reconcile

    return lambda _project: cmd_reconcile()
