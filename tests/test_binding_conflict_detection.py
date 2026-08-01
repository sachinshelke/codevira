"""The wrong-project-binding guard must see the entries the fixer writes.

`check_claude_binding_conflict` warns when a BARE global `codevira` entry in
~/.claude.json coexists with project-scoped ones — the bare entry out-ranks
them, so a session binds to a guessed project and reads another project's
memory.

It was reporting "no conflicting registrations" on a machine where exactly
that conflict existed. `claude_scoped_entries()` tested `"codevira" in
servers`, an EXACT key match, while `register-all` names entries after the
project (`codevira-agent-mcp`, `codevira-udap`, …) so there is one MCP per
project. The exact match found zero, the check needs BOTH halves to fire, so
it never fired. The detector was blind to the entries the fixer creates.

Observed live, which is how it was found: a session working in `agent-mcp`
wrote ten decisions and thirteen session logs into `Agentic/LH`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server import ide_inject
from mcp_server.doctor import _PASS


def _write_claude_json(tmp_path: Path, data: dict, monkeypatch) -> Path:
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps(data))
    monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: cfg)
    return cfg


BARE = {"type": "stdio", "command": "/usr/local/bin/codevira", "args": []}


def _scoped(path: str) -> dict:
    return {"type": "stdio", "command": "codevira", "args": ["--project-dir", path]}


class TestItSeesSuffixedEntries:
    """The regression. register-all writes `codevira-<project>`."""

    def test_a_suffixed_scoped_entry_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_claude_json(
            tmp_path,
            {
                "mcpServers": {"codevira": BARE},
                "projects": {
                    "/p/agent-mcp": {
                        "mcpServers": {"codevira-agent-mcp": _scoped("/p/agent-mcp")}
                    }
                },
            },
            monkeypatch,
        )
        assert ide_inject.claude_scoped_entries() == ["/p/agent-mcp"]

    def test_the_conflict_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both halves together — this is what silently passed."""
        _write_claude_json(
            tmp_path,
            {
                "mcpServers": {"codevira": BARE},
                "projects": {
                    "/p/a": {"mcpServers": {"codevira-a": _scoped("/p/a")}},
                    "/p/b": {"mcpServers": {"codevira-b": _scoped("/p/b")}},
                },
            },
            monkeypatch,
        )
        from mcp_server.doctor import check_claude_binding_conflict

        result = check_claude_binding_conflict()
        assert result.state != _PASS, "the guard reported clean on a real conflict"
        assert "bare" in result.message.lower()
        assert result.fix_command and "doctor --fix" in result.fix_command

    def test_an_unsuffixed_scoped_entry_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-register-all installs used the bare key at project scope.
        Fixing the suffixed case must not break the original one."""
        _write_claude_json(
            tmp_path,
            {
                "mcpServers": {"codevira": BARE},
                "projects": {"/p/a": {"mcpServers": {"codevira": _scoped("/p/a")}}},
            },
            monkeypatch,
        )
        assert ide_inject.claude_scoped_entries() == ["/p/a"]


class TestItDoesNotOverReach:
    def test_no_scoped_entries_is_not_a_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare entry ALONE is the normal single-project setup and must
        not be reported — warning there would train people to ignore it."""
        _write_claude_json(tmp_path, {"mcpServers": {"codevira": BARE}}, monkeypatch)
        from mcp_server.doctor import check_claude_binding_conflict

        assert check_claude_binding_conflict().state == _PASS

    def test_scoped_without_a_bare_entry_is_not_a_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The healthy post-register-all state."""
        _write_claude_json(
            tmp_path,
            {"projects": {"/p/a": {"mcpServers": {"codevira-a": _scoped("/p/a")}}}},
            monkeypatch,
        )
        from mcp_server.doctor import check_claude_binding_conflict

        assert check_claude_binding_conflict().state == _PASS

    def test_an_unrelated_server_is_not_mistaken_for_codevira(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prefix matching must not swallow a different tool's entry."""
        _write_claude_json(
            tmp_path,
            {
                "mcpServers": {"codevira": BARE},
                "projects": {
                    "/p/a": {"mcpServers": {"codeviracompetitor": _scoped("/p/a")}}
                },
            },
            monkeypatch,
        )
        assert ide_inject.claude_scoped_entries() == []

    @pytest.mark.parametrize("bad", [None, "str", 7, [], {}])
    def test_garbage_does_not_raise(self, bad) -> None:
        assert ide_inject.has_codevira_server(bad) is False


class TestTheHelperMatchesTheRemover:
    def test_prefix_rule_is_shared(self) -> None:
        """The bug was two copies of one rule drifting apart: the remover
        used `k == prefix or k.startswith(prefix + "-")`, the detectors
        used exact equality."""
        assert ide_inject.has_codevira_server({"codevira": {}})
        assert ide_inject.has_codevira_server({"codevira-agent-mcp": {}})
        assert not ide_inject.has_codevira_server({"codeviraX": {}})
        assert not ide_inject.has_codevira_server({"other": {}})
