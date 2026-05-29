"""
Tests for mcp_server.cli_consensus + mcp_server.storage.consensus_store
+ mcp_server.tools.consensus — v3.1.0 M6 Phase B.

Covers:
  - read/write checkpoint per IDE
  - append_conflict + list_pending
  - scan_and_materialize: scans only foreign decisions; respects
    checkpoint; advances checkpoint; surfaces duplicate vs
    asymmetric-conflict shapes; bails out cleanly on
    CODEVIRA_IDE=unknown.
  - cmd_consensus_check stdout + return codes.
  - get_session_context gains a 'consensus' panel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.cli_consensus import cmd_consensus_check
from mcp_server.storage import consensus_store, decisions_store, paths
from mcp_server.tools.consensus import consensus_check, consensus_status


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# Checkpoint
# ──────────────────────────────────────────────────────────────────────


class TestCheckpoint:
    def test_first_read_returns_empty(self, project: Path) -> None:
        assert consensus_store.read_checkpoint("claude_code") == {}

    def test_write_then_read_roundtrip(self, project: Path) -> None:
        consensus_store.write_checkpoint("cursor", last_seen_decision_id="D000123")
        cp = consensus_store.read_checkpoint("cursor")
        assert cp["last_seen_decision_id"] == "D000123"
        assert cp["_schema_v"] == 1
        # File lives at the documented path.
        assert paths.ide_checkpoint_path("cursor").is_file()

    def test_malformed_checkpoint_returns_empty(self, project: Path) -> None:
        path = paths.ide_checkpoint_path("windsurf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{this is not json")
        assert consensus_store.read_checkpoint("windsurf") == {}


# ──────────────────────────────────────────────────────────────────────
# scan_and_materialize
# ──────────────────────────────────────────────────────────────────────


class TestScanAndMaterialize:
    def test_unknown_ide_bails_out(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEVIRA_IDE", raising=False)
        # Seed a decision so the scan would have something to look at.
        decisions_store.record(decision="x")
        summary = consensus_store.scan_and_materialize()
        assert summary["conflicts_recorded"] == 0
        assert "skipped_reason" in summary

    def test_no_foreign_decisions_records_nothing(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        # All decisions written by THIS IDE → no foreign rows → no conflicts.
        decisions_store.record(decision="Use bcrypt", do_not_revert=True)
        decisions_store.record(decision="Rate-limit logins")
        summary = consensus_store.scan_and_materialize()
        assert summary["foreign"] == 0
        assert summary["conflicts_recorded"] == 0

    def test_foreign_duplicate_recorded(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(
            decision="Use bcrypt for password hashing", do_not_revert=True
        )
        # Switch IDEs, write a near-duplicate.
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(decision="Use bcrypt for password hashing")
        # Run scan from cursor's perspective.
        summary = consensus_store.scan_and_materialize()
        # No, claude_code's decision is current_ide=cursor's foreign;
        # cursor's decision is current_ide=cursor's own. So the foreign
        # one (claude_code's) gets paired against cursor's current set.
        # But scan from cursor's POV: 1 conflict expected.
        # Actually scan is from CURRENT_IDE = cursor, so claude_code's
        # decision is foreign, cursor's is current. Pair: 1 conflict.
        assert summary["foreign"] == 1
        assert summary["conflicts_recorded"] == 1
        pending = consensus_store.list_pending()
        assert len(pending) == 1
        pc = pending[0]
        assert pc["conflict_kind"] == "duplicate"
        assert pc["current_ide"] == "cursor"
        assert pc["foreign_origin"]["ide"] == "claude_code"

    def test_checkpoint_advances_after_scan(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        d1 = decisions_store.record(decision="A")
        d2 = decisions_store.record(decision="B")
        summary = consensus_store.scan_and_materialize()
        assert summary["new_checkpoint"] in (d1, d2)
        cp = consensus_store.read_checkpoint("claude_code")
        assert cp["last_seen_decision_id"] == summary["new_checkpoint"]

    def test_second_scan_only_sees_new_decisions(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(decision="A")
        consensus_store.scan_and_materialize()
        # New decision after the checkpoint.
        decisions_store.record(decision="B")
        summary = consensus_store.scan_and_materialize()
        assert summary["scanned"] == 1  # only B

    def test_supersededs_skipped(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        d_old = decisions_store.record(decision="old decision text", do_not_revert=True)
        # Cursor writes a near-duplicate.
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(decision="old decision text")
        # Then claude_code supersedes its own.
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.supersede(d_old, "old decision text v2", reason="bumped")
        # Now scan from cursor's POV; the foreign superseded one should be skipped.
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        # Checkpoint was never set for cursor → all foreign decisions
        # are scanned, but superseded should still be excluded.
        consensus_store.scan_and_materialize()
        pending = consensus_store.list_pending()
        for pc in pending:
            assert (
                pc["foreign_decision_id"] != d_old
                or pc["foreign_origin"]["ide"] == "cursor"
            )


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


class TestCmdConsensusCheck:
    def test_unknown_ide_prints_skip_message(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("CODEVIRA_IDE", raising=False)
        decisions_store.record(decision="x")
        rc = cmd_consensus_check()
        assert rc == 0
        out = capsys.readouterr().out
        assert "skipped" in out
        assert "CODEVIRA_IDE" in out

    def test_no_decisions_returns_zero(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        rc = cmd_consensus_check()
        assert rc == 0
        out = capsys.readouterr().out
        assert "scanned 0" in out

    def test_records_and_reports_conflicts(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(
            decision="Use bcrypt for password hashing", do_not_revert=True
        )
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(decision="Use bcrypt for password hashing")
        rc = cmd_consensus_check()
        assert rc == 0
        out = capsys.readouterr().out
        assert "conflicts recorded: 1" in out


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────


class TestMcpTools:
    def test_consensus_status_empty(self, project: Path) -> None:
        r = consensus_status()
        assert r["count"] == 0
        assert r["pending"] == []

    def test_consensus_check_then_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(
            decision="Use bcrypt for password hashing", do_not_revert=True
        )
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(decision="Use bcrypt for password hashing")
        summary = consensus_check()
        assert summary["conflicts_recorded"] == 1
        status = consensus_status(top_k=5)
        assert status["count"] == 1
        assert status["pending"][0]["conflict_kind"] == "duplicate"


# ──────────────────────────────────────────────────────────────────────
# get_session_context consensus panel
# ──────────────────────────────────────────────────────────────────────


class TestSessionContextConsensusPanel:
    def test_empty_panel(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch _setup_project's expected mocks minimally.
        from mcp_server.tools import learning

        with monkeypatch.context() as m:
            m.setattr(
                "mcp_server.tools.roadmap.get_roadmap",
                lambda *_a, **_kw: {"current_phase": {}},
                raising=False,
            )
            ctx = learning.get_session_context()
        assert "consensus" in ctx
        assert ctx["consensus"]["pending_count"] == 0

    def test_populated_panel(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(
            decision="Use bcrypt for password hashing", do_not_revert=True
        )
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(decision="Use bcrypt for password hashing")
        consensus_store.scan_and_materialize()  # populate pending_conflicts

        from mcp_server.tools import learning

        with monkeypatch.context() as m:
            m.setattr(
                "mcp_server.tools.roadmap.get_roadmap",
                lambda *_a, **_kw: {"current_phase": {}},
                raising=False,
            )
            ctx = learning.get_session_context()
        assert ctx["consensus"]["pending_count"] >= 1
        assert ctx["consensus"]["top"][0]["conflict_kind"] == "duplicate"
