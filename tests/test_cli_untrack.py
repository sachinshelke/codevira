"""
test_cli_untrack.py — v3.7.1 fix C: ``codevira untrack <project>`` coverage.

``untrack`` is the surgical inverse of ``codevira init`` for ONE project. It
must remove that project's per-project IDE-config entries (chiefly the
Antigravity ``codevira-<name>`` entries fix B writes) AND its centralized data
dir, while leaving the bare global entry and every OTHER project untouched.

These tests isolate the config surfaces + global home so we can verify the
scoping without touching the real machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server import cli, ide_inject
from mcp_server import paths as paths_mod


def _seed_antigravity_config(cfg: Path, projects: dict[str, Path]) -> None:
    servers: dict = {"codevira": {"command": "cv", "args": []}}  # bare global
    for name, proj in projects.items():
        servers[f"codevira-{name}"] = {
            "command": "cv",
            "args": ["--project-dir", str(proj)],
        }
    servers["unrelated-server"] = {"command": "x"}
    cfg.write_text(json.dumps({"mcpServers": servers}))


@pytest.fixture
def isolated_untrack(tmp_path, monkeypatch):
    """Point every config surface + the global home at tmp_path."""
    cfg = tmp_path / "gemini" / "mcp_config.json"
    cfg.parent.mkdir(parents=True)
    global_home = tmp_path / "global"
    (global_home / "projects").mkdir(parents=True)

    # Antigravity is the only surface with entries; the rest resolve to
    # non-existent temp paths so they're harmlessly skipped.
    monkeypatch.setattr(ide_inject, "_antigravity_write_targets", lambda: [cfg])
    for name in (
        "_claude_global_config_path",
        "_cursor_global_config_path",
        "_windsurf_global_config_path",
        "_claude_desktop_config_path",
    ):
        monkeypatch.setattr(ide_inject, name, lambda: tmp_path / "nonexistent.json")
    for name in ("_claude_config_path", "_cursor_config_path", "_windsurf_config_path"):
        monkeypatch.setattr(ide_inject, name, lambda pr: tmp_path / "nonexistent.json")
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)
    return cfg, global_home


def test_untrack_removes_only_target_project(isolated_untrack, tmp_path):
    cfg, global_home = isolated_untrack
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    _seed_antigravity_config(cfg, {"alpha": alpha, "beta": beta})
    # Seed alpha's data dir.
    alpha_key = paths_mod._sanitize_path_key(alpha)
    alpha_data = global_home / "projects" / alpha_key
    alpha_data.mkdir()
    (alpha_data / "config.yaml").write_text("schema_version: 1\n")

    rc = cli.cmd_untrack(path=str(alpha), yes=True)
    assert rc == 0

    servers = json.loads(cfg.read_text())["mcpServers"]
    # alpha's entry gone; bare global, beta, and unrelated survive.
    assert set(servers) == {"codevira", "codevira-beta", "unrelated-server"}
    # alpha's data dir removed.
    assert not alpha_data.exists()


def test_untrack_dry_run_writes_nothing(isolated_untrack, tmp_path):
    cfg, global_home = isolated_untrack
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    _seed_antigravity_config(cfg, {"alpha": alpha})
    alpha_data = global_home / "projects" / paths_mod._sanitize_path_key(alpha)
    alpha_data.mkdir()
    before = cfg.read_text()

    rc = cli.cmd_untrack(path=str(alpha), dry_run=True)
    assert rc == 0
    assert cfg.read_text() == before  # nothing removed
    assert alpha_data.exists()


def test_untrack_nothing_to_do_returns_zero(isolated_untrack, tmp_path):
    cfg, _ = isolated_untrack
    _seed_antigravity_config(cfg, {})  # only bare global + unrelated
    never_tracked = tmp_path / "ghost"
    never_tracked.mkdir()

    rc = cli.cmd_untrack(path=str(never_tracked), yes=True)
    assert rc == 0
    # Bare global entry left intact — untrack is project-scoped.
    assert "codevira" in json.loads(cfg.read_text())["mcpServers"]
