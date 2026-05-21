"""
Tests for mcp_server/migrate.py — Legacy -> Centralized storage migration.

Covers:
  - detect_migration_needed(): conditions for triggering migration
  - migrate_to_centralized(): full migration pipeline
    - config.yaml + roadmap.yaml copy
    - graph.db via sqlite3 backup API (WAL-safe)
    - codeindex/ directory (ChromaDB data)
    - metadata.json generation
    - global.db project registry update
    - legacy dir rename to .codevira.migrated/
  - cleanup_legacy_dir(): removes backup directory
  - _ensure_git_remote_column(): schema upgrade for global_db
  - Idempotency: second call is no-op

Chaos tests:
  - Corrupt graph.db -> fallback to shutil.copy2 + WAL/SHM
  - Permission denied on centralized dir creation
  - Partial recovery (metadata.json missing)
  - Concurrent migrations (second call is no-op)
  - Legacy dir with read-only files
  - Non-git project (git_remote = None)
  - cleanup_legacy_dir when dir does not exist
"""

from __future__ import annotations

import json
import sqlite3
import sys
from unittest.mock import patch, MagicMock

import pytest

import mcp_server.paths as paths
from mcp_server.paths import _sanitize_path_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_legacy_project(
    tmp_path,
    name="test-project",
    with_roadmap=False,
    with_graph_db=False,
    with_codeindex=False,
    with_changesets=False,
):
    """Create a legacy project with .codevira/ directory structure."""
    project = tmp_path / name
    legacy = project / ".codevira"
    legacy.mkdir(parents=True)
    (legacy / "config.yaml").write_text(f"project:\n  name: {name}\n")

    if with_roadmap:
        (legacy / "roadmap.yaml").write_text("phases: []\n")

    if with_graph_db:
        graph_dir = legacy / "graph"
        graph_dir.mkdir(parents=True)
        db_path = graph_dir / "graph.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test_data VALUES (1, 'migrated_value')")
        conn.commit()
        conn.close()

    if with_codeindex:
        idx_dir = legacy / "codeindex"
        idx_dir.mkdir(parents=True)
        (idx_dir / "index.bin").write_bytes(b"\x00" * 64)
        (idx_dir / "metadata.json").write_text('{"collection": "test"}')

    if with_changesets:
        cs_dir = legacy / "graph" / "changesets"
        cs_dir.mkdir(parents=True, exist_ok=True)
        (cs_dir / "cs-001.yaml").write_text("changeset: cs-001\nstatus: complete\n")
        (cs_dir / "cs-002.yaml").write_text("changeset: cs-002\nstatus: in_progress\n")

    return project


def _setup_fake_home(tmp_path, monkeypatch, name="home"):
    """Create and patch a fake global home directory."""
    fake_home = tmp_path / name
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
    return fake_home


# ===================================================================
# detect_migration_needed
# ===================================================================


class TestDetectMigrationNeeded:
    """Test conditions for triggering migration."""

    def test_not_needed_when_no_legacy(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = tmp_path / "fresh"
        project.mkdir()
        _setup_fake_home(tmp_path, monkeypatch)
        assert detect_migration_needed(project) is False

    def test_needed_with_legacy_config(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = _create_legacy_project(tmp_path, "legacy")
        _setup_fake_home(tmp_path, monkeypatch)
        assert detect_migration_needed(project) is True

    def test_not_needed_if_already_migrated(self, tmp_path, monkeypatch):
        from mcp_server.migrate import detect_migration_needed

        project = _create_legacy_project(tmp_path, "already-migrated")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "metadata.json").write_text('{"version": "1.6.0"}')

        assert detect_migration_needed(project) is False

    def test_needed_if_partial_migration(self, tmp_path, monkeypatch):
        """Centralized dir exists but metadata.json is missing -> re-run needed."""
        from mcp_server.migrate import detect_migration_needed

        project = _create_legacy_project(tmp_path, "partial")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        key = _sanitize_path_key(project)
        partial = fake_home / "projects" / key
        partial.mkdir(parents=True)
        # No metadata.json -> still needs migration

        assert detect_migration_needed(project) is True

    def test_not_needed_without_config_yaml(self, tmp_path, monkeypatch):
        """Legacy dir exists but without config.yaml -> not a real project."""
        from mcp_server.migrate import detect_migration_needed

        project = tmp_path / "no-config"
        (project / ".codevira").mkdir(parents=True)
        # No config.yaml
        _setup_fake_home(tmp_path, monkeypatch)

        assert detect_migration_needed(project) is False


# ===================================================================
# migrate_to_centralized — core pipeline
# ===================================================================


class TestMigrateToCentralized:
    """Test the full migration pipeline."""

    def test_copies_config_and_roadmap(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "mig-config", with_roadmap=True)
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)

        assert result["migrated"] is True
        assert result["files_copied"] >= 2

        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        assert (centralized / "config.yaml").exists()
        assert (centralized / "roadmap.yaml").exists()

    def test_sqlite_backup_for_graph_db(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "db-mig", with_graph_db=True)
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        key = _sanitize_path_key(project)
        dst_db = fake_home / "projects" / key / "graph" / "graph.db"
        assert dst_db.exists()

        # Verify data integrity
        conn = sqlite3.connect(str(dst_db))
        row = conn.execute("SELECT val FROM test_data WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "migrated_value"

    def test_copies_codeindex_directory(self, tmp_path, monkeypatch):
        """Migration copies codeindex/ (ChromaDB) directory."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "idx-mig", with_codeindex=True)
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        key = _sanitize_path_key(project)
        dst_idx = fake_home / "projects" / key / "codeindex"
        assert dst_idx.exists()
        assert (dst_idx / "index.bin").exists()
        assert (dst_idx / "metadata.json").exists()

    # v2.2.0+: test_copies_changesets_directory removed (changesets
    # feature deleted; migration no longer touches that path).

    def test_writes_metadata_json(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "meta-test")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        migrate_to_centralized(project)

        key = _sanitize_path_key(project)
        meta_file = fake_home / "projects" / key / "metadata.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["path_key"] == key
        assert meta["original_path"] == str(project)
        from mcp_server import __version__

        assert meta["version"] == __version__
        assert "migrated_at" in meta

    def test_metadata_contains_git_remote(self, tmp_path, monkeypatch):
        """metadata.json records the git remote URL."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "git-meta")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        with patch(
            "mcp_server.paths._get_git_remote_url",
            return_value="https://github.com/org/repo.git",
        ):
            migrate_to_centralized(project)

        key = _sanitize_path_key(project)
        meta = json.loads((fake_home / "projects" / key / "metadata.json").read_text())
        assert meta["git_remote"] == "https://github.com/org/repo.git"

    def test_renames_legacy_to_migrated(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "rename-test")
        _ = _setup_fake_home(tmp_path, monkeypatch)

        migrate_to_centralized(project)

        legacy = project / ".codevira"
        assert not legacy.exists()
        assert (project / ".codevira.migrated").exists()

    def test_no_legacy_returns_false(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = tmp_path / "no-legacy"
        project.mkdir()
        _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is False
        assert "reason" in result

    def test_non_git_project(self, tmp_path, monkeypatch):
        """Non-git project (git_remote=None) migrates successfully."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "no-git")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        with patch("mcp_server.paths._get_git_remote_url", return_value=None):
            result = migrate_to_centralized(project)

        assert result["migrated"] is True

        key = _sanitize_path_key(project)
        meta = json.loads((fake_home / "projects" / key / "metadata.json").read_text())
        assert meta["git_remote"] is None

    def test_without_roadmap_file(self, tmp_path, monkeypatch):
        """Migration works even when roadmap.yaml does not exist."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "no-roadmap", with_roadmap=False)
        _ = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True
        # Only config.yaml copied
        assert result["files_copied"] == 1


# ===================================================================
# Idempotency and concurrent migrations
# ===================================================================


class TestMigrationIdempotency:
    """Verify migration is safe to call multiple times."""

    def test_idempotent_second_call_is_noop(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "idem-test")
        _setup_fake_home(tmp_path, monkeypatch)

        result1 = migrate_to_centralized(project)
        assert result1["migrated"] is True

        # Second call: legacy dir was renamed, so no migration needed
        result2 = migrate_to_centralized(project)
        assert result2["migrated"] is False
        assert "reason" in result2

    def test_already_migrated_returns_false(self, tmp_path, monkeypatch):
        """If centralized metadata.json exists, returns already migrated."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "already-done")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        # Manually create the metadata.json to simulate a prior migration
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "metadata.json").write_text('{"version": "1.6.0"}')

        result = migrate_to_centralized(project)
        assert result["migrated"] is False
        assert "Already migrated" in result["reason"]

    def test_concurrent_migration_second_is_noop(self, tmp_path, monkeypatch):
        """Simulate concurrent migration — second call sees metadata.json."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "concurrent")
        _ = _setup_fake_home(tmp_path, monkeypatch)

        # First migration
        result1 = migrate_to_centralized(project)
        assert result1["migrated"] is True

        # Recreate legacy dir (simulating a race condition scenario)
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: concurrent\n")

        # Second migration: metadata.json already present
        result2 = migrate_to_centralized(project)
        assert result2["migrated"] is False


# ===================================================================
# cleanup_legacy_dir
# ===================================================================


class TestCleanupLegacyDir:
    """Test removal of .codevira.migrated/ backup."""

    def test_cleanup_removes_backup(self, tmp_path, monkeypatch):
        from mcp_server.migrate import migrate_to_centralized, cleanup_legacy_dir

        project = _create_legacy_project(tmp_path, "cleanup-test")
        _setup_fake_home(tmp_path, monkeypatch)

        migrate_to_centralized(project)

        backup = project / ".codevira.migrated"
        assert backup.exists()

        removed = cleanup_legacy_dir(project)
        assert removed is True
        assert not backup.exists()

    def test_cleanup_returns_false_when_no_backup(self, tmp_path):
        """cleanup_legacy_dir returns False when .codevira.migrated/ does not exist."""
        from mcp_server.migrate import cleanup_legacy_dir

        project = tmp_path / "no-backup"
        project.mkdir()

        result = cleanup_legacy_dir(project)
        assert result is False

    def test_cleanup_twice_second_returns_false(self, tmp_path, monkeypatch):
        """Calling cleanup twice: first True, second False."""
        from mcp_server.migrate import migrate_to_centralized, cleanup_legacy_dir

        project = _create_legacy_project(tmp_path, "cleanup-twice")
        _setup_fake_home(tmp_path, monkeypatch)

        migrate_to_centralized(project)

        assert cleanup_legacy_dir(project) is True
        assert cleanup_legacy_dir(project) is False


# ===================================================================
# _ensure_git_remote_column
# ===================================================================


class TestEnsureGitRemoteColumn:
    """Test schema upgrade helper."""

    def test_adds_column_if_missing(self, tmp_path):
        """Adds git_remote column to projects table if not present."""
        from mcp_server.migrate import _ensure_git_remote_column

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT)")
        conn.commit()

        # Create a mock gdb with the connection
        class FakeGDB:
            pass

        gdb = FakeGDB()
        gdb.conn = conn

        _ensure_git_remote_column(gdb)

        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        ]
        assert "git_remote" in cols
        conn.close()

    def test_noop_if_column_exists(self, tmp_path):
        """Does nothing if git_remote column already exists."""
        from mcp_server.migrate import _ensure_git_remote_column

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT, git_remote TEXT)"
        )
        conn.commit()

        class FakeGDB:
            pass

        gdb = FakeGDB()
        gdb.conn = conn

        # Should not raise
        _ensure_git_remote_column(gdb)

        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        ]
        assert cols.count("git_remote") == 1
        conn.close()

    def test_handles_exception_gracefully(self, tmp_path):
        """Swallows exceptions (e.g., locked database)."""
        from mcp_server.migrate import _ensure_git_remote_column

        class FakeGDB:
            pass

        gdb = FakeGDB()
        gdb.conn = MagicMock()
        gdb.conn.execute.side_effect = sqlite3.OperationalError("database is locked")

        # Should not raise
        _ensure_git_remote_column(gdb)


# ===================================================================
# Partial recovery
# ===================================================================


class TestPartialRecovery:
    """Test recovery from interrupted migrations."""

    def test_partial_migration_reruns(self, tmp_path, monkeypatch):
        """If centralized dir exists but metadata.json is missing, migration re-runs."""
        from mcp_server.migrate import migrate_to_centralized, detect_migration_needed

        project = _create_legacy_project(tmp_path, "partial-test")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        key = _sanitize_path_key(project)
        partial = fake_home / "projects" / key
        partial.mkdir(parents=True)

        assert detect_migration_needed(project) is True

        result = migrate_to_centralized(project)
        assert result["migrated"] is True
        assert (partial / "metadata.json").exists()

    def test_existing_migrated_backup_replaced(self, tmp_path, monkeypatch):
        """If .codevira.migrated/ already exists from a prior attempt, it is replaced."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "backup-replace")
        _ = _setup_fake_home(tmp_path, monkeypatch)

        # Create a stale backup from a prior interrupted migration
        stale_backup = project / ".codevira.migrated"
        stale_backup.mkdir(parents=True)
        (stale_backup / "old-config.yaml").write_text("stale")

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        # The old stale backup should be replaced
        assert (project / ".codevira.migrated").exists()
        assert not (project / ".codevira.migrated" / "old-config.yaml").exists()


# ===================================================================
# CHAOS Tests
# ===================================================================


class TestMigrateChaos:
    """Edge cases, corruptions, and adversarial inputs."""

    def test_corrupt_graph_db_falls_back_to_copy(self, tmp_path, monkeypatch):
        """Corrupt graph.db -> sqlite3 backup fails -> fallback to shutil.copy2."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "corrupt-db")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        # Write a corrupt .db file
        graph_dir = project / ".codevira" / "graph"
        graph_dir.mkdir(parents=True)
        corrupt_db = graph_dir / "graph.db"
        corrupt_db.write_bytes(b"NOT A SQLITE DATABASE" + b"\x00" * 100)

        # Also create WAL/SHM files to test fallback copies them
        (graph_dir / "graph.db-wal").write_bytes(b"fake wal data")
        (graph_dir / "graph.db-shm").write_bytes(b"fake shm data")

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        # Fallback should have copied the file (even though it is corrupt)
        key = _sanitize_path_key(project)
        dst_db = fake_home / "projects" / key / "graph" / "graph.db"
        assert dst_db.exists()
        # WAL/SHM should also be copied in fallback
        assert (dst_db.parent / "graph.db-wal").exists()
        assert (dst_db.parent / "graph.db-shm").exists()

    def test_global_db_update_failure_does_not_block(self, tmp_path, monkeypatch):
        """Failure to update global.db during migration does not stop the migration."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "gdb-fail")
        _ = _setup_fake_home(tmp_path, monkeypatch)

        # Mock GlobalDB to raise an exception (imported locally in migrate_to_centralized)
        with patch(
            "indexer.global_db.GlobalDB.__init__",
            side_effect=Exception("DB init failed"),
        ):
            result = migrate_to_centralized(project)

        assert result["migrated"] is True
        # Migration completed despite global.db failure

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
    def test_legacy_dir_with_readonly_files(self, tmp_path, monkeypatch):
        """Legacy dir with read-only files still migrates (reads are sufficient)."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "readonly")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        # Make config.yaml read-only
        config = project / ".codevira" / "config.yaml"
        config.chmod(0o444)

        try:
            result = migrate_to_centralized(project)
            assert result["migrated"] is True

            key = _sanitize_path_key(project)
            assert (fake_home / "projects" / key / "config.yaml").exists()
        finally:
            # Restore permissions for cleanup
            if config.exists():
                config.chmod(0o644)
            migrated = project / ".codevira.migrated"
            if migrated.exists():
                for f in migrated.rglob("*"):
                    if f.is_file():
                        f.chmod(0o644)

    def test_migration_creates_directory_structure(self, tmp_path, monkeypatch):
        """Migration creates graph/changesets/, codeindex/, logs/ subdirs."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "dir-struct")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        migrate_to_centralized(project)

        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        assert (centralized / "graph").exists()
        assert (centralized / "codeindex").exists()
        assert (centralized / "logs").exists()

    def test_codeindex_dst_replaced_if_exists(self, tmp_path, monkeypatch):
        """If codeindex/ already exists in centralized dir, it is replaced."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "idx-replace", with_codeindex=True)
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        # Pre-create a stale codeindex in the centralized location
        key = _sanitize_path_key(project)
        stale_idx = fake_home / "projects" / key / "codeindex"
        stale_idx.mkdir(parents=True)
        (stale_idx / "stale_file.bin").write_bytes(b"old data")

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

        # Stale file should be gone, new files present
        dst_idx = fake_home / "projects" / key / "codeindex"
        assert not (dst_idx / "stale_file.bin").exists()
        assert (dst_idx / "index.bin").exists()

    def test_migration_result_contains_paths(self, tmp_path, monkeypatch):
        """Result dict includes old_path and new_path strings."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "result-paths")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True
        assert "old_path" in result
        assert "new_path" in result
        assert str(project) in result["old_path"]
        assert str(fake_home) in result["new_path"]

    def test_migrate_with_empty_codeindex(self, tmp_path, monkeypatch):
        """Empty codeindex/ directory is still copied (as a directory)."""
        from mcp_server.migrate import migrate_to_centralized

        project = _create_legacy_project(tmp_path, "empty-idx")
        # Create empty codeindex dir
        (project / ".codevira" / "codeindex").mkdir(parents=True)
        _ = _setup_fake_home(tmp_path, monkeypatch)

        result = migrate_to_centralized(project)
        assert result["migrated"] is True

    def test_migrate_updates_global_db_registry(self, tmp_path, monkeypatch):
        """Migration registers project in global.db."""
        from mcp_server.migrate import migrate_to_centralized
        from indexer.global_db import GlobalDB

        project = _create_legacy_project(tmp_path, "gdb-reg")
        fake_home = _setup_fake_home(tmp_path, monkeypatch)

        # Provide a real global.db path
        monkeypatch.setattr(
            paths, "get_global_db_path", lambda: fake_home / "global.db"
        )

        with patch(
            "mcp_server.paths._get_git_remote_url",
            return_value="https://github.com/org/gdb-test.git",
        ):
            result = migrate_to_centralized(project)

        assert result["migrated"] is True

        # Verify registration via direct query
        gdb = GlobalDB(fake_home / "global.db")
        row = gdb.conn.execute(
            "SELECT path FROM projects WHERE git_remote = ?",
            ("https://github.com/org/gdb-test.git",),
        ).fetchone()
        gdb.close()
        assert row is not None
