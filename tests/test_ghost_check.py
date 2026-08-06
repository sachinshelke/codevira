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
from mcp_server._project_inventory import (
    empty_stale_dirs,
    enumerate_projects,
    summarize,
)
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


class TestStaleHintPointsAtPrune:
    """Regression (v4.0.x): the doctor stale-leftover hint must name the tidy
    command ``prune``, not the deprecated full-uninstall ``clean`` (D00012X),
    and its stale count must equal what ``codevira prune`` actually removes.

    The v4.0.0 bug: on a real machine ``codevira doctor`` printed
    "8 stale dir(s) — empty leftovers; `codevira clean` tidies them" while
    ``codevira prune --dry-run`` reported "No ghost projects or empty data
    dirs" — because each of those 8 dirs held a >10 KB ``graph/fixes.db``
    fix-history shell that prune deliberately skips. Following the hint would
    have run the uninstaller and removed nothing it advertised.
    """

    def test_stale_hint_names_prune_not_clean(self, tmp_path, monkeypatch):
        """Removable (≤10 KB) stale dirs are surfaced pointing at ``prune``."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for i in range(3):
            (pdir / f"stale-{i}").mkdir()  # bare empty dir → removable stale
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _PASS
        assert "3 stale" in result.message
        assert "codevira prune" in result.message
        assert "codevira clean" not in result.message

    def test_ghost_fix_command_names_prune_not_clean(self, tmp_path, monkeypatch):
        """The ghost WARN's fix_command must also point at ``prune``."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        g = pdir / "ghost-proj"
        g.mkdir()
        (g / "metadata.json").write_text("{}")  # real state, unregistered
        _patch_home(monkeypatch, home)

        result = check_ghost_projects()
        assert result.state == _WARN
        assert result.fix_command
        assert "prune" in result.fix_command
        assert "clean" not in result.fix_command

    def test_fix_history_shell_is_not_counted_as_removable_stale(
        self, tmp_path, monkeypatch
    ):
        """THE REPRO: a >10 KB stale dir (fix-history shell) is NOT counted.

        Doctor must not advertise a tidy that removes nothing — a stale dir
        prune skips must not appear in doctor's stale count.
        """
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        # One removable empty stale dir (≤10 KB, bare).
        (pdir / "empty-stale").mkdir()
        # One NON-removable stale dir: graph/fixes.db only (no graph.db), and
        # deliberately >10 KB so prune's size guard skips it. This is exactly
        # the ~/.codevira/projects/ shape that produced the 4.0.0 mismatch.
        fixhist = pdir / "fixhist-stale"
        (fixhist / "graph").mkdir(parents=True)
        (fixhist / "graph" / "fixes.db").write_bytes(b"\x00" * (11 * 1024))
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        # Sanity: the inventory sees BOTH as 'stale' ...
        assert summarize(entries)["stale"] == 2
        # ... but only the empty one is prune-removable.
        assert len(empty_stale_dirs(entries)) == 1

        result = check_ghost_projects()
        assert result.state == _PASS
        # Doctor surfaces ONLY the 1 removable dir, never the raw stale=2.
        assert "1 stale" in result.message
        assert "2 stale" not in result.message

    def test_doctor_stale_count_equals_what_prune_would_remove(
        self, tmp_path, monkeypatch, capsys
    ):
        """End-to-end: doctor's surfaced stale count == what ``prune`` removes.

        Builds the exact reconciled repro — only non-removable fix-history
        shells present — then asserts doctor reports zero stale AND prune's
        dry-run confirms there is nothing to remove.
        """
        from mcp_server.cli import _cmd_clean_ghosts

        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for i in range(3):
            fixhist = pdir / f"fixhist-{i}"
            (fixhist / "graph").mkdir(parents=True)
            (fixhist / "graph" / "fixes.db").write_bytes(b"\x00" * (16 * 1024))
        _patch_home(monkeypatch, home)

        entries = enumerate_projects()
        # Raw inventory says 3 stale; the removable set is empty.
        assert summarize(entries)["stale"] == 3
        removable = len(empty_stale_dirs(entries))
        assert removable == 0

        # Doctor: no stale mention at all (nothing prune can tidy).
        result = check_ghost_projects()
        assert result.state == _PASS
        assert "stale dir(s)" not in result.message

        # prune (ghost mode) dry-run agrees: nothing to remove.
        _cmd_clean_ghosts(dry_run=True)
        prune_out = capsys.readouterr().out
        assert "No ghost projects or empty data dirs" in prune_out
