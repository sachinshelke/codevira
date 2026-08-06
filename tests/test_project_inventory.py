"""Tests for :mod:`mcp_server._project_inventory` classification.

Slug-join fallback (2026-08, D00013I follow-up)
-----------------------------------------------
A data dir written by a code path that only touched ``graph/fixes.db`` (the
fix-history scan) has no ``metadata.json`` and no ``git_remote`` — the two keys
``enumerate_projects`` used to join a disk dir to its ``global.db`` row. On a
real machine 7 such dirs (AgentStore, LH, ToolsConnector, nexus-os, UDAP,
agent-mcp, AI-Business-Builder) were REGISTERED projects, yet each split into:

* a phantom ``tracked`` row (the db row, ``slug=None``, no disk dir), and
* a false ``stale`` row (the disk dir, no db match),

so ``codevira doctor`` / ``codevira projects`` surfaced them as "empty stale
leftovers" even though they carried real fix-history state.

The fix adds a third join key: a disk dir whose NAME equals
``_sanitize_path_key(registered_path)`` resolves to that registration. The dir
then classifies ``tracked`` (or ``orphan`` if the root is gone) — never a
removable leftover — and the phantom duplicate disappears.

Contract pinned here:

* A ``fixes.db``-only dir whose slug matches a registered path → ``tracked``,
  with the root present; ``orphan`` when the root is gone.
* No phantom duplicate: the canonical path appears in exactly one entry.
* A ``fixes.db``-only dir with NO matching registration stays ``stale``
  (unchanged) — we do not promote unregistered leftovers to ``ghost``, which
  would make ``codevira prune --ghosts`` delete their fix-history.
* Such a tracked dir is never a prune-removal candidate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_server._project_inventory import (
    empty_stale_dirs,
    enumerate_projects,
    summarize,
)
from mcp_server.paths import _sanitize_path_key


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )


def _make_global_db(home: Path, rows: list[tuple[str, str]]) -> None:
    """Create global.db with (path, name) project rows."""
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(home / "global.db"))
    conn.execute(
        "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "language TEXT, git_remote TEXT, "
        "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.executemany("INSERT INTO projects (path, name) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _fixes_db_only_dir(projects_dir: Path, slug: str) -> Path:
    """Create a ~/.codevira/projects/<slug>/ that holds ONLY graph/fixes.db —
    the exact shape a fix-history scan leaves behind (no metadata/config)."""
    d = projects_dir / slug
    (d / "graph").mkdir(parents=True)
    (d / "graph" / "fixes.db").write_bytes(b"\x00" * (16 * 1024))  # >10 KB
    return d


class TestSlugJoinFallback:
    def test_fixes_db_only_registered_dir_is_tracked_not_stale(
        self, tmp_path, monkeypatch
    ):
        """THE FIX: a metadata-less fixes.db dir whose slug matches a
        registered, on-disk project root classifies as ``tracked``."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)

        proj_root = tmp_path / "work" / "myproj"
        proj_root.mkdir(parents=True)  # root exists → tracked (not orphan)
        _make_global_db(home, [(str(proj_root), "myproj")])

        slug = _sanitize_path_key(str(proj_root))
        _fixes_db_only_dir(projects_dir, slug)
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        mine = [e for e in entries if e.slug == slug]
        assert len(mine) == 1, "the disk dir must produce exactly one entry"
        assert mine[0].status == "tracked"
        assert mine[0].canonical_path == str(proj_root)

        counts = summarize(entries)
        assert counts["stale"] == 0
        assert counts["tracked"] == 1

    def test_no_phantom_duplicate_row(self, tmp_path, monkeypatch):
        """Pre-fix the registered path appeared twice (phantom tracked +
        false stale). After the slug-join it must appear exactly once."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)

        proj_root = tmp_path / "work" / "dedupe"
        proj_root.mkdir(parents=True)
        _make_global_db(home, [(str(proj_root), "dedupe")])

        slug = _sanitize_path_key(str(proj_root))
        _fixes_db_only_dir(projects_dir, slug)
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        with_path = [e for e in entries if e.canonical_path == str(proj_root)]
        assert len(with_path) == 1, "registered path must not double-count"
        # Exactly one logical entry total (no leftover slug=None phantom).
        assert len(entries) == 1

    def test_registered_but_missing_root_is_orphan_not_stale(
        self, tmp_path, monkeypatch
    ):
        """If the slug-matched registration points at a root that no longer
        exists, the dir is an ``orphan`` (a deleted project), never a
        removable ``stale`` leftover."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)

        gone_root = tmp_path / "deleted" / "proj"  # never created on disk
        _make_global_db(home, [(str(gone_root), "proj")])

        slug = _sanitize_path_key(str(gone_root))
        _fixes_db_only_dir(projects_dir, slug)
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        mine = [e for e in entries if e.slug == slug]
        assert len(mine) == 1
        assert mine[0].status == "orphan"
        assert summarize(entries)["stale"] == 0

    def test_unregistered_fixes_db_dir_stays_stale(self, tmp_path, monkeypatch):
        """A fixes.db-only dir with NO matching registration is still
        ``stale`` — we must NOT promote it to ghost (that would let
        ``prune --ghosts`` delete real fix-history)."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)
        _make_global_db(home, [])  # empty registry

        # A slug that corresponds to no registered path.
        _fixes_db_only_dir(projects_dir, "Users_someone_orphaned_proj_deadbeef")
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        assert len(entries) == 1
        assert entries[0].status == "stale"
        # It carries real bytes (>10 KB) so it is NOT a prune-removal candidate.
        assert empty_stale_dirs(entries) == []

    def test_tracked_fixes_db_dir_is_never_a_prune_candidate(
        self, tmp_path, monkeypatch
    ):
        """The whole point: reclassifying to tracked keeps prune away from
        the fix-history data."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)

        proj_root = tmp_path / "work" / "keepme"
        proj_root.mkdir(parents=True)
        _make_global_db(home, [(str(proj_root), "keepme")])

        slug = _sanitize_path_key(str(proj_root))
        _fixes_db_only_dir(projects_dir, slug)
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        assert [e.slug for e in entries if e.status == "tracked"] == [slug]
        # Not a ghost, not an empty-stale — nothing for prune to remove.
        assert [e for e in entries if e.status == "ghost"] == []
        assert empty_stale_dirs(entries) == []
