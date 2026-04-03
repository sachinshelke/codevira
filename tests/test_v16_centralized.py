"""
Tests for Codevira v1.6 centralized storage.

Covers:
  - _sanitize_path_key()
  - get_data_dir() resolution chain (centralized → git-remote → legacy → default)
  - _discover_project_root() via project markers
  - Migration: detect_migration_needed, migrate_to_centralized (idempotent, partial recovery)
  - GlobalDB: register_project with git_remote, find_project_by_remote
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mcp_server.paths as paths
from mcp_server.paths import (
    _sanitize_path_key,
    get_data_dir,
    get_project_root,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_project_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())


# ---------------------------------------------------------------------------
# _sanitize_path_key
# ---------------------------------------------------------------------------

class TestSanitizePathKey:
    def test_unix_path(self):
        key = _sanitize_path_key("/Users/sachin/Projects/Foo")
        # Human-readable part uses underscores for path separators, ends with hash
        assert key.startswith("Users_sachin_Projects_Foo_")
        assert len(key.split("_")[-1]) == 8  # 8-char hash suffix

    def test_unix_trailing_slash(self):
        # Trailing slash resolves to same path, so same key
        key1 = _sanitize_path_key("/Users/sachin/Projects/Foo/")
        key2 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert key1 == key2

    def test_path_with_spaces(self, tmp_path):
        p = tmp_path / "My Project"
        key = _sanitize_path_key(str(p))
        assert " " not in key

    def test_windows_drive_letter(self):
        key = _sanitize_path_key("C:\\Users\\sachin\\Projects")
        assert ":" not in key
        assert "\\" not in key

    def test_no_leading_trailing_hyphens(self):
        key = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert not key.startswith("-")
        assert not key.startswith("_")
        assert not key.endswith("-")

    def test_no_collision_between_hyphen_and_separator(self):
        """/foo-bar and /foo/bar must produce DIFFERENT keys."""
        key1 = _sanitize_path_key("/tmp/foo-bar")
        key2 = _sanitize_path_key("/tmp/foo/bar")
        assert key1 != key2

    def test_no_collision_across_drive_letters(self):
        """D:\\Projects\\Foo and C:\\Projects\\Foo must produce DIFFERENT keys."""
        key1 = _sanitize_path_key("C:\\Projects\\Foo")
        key2 = _sanitize_path_key("D:\\Projects\\Foo")
        assert key1 != key2

    def test_deterministic(self):
        key1 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        key2 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert key1 == key2


# ---------------------------------------------------------------------------
# get_data_dir resolution chain
# ---------------------------------------------------------------------------

class TestGetDataDir:
    def test_new_project_returns_centralized(self, tmp_path, monkeypatch):
        """New project with no .codevira/ → centralized path (default, step 4)."""
        project = tmp_path / "brand-new"
        project.mkdir()
        # Create a marker so project root is discovered
        (project / "pyproject.toml").write_text("[project]\nname='test'\n")

        _set_project_root(monkeypatch, project)
        # Redirect global home to tmp_path so we don't pollute real ~/.codevira
        fake_home = tmp_path / "global-home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        key = _sanitize_path_key(project)
        expected = fake_home / "projects" / key
        assert data == expected

    def test_centralized_dir_takes_priority(self, tmp_path, monkeypatch):
        """Centralized config.yaml exists → returns centralized path (step 1)."""
        project = tmp_path / "existing-project"
        project.mkdir()
        (project / ".git").mkdir()

        fake_home = tmp_path / "global-home"
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "config.yaml").write_text("project:\n  name: test\n")

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        assert data == centralized

    def test_legacy_fallback(self, tmp_path, monkeypatch):
        """Legacy .codevira/config.yaml exists, no centralized → returns legacy (step 3)."""
        project = tmp_path / "legacy-project"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: legacy\n")

        fake_home = tmp_path / "global-home"
        fake_home.mkdir()

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
        # No git remote (non-git project), centralized has no config.yaml
        data = get_data_dir()
        assert data == legacy.resolve()

    def test_centralized_beats_legacy(self, tmp_path, monkeypatch):
        """Both centralized and legacy exist → centralized wins (step 1 > step 3)."""
        project = tmp_path / "both-project"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: legacy\n")

        fake_home = tmp_path / "global-home"
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "config.yaml").write_text("project:\n  name: centralized\n")

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        assert data == centralized


# ---------------------------------------------------------------------------
# _discover_project_root via project markers
# ---------------------------------------------------------------------------

class TestDiscoverProjectRoot:
    def test_finds_root_via_git(self, tmp_path, monkeypatch):
        project = tmp_path / "git-project"
        nested = project / "src" / "feature"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_pyproject_toml(self, tmp_path, monkeypatch):
        project = tmp_path / "py-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='x'\n")

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_package_json(self, tmp_path, monkeypatch):
        project = tmp_path / "js-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / "package.json").write_text('{"name":"x"}')

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_stops_at_first_git_for_nested_repos(self, tmp_path, monkeypatch):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / ".git").mkdir()
        (inner / ".git").mkdir()

        _set_project_root(monkeypatch, inner)
        # Should stop at inner, not walk up to outer
        assert get_project_root() == inner.resolve()

    def test_falls_back_to_cwd_when_no_marker(self, tmp_path, monkeypatch):
        project = tmp_path / "no-markers"
        project.mkdir()
        _set_project_root(monkeypatch, project)
        assert get_project_root() == project.resolve()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_detect_migration_not_needed_when_no_legacy(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = tmp_path / "fresh"
        project.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path / "home")
        assert detect_migration_needed(project) is False

    def test_detect_migration_needed_with_legacy(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = tmp_path / "legacy"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        assert detect_migration_needed(project) is True

    def test_detect_migration_not_needed_if_already_done(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = tmp_path / "already-migrated"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "metadata.json").write_text('{"version": "1.6.0"}')
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        assert detect_migration_needed(project) is False

    def test_migrate_copies_config_and_roadmap(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "migrate-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: mig\n")
        (legacy / "roadmap.yaml").write_text("phases: []\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        result = migrate_to_centralized(project)

        assert result["migrated"] is True
        assert result["files_copied"] >= 2

        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        assert (centralized / "config.yaml").exists()
        assert (centralized / "roadmap.yaml").exists()

    def test_migrate_uses_sqlite_backup_for_graph_db(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "db-migrate"
        legacy = project / ".codevira"
        (legacy / "graph").mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        # Create a real SQLite DB with data
        src_db = legacy / "graph" / "graph.db"
        conn = sqlite3.connect(str(src_db))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'hello')")
        conn.commit()
        conn.close()

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        key = _sanitize_path_key(project)
        dst_db = fake_home / "projects" / key / "graph" / "graph.db"
        assert dst_db.exists()

        # Verify data integrity
        conn2 = sqlite3.connect(str(dst_db))
        row = conn2.execute("SELECT val FROM test WHERE id=1").fetchone()
        conn2.close()
        assert row[0] == "hello"

    def test_migrate_writes_metadata_json(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "meta-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        migrate_to_centralized(project)

        key = _sanitize_path_key(project)
        meta_file = fake_home / "projects" / key / "metadata.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["path_key"] == key
        assert meta["original_path"] == str(project)
        assert meta["version"] == "1.6.0"
        assert "migrated_at" in meta

    def test_migrate_renames_legacy_to_migrated(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "rename-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        migrate_to_centralized(project)

        assert not legacy.exists()
        assert (project / ".codevira.migrated").exists()

    def test_migrate_idempotent(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "idem-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        result1 = migrate_to_centralized(project)
        assert result1["migrated"] is True

        # Second call should be a no-op (legacy dir was renamed, so no migration needed)
        result2 = migrate_to_centralized(project)
        assert result2["migrated"] is False
        assert "reason" in result2

    def test_migrate_partial_recovery(self, tmp_path, monkeypatch):
        """If centralized dir exists but metadata.json is missing, migration re-runs."""
        from mcp_server.migrate import migrate_to_centralized, detect_migration_needed

        project = tmp_path / "partial-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        key = _sanitize_path_key(project)
        # Simulate partial migration: centralized dir exists but no metadata.json
        partial = fake_home / "projects" / key
        partial.mkdir(parents=True)

        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        # Should still detect migration needed (no metadata.json)
        assert detect_migration_needed(project) is True

        result = migrate_to_centralized(project)
        assert result["migrated"] is True
        assert (partial / "metadata.json").exists()

    def test_cleanup_legacy_dir(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized, cleanup_legacy_dir

        project = tmp_path / "cleanup-test"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: test\n")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        migrate_to_centralized(project)

        backup = project / ".codevira.migrated"
        assert backup.exists()

        removed = cleanup_legacy_dir(project)
        assert removed is True
        assert not backup.exists()

    def test_migrate_no_legacy_returns_false(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "no-legacy"
        project.mkdir()

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        result = migrate_to_centralized(project)
        assert result["migrated"] is False


# ---------------------------------------------------------------------------
# GlobalDB: git_remote support
# ---------------------------------------------------------------------------

class TestGlobalDBGitRemote:
    def test_register_with_git_remote(self, tmp_path):
        from indexer.global_db import GlobalDB

        db = GlobalDB(tmp_path / "global.db")
        db.register_project(
            path=str(tmp_path / "proj"),
            name="TestProj",
            language="python",
            git_remote="https://github.com/org/repo.git",
        )
        db.close()

        # Verify it was stored
        db2 = GlobalDB(tmp_path / "global.db")
        found = db2.find_project_by_remote("https://github.com/org/repo.git")
        db2.close()
        assert found == str(tmp_path / "proj")

    def test_find_project_by_remote_missing(self, tmp_path):
        from indexer.global_db import GlobalDB

        db = GlobalDB(tmp_path / "global.db")
        db.close()

        db2 = GlobalDB(tmp_path / "global.db")
        found = db2.find_project_by_remote("https://github.com/nonexistent/repo.git")
        db2.close()
        assert found is None

    def test_register_without_git_remote(self, tmp_path):
        """register_project with git_remote=None should not crash."""
        from indexer.global_db import GlobalDB

        db = GlobalDB(tmp_path / "global.db")
        db.register_project(
            path=str(tmp_path / "no-git"),
            name="NoGit",
            language="python",
            git_remote=None,
        )
        db.close()
