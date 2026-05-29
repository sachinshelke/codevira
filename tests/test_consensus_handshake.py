"""
Tests for v3.1.0 M7 Phase C: consensus handshake protocol.

Covers:
  - config.get_flag / is_enabled
  - consensus_store: propose, resolve, find, status (pending,
    approved, rejected, withdrawn, expired), finalize with
    expired_unilateral safety
  - same-IDE fast path
  - MCP tools: feature-flag gate; opt-in behavior; consensus_propose,
    consensus_resolve, origin_of
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import (
    config,
    consensus_store,
    decisions_store,
    paths,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


def _enable_handshake(project: Path) -> None:
    """Toggle memory.consensus.handshake_enabled=true in config."""
    (project / ".codevira" / "config.yaml").write_text(
        "project:\n"
        "  name: test\n"
        "memory:\n"
        "  consensus:\n"
        "    handshake_enabled: true\n"
        "    handshake_timeout_days: 14\n"
    )


# ──────────────────────────────────────────────────────────────────────
# config helper
# ──────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_missing_file_returns_default(self, project: Path) -> None:
        (project / ".codevira" / "config.yaml").unlink()
        assert (
            config.get_flag("memory.consensus.handshake_enabled", default=False)
            is False
        )

    def test_unset_key_returns_default(self, project: Path) -> None:
        assert config.is_enabled("memory.nonexistent.flag") is False

    def test_explicit_true(self, project: Path) -> None:
        _enable_handshake(project)
        assert config.is_enabled("memory.consensus.handshake_enabled") is True
        assert config.get_flag("memory.consensus.handshake_timeout_days") == 14

    def test_malformed_yaml_returns_default(self, project: Path) -> None:
        (project / ".codevira" / "config.yaml").write_text(
            "::: not yaml ::: at all :::"
        )
        assert config.is_enabled("memory.consensus.handshake_enabled") is False


# ──────────────────────────────────────────────────────────────────────
# propose_supersession
# ──────────────────────────────────────────────────────────────────────


class TestPropose:
    def test_unknown_target_rejected(self, project: Path) -> None:
        r = consensus_store.propose_supersession(
            "D999999", new_decision="x", reason="missing"
        )
        assert r["proposed"] is False
        assert "not found" in r["error"]

    def test_cross_ide_opens_proposal(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Target was authored by claude_code.
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        target = decisions_store.record(decision="Use bcrypt", do_not_revert=True)
        # Cursor proposes superseding it.
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        r = consensus_store.propose_supersession(
            target, new_decision="Use Argon2 instead", reason="modern hash"
        )
        assert r["proposed"] is True
        assert "expires_at" in r
        # Default timeout: 14 days from now.
        exp = datetime.fromisoformat(r["expires_at"])
        now = datetime.now(timezone.utc)
        assert timedelta(days=13) < (exp - now) < timedelta(days=15)

    def test_same_ide_fast_path(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        target = decisions_store.record(decision="A", do_not_revert=True)
        # Same IDE proposes — fast path bypasses the handshake.
        r = consensus_store.propose_supersession(
            target, new_decision="B", reason="cleaner"
        )
        assert r.get("fast_path") is True
        assert r.get("ide_match") == "claude_code"
        # No proposal row appended.
        assert not paths.pending_conflicts_path().is_file()


# ──────────────────────────────────────────────────────────────────────
# resolve_proposal + lifecycle
# ──────────────────────────────────────────────────────────────────────


def _open_proposal(monkeypatch: pytest.MonkeyPatch) -> str:
    """Helper: open a cross-IDE proposal; return proposal_id."""
    monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
    target = decisions_store.record(decision="X", do_not_revert=True)
    monkeypatch.setenv("CODEVIRA_IDE", "cursor")
    r = consensus_store.propose_supersession(target, new_decision="Y", reason="bumped")
    return r["proposal_id"]


class TestResolveLifecycle:
    def test_unknown_proposal_rejected(self, project: Path) -> None:
        r = consensus_store.resolve_proposal("PC999999", action="approved")
        assert r["resolved"] is False

    def test_bad_action_rejected(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        r = consensus_store.resolve_proposal(pid, action="maybe")
        assert r["resolved"] is False
        assert "action must be one of" in r["error"]

    def test_pending_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        assert consensus_store.proposal_status(pid)["status"] == "pending"

    def test_approved_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        r = consensus_store.resolve_proposal(pid, action="approved")
        assert r["resolved"] is True
        assert consensus_store.proposal_status(pid)["status"] == "approved"

    def test_rejected_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        consensus_store.resolve_proposal(pid, action="rejected", comment="no")
        assert consensus_store.proposal_status(pid)["status"] == "rejected"

    def test_withdrawn_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        consensus_store.resolve_proposal(pid, action="withdrawn")
        assert consensus_store.proposal_status(pid)["status"] == "withdrawn"

    def test_latest_resolution_wins(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        consensus_store.resolve_proposal(pid, action="rejected")
        consensus_store.resolve_proposal(pid, action="approved")
        # Last write wins (mirrors decisions amendment semantics).
        assert consensus_store.proposal_status(pid)["status"] == "approved"

    def test_expired_status(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        # Travel 30 days into the future.
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        assert (
            consensus_store.proposal_status(pid, now=far_future)["status"] == "expired"
        )


# ──────────────────────────────────────────────────────────────────────
# finalize_proposal
# ──────────────────────────────────────────────────────────────────────


class TestFinalize:
    def test_pending_cannot_finalize(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        r = consensus_store.finalize_proposal(pid)
        assert r["finalized"] is False

    def test_approved_finalizes(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        consensus_store.resolve_proposal(pid, action="approved")
        r = consensus_store.finalize_proposal(pid)
        assert r["finalized"] is True
        assert r["new_decision_id"]

        # Target is now superseded; new decision exists.
        target_id = r["supersedes"]
        old = decisions_store.get(target_id)
        assert old["is_superseded"] is True
        new = decisions_store.get(r["new_decision_id"])
        assert new is not None
        assert "Y" in new["decision"]

    def test_expired_requires_unilateral_flag(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        r = consensus_store.finalize_proposal(pid, now=far_future)
        assert r["finalized"] is False
        assert "expired_unilateral=True" in r["error"]

    def test_expired_unilateral_finalizes_with_audit(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _open_proposal(monkeypatch)
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        r = consensus_store.finalize_proposal(
            pid, expired_unilateral=True, now=far_future
        )
        assert r["finalized"] is True
        assert r["expired_unilateral"] is True

        # Audit row appears in pending_conflicts with action='expired' and
        # expired_unilateral=True.
        from mcp_server.storage import jsonl_store

        rows = jsonl_store.read_all(paths.pending_conflicts_path())
        audit_rows = [
            r
            for r in rows
            if r.get("kind") == "resolution" and r.get("expired_unilateral") is True
        ]
        assert len(audit_rows) == 1
        assert audit_rows[0]["action"] == "expired"


# ──────────────────────────────────────────────────────────────────────
# MCP tools (feature-flag gate)
# ──────────────────────────────────────────────────────────────────────


class TestMcpToolsFeatureFlag:
    def test_propose_disabled_by_default(self, project: Path) -> None:
        from mcp_server.tools.consensus import consensus_propose_supersession

        r = consensus_propose_supersession(
            target_decision_id="D000001",
            new_decision="x",
            reason="y",
        )
        assert r["disabled"] is True

    def test_resolve_disabled_by_default(self, project: Path) -> None:
        from mcp_server.tools.consensus import consensus_resolve

        r = consensus_resolve(proposal_id="PC000001", action="approved")
        assert r["disabled"] is True

    def test_propose_when_enabled(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_handshake(project)
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        target = decisions_store.record(decision="x", do_not_revert=True)
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        from mcp_server.tools.consensus import consensus_propose_supersession

        r = consensus_propose_supersession(
            target_decision_id=target, new_decision="y", reason="z"
        )
        assert r["proposed"] is True


class TestOriginOf:
    def test_unknown_decision_returns_error(self, project: Path) -> None:
        from mcp_server.tools.consensus import origin_of

        r = origin_of("D999999")
        assert r["found"] is False

    def test_returns_origin_block(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "windsurf")
        did = decisions_store.record(decision="x")
        from mcp_server.tools.consensus import origin_of

        r = origin_of(did)
        assert r["found"] is True
        assert r["origin"]["ide"] == "windsurf"
