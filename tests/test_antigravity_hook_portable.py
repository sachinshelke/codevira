"""
test_antigravity_hook_portable.py — v3.7.1 (D00011Y): the Antigravity
enforcement hook command must be portable.

`.agents/hooks.json` lives inside the repo, so a team may commit it. Embedding an
absolute path (`/Users/alice/.local/bin/codevira`) means every teammate — and
the same user on another OS — inherits a hook pointing at one machine's home
directory, silently breaking enforcement. When `codevira` is on PATH the command
must be the bare, portable form.
"""

from __future__ import annotations

import json

import pytest

from mcp_server import ide_inject


def _cmd(proj):
    hooks = json.loads((proj / ".agents" / "hooks.json").read_text())
    return hooks["codevira-enforcement"]["PreToolUse"][0]["hooks"][0]["command"]


class TestPortableCommand:
    def test_bare_command_when_on_path(self, tmp_path, monkeypatch):
        """On PATH -> `codevira ...`, no machine-specific absolute path."""
        monkeypatch.setattr(ide_inject.shutil, "which", lambda name: "/anything")
        proj = tmp_path / "proj"
        proj.mkdir()

        ide_inject.install_antigravity_enforcement_hook(
            proj, "/Users/alice/.local/bin/codevira"
        )
        cmd = _cmd(proj)

        assert cmd.startswith("codevira ")
        assert "/Users/alice" not in cmd, "absolute home path leaked into a repo file"
        assert cmd == "codevira engine handle --ide antigravity PreToolUse"

    def test_absolute_fallback_when_not_on_path(self, tmp_path, monkeypatch):
        """Not on PATH -> quoted absolute path so it at least works locally."""
        monkeypatch.setattr(ide_inject.shutil, "which", lambda name: None)
        proj = tmp_path / "proj"
        proj.mkdir()

        ide_inject.install_antigravity_enforcement_hook(
            proj, "/opt/tools/codevira bin/codevira"
        )
        cmd = _cmd(proj)

        assert (
            cmd
            == '"/opt/tools/codevira bin/codevira" engine handle --ide antigravity PreToolUse'
        )

    def test_python_fallback_invokes_module(self, tmp_path, monkeypatch):
        """A Python-interpreter cmd_path must run `-m mcp_server`, not treat the
        interpreter as the codevira binary."""
        monkeypatch.setattr(ide_inject.shutil, "which", lambda name: None)
        proj = tmp_path / "proj"
        proj.mkdir()

        ide_inject.install_antigravity_enforcement_hook(proj, "/usr/bin/python3.13")
        cmd = _cmd(proj)

        assert "-m mcp_server" in cmd
        assert cmd.startswith('"/usr/bin/python3.13" -m mcp_server engine handle')

    @pytest.mark.parametrize("winpath", ["C:\\Python\\python.exe", "python.exe"])
    def test_windows_python_names(self, tmp_path, monkeypatch, winpath):
        monkeypatch.setattr(ide_inject.shutil, "which", lambda name: None)
        assert "-m mcp_server" in ide_inject._portable_hook_command(winpath)

    def test_command_still_targets_the_engine(self, tmp_path, monkeypatch):
        """Whatever the form, it must still route to the enforcement engine."""
        for which_ret in ("/x", None):
            monkeypatch.setattr(ide_inject.shutil, "which", lambda name: which_ret)
            cmd = ide_inject._portable_hook_command("/bin/codevira")
            assert "engine handle" in cmd
            assert "--ide antigravity" in cmd
            assert cmd.rstrip().endswith("PreToolUse")
