"""Tests for :mod:`indexer._dedupe_migration` — Bug 20 (rc.4 dogfood, 2026-05-13).

The pre-fix codepath registered the same logical project under two different
``path`` values in ``global.db.projects``:

* ``cli.py:cmd_init`` + ``auto_init._register_global`` passed
  ``str(data_dir)`` (the ``~/.codevira/projects/<slug>`` storage path).
* ``global_sync.sync_to_global`` passed ``str(project_root)`` (the canonical
  project path).

These tests cover the one-shot migration that collapses pre-existing
duplicates so a user upgrading from rc.3 → rc.4 doesn't carry the corruption
forward.

Contract:
  * Multiple rows sharing the same ``git_remote`` collapse to one.
  * The non-storage (canonical project_root) row is kept when available.
  * Otherwise the most recently ``last_synced_at`` row is kept.
  * Rows with NULL or empty ``git_remote`` are NEVER deduped (we have no
    safe identity for git-less projects).
  * Idempotent: running on a clean DB does nothing.
"""
from __future__ import annotations

import sqlite3

import pytest

from indexer._dedupe_migration import dedupe_projects_by_git_remote


@pytest.fixture
def db():
    """Bare in-memory projects table — no GlobalDB schema baggage."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        " path TEXT PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " language TEXT,"
        " git_remote TEXT,"
        " last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.commit()
    return conn


class TestDedupeHappyPath:
    """The Bug 20 scenario: same git_remote, two paths, one survives."""

    def test_canonical_path_wins_over_storage_path(self, db, monkeypatch, tmp_path):
        """When one row is a storage path and one isn't, keep the non-storage row."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        storage = str(tmp_path / ".codevira" / "projects" / "proj_abc123")
        canonical = "/Users/sachin/Documents/Projects/proj"
        db.executemany(
            "INSERT INTO projects (path, name, git_remote, last_synced_at) VALUES (?, ?, ?, ?)",
            [
                (storage, "proj", "git@host:proj.git", "2026-05-10 12:00:00"),
                (canonical, "proj", "git@host:proj.git", "2026-05-13 12:00:00"),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 1
        rows = db.execute("SELECT path FROM projects").fetchall()
        assert len(rows) == 1
        assert rows[0]["path"] == canonical, (
            "Should keep the canonical (non-storage) path, not the storage path."
        )

    def test_most_recent_wins_when_both_storage(self, db, monkeypatch, tmp_path):
        """If both rows are storage paths (rc.3-only data), keep the most recent."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        old = str(tmp_path / ".codevira" / "projects" / "proj_old")
        new = str(tmp_path / ".codevira" / "projects" / "proj_new")
        db.executemany(
            "INSERT INTO projects (path, name, git_remote, last_synced_at) VALUES (?, ?, ?, ?)",
            [
                (old, "proj", "git@host:proj.git", "2026-05-01 00:00:00"),
                (new, "proj", "git@host:proj.git", "2026-05-13 00:00:00"),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 1
        rows = db.execute("SELECT path FROM projects").fetchall()
        assert rows[0]["path"] == new

    def test_three_way_collapse(self, db, monkeypatch, tmp_path):
        """Three duplicate rows collapse to one canonical row."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        canonical = "/Users/sachin/Documents/Projects/proj"
        storage_a = str(tmp_path / ".codevira" / "projects" / "proj_aaa")
        storage_b = str(tmp_path / ".codevira" / "projects" / "proj_bbb")
        db.executemany(
            "INSERT INTO projects (path, name, git_remote, last_synced_at) VALUES (?, ?, ?, ?)",
            [
                (storage_a, "proj", "git@host:proj.git", "2026-05-10 00:00:00"),
                (storage_b, "proj", "git@host:proj.git", "2026-05-11 00:00:00"),
                (canonical, "proj", "git@host:proj.git", "2026-05-12 00:00:00"),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 2
        assert db.execute("SELECT path FROM projects").fetchone()["path"] == canonical


class TestDedupeSafety:
    """Edge cases that must NOT touch data."""

    def test_no_duplicates_is_no_op(self, db, monkeypatch, tmp_path):
        """Idempotent — running on a clean DB returns 0 and changes nothing."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        db.executemany(
            "INSERT INTO projects (path, name, git_remote) VALUES (?, ?, ?)",
            [
                ("/Users/sachin/proj-a", "proj-a", "git@host:a.git"),
                ("/Users/sachin/proj-b", "proj-b", "git@host:b.git"),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 0
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2

    def test_null_git_remote_rows_are_left_alone(self, db, monkeypatch, tmp_path):
        """Two rows with NULL git_remote and same path don't dedupe — we
        have no safe identity for git-less projects.
        """
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        db.executemany(
            "INSERT INTO projects (path, name, git_remote) VALUES (?, ?, ?)",
            [
                ("/Users/sachin/no-git-a", "no-git-a", None),
                ("/Users/sachin/no-git-b", "no-git-b", None),
                ("/Users/sachin/no-git-c", "no-git-c", ""),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 0
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 3

    def test_idempotent_second_run(self, db, monkeypatch, tmp_path):
        """After one run, a second run does nothing."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        db.executemany(
            "INSERT INTO projects (path, name, git_remote, last_synced_at) VALUES (?, ?, ?, ?)",
            [
                (str(tmp_path / ".codevira" / "projects" / "storage"), "p", "g", "2026-05-01"),
                ("/Users/sachin/canonical", "p", "g", "2026-05-13"),
            ],
        )
        db.commit()

        first = dedupe_projects_by_git_remote(db)
        second = dedupe_projects_by_git_remote(db)

        assert first == 1
        assert second == 0  # nothing left to dedupe

    def test_different_git_remotes_never_collapse(self, db, monkeypatch, tmp_path):
        """Rows with different git_remote values are independent — never collapse."""
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira",
        )
        db.executemany(
            "INSERT INTO projects (path, name, git_remote) VALUES (?, ?, ?)",
            [
                ("/Users/sachin/proj-a", "proj-a", "git@host:a.git"),
                ("/Users/sachin/proj-b", "proj-b", "git@host:b.git"),
                ("/Users/sachin/proj-c", "proj-c", "git@host:c.git"),
            ],
        )
        db.commit()

        deleted = dedupe_projects_by_git_remote(db)

        assert deleted == 0
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 3


class TestDedupeIntegrationViaInit:
    """The migration runs from :meth:`GlobalDB.__init__` — verify the wiring."""

    def test_global_db_init_runs_dedupe(self, tmp_path):
        """Opening a GlobalDB on a corrupted (Bug 20) DB collapses duplicates."""
        # Pre-seed a Bug-20-shaped DB.
        db_path = tmp_path / "global.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "language TEXT, git_remote TEXT, "
            "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO projects (path, name, git_remote, last_synced_at) VALUES (?, ?, ?, ?)",
            [
                ("/Users/sachin/.codevira/projects/proj_abc", "proj", "g", "2026-05-01"),
                ("/Users/sachin/Documents/Projects/proj", "proj", "g", "2026-05-13"),
            ],
        )
        conn.commit()
        conn.close()

        # Opening a GlobalDB on this file should auto-collapse the duplicates.
        from indexer.global_db import GlobalDB
        gdb = GlobalDB(db_path)
        try:
            # Count the cleaned-up rows directly.
            n = gdb.conn.execute(
                "SELECT COUNT(*) FROM projects WHERE git_remote = 'g'"
            ).fetchone()[0]
        finally:
            gdb.close()
        assert n == 1, "GlobalDB.__init__ should have run the dedupe migration."
