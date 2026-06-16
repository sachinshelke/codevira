"""Tests for :mod:`mcp_server._ghost_check` — Bug 21c (rc.4 dogfood, 2026-05-13).

v3.4.0 fix (2026-06-15): doctor's ghost check used to roll its own crude
definition — "any dir missing config OR metadata is a ghost" — which counted
empty *stale* leftover dirs as ghosts. That made doctor disagree with
``codevira projects``: doctor reported "29 ghosts" while ``projects`` reported
"0 ghost · 29 stale" on the same machine. The check now delegates to the
canonical :mod:`mcp_server._project_inventory`, so the two surfaces agree by
construction.

Contract pinned here:

* No projects dir → PASS.
* Only empty/stale dirs → PASS (stale is harmless, NOT a ghost) + the stale
  count surfaced informationally.
* A dir with real state (graph / roadmap / config / metadata) but no global.db
  registration → ghost → WARN with count + actionable fix_command + slug names.
* Doctor's ghost count must EQUAL ``summarize(enumerate_projects())["ghost"]``
  — the two can never drift again.
* check_ghost_projects is wired into doctor's ``_CHECKS`` tuple.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from mcp_server._ghost_check import check_ghost_projects
from mcp_server._project_inventory import enumerate_projects, summarize
from mcp_server.doctor import _PASS, _WARN, _CHECKS


def _patch_home(monkeypatch, home):
    """Redirect both global-home and global-db resolution to an isolated dir."""
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )


class TestCheckGhostProjects:
    """Behaviour of the standalone check against the canonical inventory."""

    def test_no_projects_dir_passes(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path / ".codevira-empty")
        result = check_ghost_projects()
        assert result.state == _PASS
        assert "no ghost" in result.message.lower()

    def test_only_stale_dirs_pass(self, tmp_path, monkeypatch):
        """THE FIX: empty leftover dirs are *stale*, not ghosts → PASS.

        Pre-fix these tripped a false WARN ("29 ghosts") that disagreed with
        ``codevira projects`` ("0 ghost · 29 stale").
        """
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for i in range(5):
            (pdir / f"stale-{i}").mkdir()  # bare empty dir → stale
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _PASS, "empty dirs must not warn as ghosts"
        # The stale count is surfaced so the number isn't a surprise.
        assert "5 stale" in result.message

    def test_real_state_no_registration_is_ghost(self, tmp_path, monkeypatch):
        """A dir with graph + roadmap but no config/metadata/registration."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        ghost = pdir / "ghost-proj"
        (ghost / "graph").mkdir(parents=True)
        (ghost / "graph" / "graph.db").write_bytes(b"\x00")
        (ghost / "roadmap.yaml").write_text("project: g\n")
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _WARN
        assert "ghost-proj" in result.message
        assert result.fix_command  # must offer a fix

    def test_metadata_without_registration_is_ghost(self, tmp_path, monkeypatch):
        """metadata.json present but no global.db row → incomplete bookkeeping."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        ghost = pdir / "ghost-proj"
        ghost.mkdir()
        (ghost / "metadata.json").write_text("{}")
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _WARN
        assert "1" in result.message

    def test_truncates_to_3_in_message(self, tmp_path, monkeypatch):
        """Many ghosts → show first 3 + "(+N more)"."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for i in range(7):
            g = pdir / f"ghost-{i}"
            g.mkdir()
            (g / "metadata.json").write_text("{}")  # real state, unregistered
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _WARN
        assert "+4 more" in result.message

    def test_mixed_ghost_and_stale_counts_only_ghosts(self, tmp_path, monkeypatch):
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        # One ghost (real state, unregistered).
        g = pdir / "ghost-one"
        g.mkdir()
        (g / "metadata.json").write_text("{}")
        # Three stale (empty).
        for i in range(3):
            (pdir / f"stale-{i}").mkdir()
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _WARN
        assert "1 project dir(s) are ghosts" in result.message
        assert "ghost-one" in result.message


class TestDoctorAgreesWithInventory:
    """Doctor's ghost count must never drift from `codevira projects`."""

    @pytest.fixture
    def mixed_home(self, tmp_path, monkeypatch):
        """tracked-shaped (orphan) + ghost + stale — the canonical mix."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)

        # (A) Full bookkeeping but canonical_path absent on disk → orphan.
        a = pdir / "registered_aaaa"
        (a / "graph").mkdir(parents=True)
        (a / "graph" / "graph.db").write_bytes(b"\x00")
        (a / "config.yaml").write_text("project:\n  name: a\n")
        (a / "metadata.json").write_text(
            json.dumps({"original_path": "/Users/nobody/proj-a"})
        )
        # (B) Graph only, unregistered → ghost.
        b = pdir / "ghost_bbbb"
        (b / "graph").mkdir(parents=True)
        (b / "roadmap.yaml").write_text("project: b\n")
        # (C) Empty → stale.
        (pdir / "stale_cccc").mkdir()

        conn = sqlite3.connect(str(home / "global.db"))
        conn.execute(
            "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "language TEXT, git_remote TEXT, "
            "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO projects (path, name) VALUES (?, ?)",
            ("/Users/nobody/proj-a", "a"),
        )
        conn.commit()
        conn.close()
        _patch_home(monkeypatch, home)
        return home

    def test_doctor_count_equals_inventory_count(self, mixed_home):
        canonical = summarize(enumerate_projects())
        result = check_ghost_projects()

        if canonical["ghost"] == 0:
            assert result.state == _PASS
        else:
            assert result.state == _WARN
            assert f"{canonical['ghost']} project dir(s) are ghosts" in result.message

    def test_mix_has_exactly_one_ghost(self, mixed_home):
        """Sanity-check the fixture really is orphan + ghost + stale."""
        counts = summarize(enumerate_projects())
        assert counts["ghost"] == 1
        assert counts["stale"] == 1
        assert counts["orphan"] == 1


class TestCheckGhostProjectsWiredIntoDoctor:
    """The check must be registered in doctor.py's _CHECKS tuple."""

    def test_check_is_in_checks_tuple(self):
        """If anyone unregisters check_ghost_projects, fail loudly."""
        names = [c.__name__ for c in _CHECKS]
        assert "check_ghost_projects" in names
