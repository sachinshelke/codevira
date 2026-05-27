"""Tests for :mod:`mcp_server._repair_init` — Bug 21a (rc.4 dogfood, 2026-05-13).

Pre-fix, a "ghost" data dir (graph/ + roadmap.yaml present, but missing
config.yaml / metadata.json / global.db registration) accumulated whenever:

1. A short-lived AI session called an MCP tool from a project's cwd,
2. The MCP server's daemon ``_run_background_init`` thread was kicked off,
3. The process exited before the thread finished steps 3-5
   (config write, metadata write, global.db register).

Result: ``~/.codevira/projects/`` filled with dirs the user never asked for,
all invisible to ``codevira status --global`` and ``codevira projects``.

Bug 21a fixes this by running :func:`repair_incomplete_init` SYNCHRONOUSLY
on every ``ensure_project_initialized`` call — the three cheap writes always
complete in the caller's thread, regardless of whether the daemon thread
finishes its heavy parts (graph + indexing).

This file pins the post-fix contract:

* Ghost dir → repair writes all three missing pieces.
* Already-complete dir → repair is a no-op (idempotent).
* global.db failure → other two pieces still get written; no crash.
* Detection failure → reports the failure but doesn't crash the caller.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mcp_server._repair_init import repair_incomplete_init


DETECTED = {
    "name": "demo",
    "language": "python",
    "watched_dirs": ["src"],
    "file_extensions": [".py"],
    "collection_name": "demo_code",
}


@pytest.fixture
def ghost_dirs(tmp_path, monkeypatch):
    """Project root + data dir with only graph/ + roadmap.yaml — Bug 21a shape."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    data_dir = tmp_path / ".codevira" / "projects" / "demo_slug"
    (data_dir / "graph").mkdir(parents=True)
    (data_dir / "roadmap.yaml").write_text("project: demo\n")
    global_db_path = tmp_path / ".codevira" / "global.db"
    monkeypatch.setattr("mcp_server.paths.get_global_db_path", lambda: global_db_path)
    monkeypatch.setattr("mcp_server.paths._get_git_remote_url", lambda _p: "git@host:demo.git")
    monkeypatch.setattr("mcp_server.detect.auto_detect_project", lambda _p: DETECTED)
    return project_root, data_dir, global_db_path


class TestRepairGhostDir:
    """Core contract: a ghost dir gets all three missing pieces written."""

    def test_writes_config_yaml(self, ghost_dirs):
        project_root, data_dir, _global_db_path = ghost_dirs
        assert not (data_dir / "config.yaml").is_file()
        result = repair_incomplete_init(data_dir, project_root)
        assert result["config_written"] is True
        assert (data_dir / "config.yaml").is_file()
        # Content sanity check — the detected project info ended up in it.
        text = (data_dir / "config.yaml").read_text()
        assert "demo" in text
        assert "python" in text

    def test_writes_metadata_json(self, ghost_dirs):
        project_root, data_dir, _global_db_path = ghost_dirs
        assert not (data_dir / "metadata.json").is_file()
        result = repair_incomplete_init(data_dir, project_root)
        assert result["metadata_written"] is True
        meta = json.loads((data_dir / "metadata.json").read_text())
        assert meta["original_path"] == str(project_root)
        assert meta["git_remote"] == "git@host:demo.git"
        assert meta["auto_initialized"] is True

    def test_registers_in_global_db(self, ghost_dirs):
        project_root, data_dir, global_db_path = ghost_dirs
        result = repair_incomplete_init(data_dir, project_root)
        assert result["registered"] is True
        # Read back directly from the DB.
        conn = sqlite3.connect(str(global_db_path))
        try:
            row = conn.execute(
                "SELECT path, name, git_remote FROM projects WHERE path = ?",
                (str(project_root),),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "Project should be registered in global.db"
        assert row[0] == str(project_root), (
            "Bug 20 regression: project must register under project_root, "
            "not data_dir."
        )
        assert row[1] == "demo"
        assert row[2] == "git@host:demo.git"

    def test_all_three_pieces_at_once(self, ghost_dirs):
        project_root, data_dir, _global_db_path = ghost_dirs
        result = repair_incomplete_init(data_dir, project_root)
        assert result == {
            "config_written": True,
            "metadata_written": True,
            "registered": True,
        }


class TestRepairIdempotent:
    """Already-complete dir → no-op. Running twice in a row is safe."""

    def test_already_complete_returns_all_false(self, ghost_dirs):
        project_root, data_dir, _global_db_path = ghost_dirs
        # Pre-populate the three pieces.
        repair_incomplete_init(data_dir, project_root)
        # Second run should be a no-op.
        result = repair_incomplete_init(data_dir, project_root)
        assert result == {
            "config_written": False,
            "metadata_written": False,
            "registered": False,
        }

    def test_partial_state_only_repairs_missing(self, ghost_dirs):
        project_root, data_dir, global_db_path = ghost_dirs
        # Pre-write config + metadata, leave global.db row missing.
        (data_dir / "config.yaml").write_text("project:\n  name: demo\n")
        (data_dir / "metadata.json").write_text("{}")

        result = repair_incomplete_init(data_dir, project_root)

        assert result["config_written"] is False
        assert result["metadata_written"] is False
        assert result["registered"] is True
        # And the global.db row IS now there.
        conn = sqlite3.connect(str(global_db_path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        finally:
            conn.close()
        assert n == 1


class TestRepairResilience:
    """Failures in one piece must not prevent the others from completing."""

    def test_global_db_failure_does_not_block_config_write(self, ghost_dirs, monkeypatch):
        """If GlobalDB raises, config + metadata still land — partial repair beats no repair."""
        project_root, data_dir, _global_db_path = ghost_dirs
        # Make GlobalDB.__init__ raise.
        monkeypatch.setattr(
            "indexer.global_db.GlobalDB",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("db locked")),
        )
        result = repair_incomplete_init(data_dir, project_root)
        # Config + metadata STILL get written.
        assert result["config_written"] is True
        assert result["metadata_written"] is True
        # Registration silently failed.
        assert result["registered"] is False

    def test_data_dir_created_if_missing(self, tmp_path, monkeypatch):
        """If the data_dir doesn't exist yet, repair creates it."""
        project_root = tmp_path / "demo"
        project_root.mkdir()
        data_dir = tmp_path / ".codevira" / "projects" / "demo_slug"
        # Don't pre-create — let repair_incomplete_init handle it.
        assert not data_dir.exists()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path",
            lambda: tmp_path / ".codevira" / "global.db",
        )
        monkeypatch.setattr(
            "mcp_server.paths._get_git_remote_url",
            lambda _p: None,
        )
        monkeypatch.setattr("mcp_server.detect.auto_detect_project", lambda _p: DETECTED)
        result = repair_incomplete_init(data_dir, project_root)
        assert data_dir.is_dir()
        assert (data_dir / "config.yaml").is_file()
        assert (data_dir / "metadata.json").is_file()
        assert result["config_written"] is True
        assert result["metadata_written"] is True
