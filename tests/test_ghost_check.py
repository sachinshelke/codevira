"""Tests for :mod:`mcp_server._ghost_check` — Bug 21c (rc.4 dogfood, 2026-05-13).

Ghost dirs are ``~/.codevira/projects/<slug>/`` directories that exist on
disk but are missing ``config.yaml`` and/or ``metadata.json`` — leftovers
from pre-rc.4 installs where the daemon ``_run_background_init`` thread
died mid-flight (Bug 21a fixed the cause; this check surfaces the legacy
state).

Contract pinned here:

* No projects dir → PASS ("first run").
* All complete projects → PASS with count.
* Any ghosts → WARN with count + actionable fix_command + first 3 slug names.
* check_ghost_projects is wired into doctor's ``_CHECKS`` tuple.
"""
from __future__ import annotations

import json

from mcp_server._ghost_check import check_ghost_projects
from mcp_server.doctor import _PASS, _WARN, _CHECKS


class TestCheckGhostProjects:
    """Behaviour of the standalone check."""

    def test_no_projects_dir_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: tmp_path / ".codevira-empty",
        )
        result = check_ghost_projects()
        assert result.state == _PASS
        assert "no projects directory" in result.message.lower() or \
               "first run" in result.message.lower() or \
               "no tracked projects" in result.message.lower()

    def test_all_complete_passes(self, tmp_path, monkeypatch):
        """Every dir has config + metadata → PASS with count."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for name in ("proj-a", "proj-b"):
            d = pdir / name
            d.mkdir()
            (d / "config.yaml").write_text("project:\n  name: " + name + "\n")
            (d / "metadata.json").write_text(json.dumps({"original_path": str(d)}))
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        result = check_ghost_projects()
        assert result.state == _PASS
        assert "2" in result.message

    def test_missing_config_is_ghost(self, tmp_path, monkeypatch):
        """A dir with metadata but no config is a ghost."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        ghost = pdir / "ghost-proj"
        ghost.mkdir()
        (ghost / "metadata.json").write_text("{}")
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        result = check_ghost_projects()
        assert result.state == _WARN
        assert "ghost-proj" in result.message
        assert result.fix_command  # must offer a fix

    def test_missing_metadata_is_ghost(self, tmp_path, monkeypatch):
        """A dir with config but no metadata is a ghost."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        ghost = pdir / "ghost-proj"
        ghost.mkdir()
        (ghost / "config.yaml").write_text("project:\n  name: g\n")
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        result = check_ghost_projects()
        assert result.state == _WARN
        assert "1" in result.message

    def test_truncates_to_3_in_message(self, tmp_path, monkeypatch):
        """Many ghosts → show first 3 + "(+N more)"."""
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        for i in range(7):
            (pdir / f"ghost-{i}").mkdir()  # bare ghost
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        result = check_ghost_projects()
        assert result.state == _WARN
        # Three names shown explicitly, rest summarized.
        assert "+4 more" in result.message

    def test_mixed_complete_and_ghost(self, tmp_path, monkeypatch):
        home = tmp_path / ".codevira"
        pdir = home / "projects"
        pdir.mkdir(parents=True)
        # Complete project.
        complete = pdir / "complete-one"
        complete.mkdir()
        (complete / "config.yaml").write_text("project:\n  name: a\n")
        (complete / "metadata.json").write_text("{}")
        # Ghost.
        (pdir / "ghost-one").mkdir()
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        result = check_ghost_projects()
        assert result.state == _WARN
        assert "1 of 2" in result.message


class TestCheckGhostProjectsWiredIntoDoctor:
    """The check must be registered in doctor.py's _CHECKS tuple."""

    def test_check_is_in_checks_tuple(self):
        """If anyone unregisters check_ghost_projects, fail loudly."""
        names = [c.__name__ for c in _CHECKS]
        assert "check_ghost_projects" in names
