"""
test_claude_binding_conflict.py — v3.7.1 (D00011V): a BARE global Claude Code
entry must never coexist with project-scoped ones.

Claude Code reads MCP servers from ~/.claude.json's top-level ``mcpServers``
(user scope) AND ``projects.<path>.mcpServers`` (project scope). A BARE global
entry — ``codevira`` with no ``--project-dir`` — out-ranks the scoped ones, so
the session binds to a guessed project instead of the one you opened.

Observed live: an LH session returned UDAP's memory. Worse, deleting the bare
entry by hand did not stick, because every subsequent init re-added it.

Guarantees pinned here:
  1. writing a project-scoped entry displaces the bare global one,
  2. global registration is skipped when scoped entries already exist,
  3. a global entry that pins --project-dir is deliberate and left alone,
  4. doctor reports the conflict.
"""

from __future__ import annotations

import json

import pytest

from mcp_server import ide_inject


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """Point ~/.claude.json at a temp file."""
    cfg = tmp_path / ".claude.json"
    monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: cfg)
    return cfg


def _write(cfg, *, bare=False, pinned=False, scoped=()):
    data: dict = {"mcpServers": {}, "projects": {}}
    if bare:
        data["mcpServers"]["codevira"] = {"command": "/bin/codevira", "args": []}
    if pinned:
        data["mcpServers"]["codevira"] = {
            "command": "/bin/codevira",
            "args": ["--project-dir", "/some/proj"],
        }
    for p in scoped:
        data["projects"][p] = {
            "mcpServers": {
                "codevira": {"command": "/bin/codevira", "args": ["--project-dir", p]}
            }
        }
    cfg.write_text(json.dumps(data))


class TestDetection:
    def test_detects_bare_global(self, fake_claude_home):
        _write(fake_claude_home, bare=True)
        assert ide_inject.bare_global_claude_entry() is not None

    def test_pinned_global_is_not_bare(self, fake_claude_home):
        """A global entry WITH --project-dir is a deliberate pin, not the bug."""
        _write(fake_claude_home, pinned=True)
        assert ide_inject.bare_global_claude_entry() is None

    def test_lists_scoped_projects(self, fake_claude_home):
        _write(fake_claude_home, scoped=("/a", "/b"))
        assert sorted(ide_inject.claude_scoped_entries()) == ["/a", "/b"]


class TestRemoval:
    def test_removes_bare_entry_only(self, fake_claude_home):
        _write(fake_claude_home, bare=True, scoped=("/a",))
        removed = ide_inject.remove_bare_global_claude_entry()
        assert removed is not None

        data = json.loads(fake_claude_home.read_text())
        assert "codevira" not in data["mcpServers"]  # bare gone
        assert "codevira" in data["projects"]["/a"]["mcpServers"]  # scoped kept

    def test_never_removes_a_pinned_global(self, fake_claude_home):
        _write(fake_claude_home, pinned=True)
        assert ide_inject.remove_bare_global_claude_entry() is None
        data = json.loads(fake_claude_home.read_text())
        assert "codevira" in data["mcpServers"]

    def test_noop_when_nothing_to_remove(self, fake_claude_home):
        _write(fake_claude_home)
        assert ide_inject.remove_bare_global_claude_entry() is None


class TestGlobalInjectionSkipsWhenScopedExists:
    def test_does_not_readd_bare_entry_over_this_projects_scoped_entry(
        self, fake_claude_home, monkeypatch, tmp_path
    ):
        """THE recurrence bug: init re-added the bare entry the user deleted.

        The check is PER PROJECT — see TestGuardIsPerProject for why asking
        "does ANY project have one?" was itself a bug.
        """
        me = tmp_path / "me"
        me.mkdir()
        _write(fake_claude_home, scoped=(str(me), "/b"))
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        ide_inject.inject_global_claude_code("/bin/codevira", "/bin/python", me)

        data = json.loads(fake_claude_home.read_text())
        assert "codevira" not in data.get(
            "mcpServers", {}
        ), "bare global entry was re-added despite this project having one"

    def test_registers_when_called_without_project_context(
        self, fake_claude_home, monkeypatch
    ):
        """With no project to reason about we cannot judge whether a bare entry
        is harmful, so we must register rather than silently do nothing."""
        _write(fake_claude_home, scoped=("/a",))
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        path = ide_inject.inject_global_claude_code("/bin/codevira", "/bin/python")

        assert path is not None
        assert "codevira" in json.loads(fake_claude_home.read_text())["mcpServers"]

    def test_still_registers_globally_when_no_scoped_entries(
        self, fake_claude_home, monkeypatch
    ):
        """Users with ONLY a global registration keep working — a bare entry is
        fine when it is the sole registration."""
        _write(fake_claude_home)
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        ide_inject.inject_global_claude_code("/bin/codevira", "/bin/python")

        data = json.loads(fake_claude_home.read_text())
        assert "codevira" in data.get("mcpServers", {})


class TestDoctorCheck:
    def test_warns_on_conflict(self, fake_claude_home):
        from mcp_server.doctor import _WARN, check_claude_binding_conflict

        _write(fake_claude_home, bare=True, scoped=("/a",))
        r = check_claude_binding_conflict()
        assert r.state == _WARN
        assert "bare" in r.message.lower()

    def test_passes_when_only_scoped(self, fake_claude_home):
        from mcp_server.doctor import _PASS, check_claude_binding_conflict

        _write(fake_claude_home, scoped=("/a",))
        assert check_claude_binding_conflict().state == _PASS

    def test_passes_when_only_bare(self, fake_claude_home):
        from mcp_server.doctor import _PASS, check_claude_binding_conflict

        _write(fake_claude_home, bare=True)
        assert check_claude_binding_conflict().state == _PASS


class TestGuardIsPerProject:
    """v3.7.1 correction: the skip-guard must ask 'does THIS project have a
    scoped entry?'. Asking 'does ANY project?' let an unrelated project's entry
    suppress registration here — and combined with heal_stale_registration
    deleting this project's own .mcp.json entry, init could leave a project with
    NO registration at all while still printing success."""

    def test_unrelated_scoped_entry_does_not_suppress_registration(
        self, fake_claude_home, monkeypatch, tmp_path
    ):
        _write(fake_claude_home, scoped=("/some/other/project",))
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)
        me = tmp_path / "myproject"
        me.mkdir()

        path = ide_inject.inject_global_claude_code("/bin/codevira", "/bin/py", me)

        assert path is not None, "registration was suppressed by another project"
        data = json.loads(fake_claude_home.read_text())
        assert "codevira" in data["mcpServers"]

    def test_returns_none_when_nothing_written(
        self, fake_claude_home, monkeypatch, tmp_path
    ):
        """Callers print success from the return value — it must not lie."""
        me = tmp_path / "myproject"
        me.mkdir()
        _write(fake_claude_home, scoped=(str(me),))
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        path = ide_inject.inject_global_claude_code("/bin/codevira", "/bin/py", me)

        assert path is None, "reported success without writing anything"
        data = json.loads(fake_claude_home.read_text())
        assert "codevira" not in data.get("mcpServers", {})

    def test_sees_scoped_entry_in_mcp_json_surface(self, fake_claude_home, tmp_path):
        """_inject_claude writes <project>/.mcp.json — the guard was blind to it."""
        me = tmp_path / "myproject"
        me.mkdir()
        (me / ".mcp.json").write_text(json.dumps({"mcpServers": {"codevira": {}}}))
        _write(fake_claude_home)

        assert ide_inject.project_has_scoped_claude_entry(me) is True


class TestNoCollateralRemoval:
    def test_inject_claude_does_not_delete_the_bare_global(
        self, fake_claude_home, tmp_path
    ):
        """The bare global serves every OTHER project; removing it while
        initializing one project un-registers codevira everywhere else."""
        _write(fake_claude_home, bare=True)
        proj = tmp_path / "proj"
        proj.mkdir()

        ide_inject._inject_claude(proj, "/bin/codevira", "/bin/py")

        data = json.loads(fake_claude_home.read_text())
        assert (
            "codevira" in data["mcpServers"]
        ), "init orphaned every other project by deleting the bare global entry"


class TestCorruptConfigIsNeverClobbered:
    def test_refuses_to_overwrite_unparseable_config(self, tmp_path):
        """~/.claude.json holds oauth, project history and other MCP servers.
        A transient parse failure must never cause it to be replaced."""
        cfg = tmp_path / ".claude.json"
        cfg.write_text('{"mcpServers": {"other": {}}, "oauthAccount": {  BROKEN')
        before = cfg.read_text()

        with pytest.raises(ide_inject.ConfigUnreadableError):
            ide_inject._write_json_safe(cfg, {"mcpServers": {"codevira": {}}})

        assert cfg.read_text() == before, "corrupt config was overwritten"

    def test_still_writes_when_file_missing_or_empty(self, tmp_path):
        missing = tmp_path / "new.json"
        ide_inject._write_json_safe(missing, {"a": 1})
        assert json.loads(missing.read_text()) == {"a": 1}

        empty = tmp_path / "empty.json"
        empty.write_text("   ")
        ide_inject._write_json_safe(empty, {"b": 2})
        assert json.loads(empty.read_text()) == {"b": 2}
