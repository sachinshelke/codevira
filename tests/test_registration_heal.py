"""
v3.7.0 (M2) — automatic, non-breaking registration heal.

Removing the stale per-project `codevira` entry a pre-3.7 init left behind, so
the single user-scope registration isn't shadowed by a duplicate (whose
hardcoded --project-dir can even pin the server to the wrong project). Surgical
(codevira key only), atomic, and NON-ORPHANING (never removes a per-project
entry unless a global one exists, so a per-project-only user keeps a server).
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server import ide_inject
from mcp_server.ide_inject import _has_codevira_entry, heal_stale_registration


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


def _point_globals_at(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(
        ide_inject, "_claude_global_config_path", lambda: home / ".claude.json"
    )
    monkeypatch.setattr(
        ide_inject, "_cursor_global_config_path", lambda: home / ".cursor" / "mcp.json"
    )
    monkeypatch.setattr(
        ide_inject,
        "_windsurf_global_config_path",
        lambda: home / ".windsurf" / "mcp.json",
    )


class TestHealStaleRegistration:
    def test_removes_per_project_codevira_but_preserves_other_servers(self, tmp_path):
        proj = tmp_path / "proj"
        mcp = proj / ".mcp.json"
        _write(
            mcp,
            {
                "codevira": {
                    "command": "codevira",
                    "args": ["--project-dir", str(proj)],
                },
                "some-other-mcp": {"command": "x"},
            },
        )

        cleaned = heal_stale_registration(proj, require_global=False)

        assert str(mcp) in cleaned
        servers = json.loads(mcp.read_text())["mcpServers"]
        assert "codevira" not in servers
        assert "some-other-mcp" in servers, "must never drop unrelated servers"

    def test_non_orphaning_keeps_entry_when_no_global_exists(
        self, tmp_path, monkeypatch
    ):
        proj = tmp_path / "proj"
        mcp = proj / ".mcp.json"
        _write(mcp, {"codevira": {"command": "codevira"}})
        _point_globals_at(monkeypatch, tmp_path / "empty-home")

        cleaned = heal_stale_registration(proj, require_global=True)

        assert cleaned == []
        assert _has_codevira_entry(mcp), "a per-project-only user must not be orphaned"

    def test_removes_when_global_entry_exists(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        mcp = proj / ".mcp.json"
        _write(mcp, {"codevira": {"command": "codevira"}})
        home = tmp_path / "home"
        _write(home / ".claude.json", {"codevira": {"command": "codevira"}})
        _point_globals_at(monkeypatch, home)

        cleaned = heal_stale_registration(proj, require_global=True)

        assert str(mcp) in cleaned
        assert not _has_codevira_entry(mcp)

    def test_noop_when_no_per_project_entry(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        assert heal_stale_registration(proj, require_global=False) == []
