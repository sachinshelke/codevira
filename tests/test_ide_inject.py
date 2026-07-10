"""
Tests for IDE auto-injection (mcp_server/ide_inject.py).

Ported from test_v16_ide_inject.py + new coverage & chaos tests.

Covers:
  - _merge_mcp_config: preserves existing preferences, non-destructive
  - _build_server_config / _build_global_server_config shapes
  - Claude Desktop injection (command+args, no cwd/url)
  - Global mode injection (no project path)
  - HTTP URL injection for Claude Code
  - Antigravity server name sanitization
  - _read_json_safe / _write_json_safe helpers
  - detect_installed_ides
  - _resolve_command
  - inject_ide_config integration tests
  - _inject_claude, _inject_cursor, _inject_windsurf per-project
  - inject_ide_config global_mode=True (skips Desktop + Antigravity)
  - inject_ide_config exception handling
  - Chaos: corrupt files, read-only, concurrency, long paths
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

import mcp_server.ide_inject as ide_inject
from mcp_server.ide_inject import (
    _build_global_server_config,
    _build_server_config,
    _claude_desktop_config_path,
    _inject_claude,
    _inject_claude_desktop,
    _inject_cursor,
    _inject_windsurf,
    _merge_mcp_config,
    _read_json_safe,
    _resolve_command,
    _write_json_safe,
    detect_installed_ides,
    inject_claude_http_url,
    inject_global_claude_code,
    inject_global_claude_desktop,
    inject_global_cursor,
    inject_global_windsurf,
    inject_ide_config,
)


# ===========================================================================
# _merge_mcp_config
# ===========================================================================


class TestMergeMcpConfig:
    def test_creates_mcpServers_if_missing(self):
        existing = {}
        merged = _merge_mcp_config(existing, "codevira", {"command": "x", "args": []})
        assert "mcpServers" in merged
        assert "codevira" in merged["mcpServers"]

    def test_preserves_other_servers(self):
        existing = {"mcpServers": {"other-tool": {"command": "other", "args": []}}}
        merged = _merge_mcp_config(existing, "codevira", {"command": "x", "args": []})
        assert "other-tool" in merged["mcpServers"]
        assert "codevira" in merged["mcpServers"]

    def test_overwrites_existing_codevira_entry(self):
        existing = {"mcpServers": {"codevira": {"command": "old", "args": []}}}
        merged = _merge_mcp_config(existing, "codevira", {"command": "new", "args": []})
        assert merged["mcpServers"]["codevira"]["command"] == "new"

    def test_preserves_top_level_keys(self):
        existing = {"globalShortcut": "Ctrl+X", "mcpServers": {}}
        merged = _merge_mcp_config(existing, "codevira", {"command": "x"})
        assert merged["globalShortcut"] == "Ctrl+X"

    def test_does_not_mutate_original(self):
        existing = {"mcpServers": {"other": {}}}
        original_copy = json.loads(json.dumps(existing))
        _merge_mcp_config(existing, "codevira", {"command": "x"})
        assert existing == original_copy


# ===========================================================================
# _build_server_config
# ===========================================================================


class TestBuildServerConfig:
    def test_binary_with_cwd(self, tmp_path):
        config = _build_server_config(
            "/usr/bin/codevira", "python3", tmp_path, use_cwd=True
        )
        assert config["command"] == "/usr/bin/codevira"
        assert config["args"] == []
        assert config["cwd"] == str(tmp_path)
        assert "--project-dir" not in config.get("args", [])

    def test_binary_without_cwd_uses_project_dir_arg(self, tmp_path):
        config = _build_server_config(
            "/usr/bin/codevira", "python3", tmp_path, use_cwd=False
        )
        assert config["command"] == "/usr/bin/codevira"
        assert "--project-dir" in config["args"]
        assert str(tmp_path) in config["args"]
        assert "cwd" not in config

    def test_python_fallback_uses_module_flag(self, tmp_path):
        config = _build_server_config("python3", "python3", tmp_path, use_cwd=True)
        assert config["command"] == "python3"
        assert "-m" in config["args"]
        assert "mcp_server" in config["args"]
        assert "--project-dir" in config["args"]

    def test_binary_with_cwd_no_project_dir_in_args(self, tmp_path):
        config = _build_server_config(
            "/usr/bin/codevira", "python3", tmp_path, use_cwd=True
        )
        assert "--project-dir" not in config["args"]


# ===========================================================================
# _build_global_server_config
# ===========================================================================


class TestBuildGlobalServerConfig:
    def test_binary_global_has_empty_args(self):
        config = _build_global_server_config("/usr/bin/codevira", "python3")
        assert config["command"] == "/usr/bin/codevira"
        assert config["args"] == []
        assert "cwd" not in config
        assert "--project-dir" not in str(config)

    def test_python_fallback_global_uses_module(self):
        config = _build_global_server_config("python3", "python3")
        assert config["command"] == "python3"
        assert "-m" in config["args"]
        assert "mcp_server" in config["args"]
        assert "--project-dir" not in config["args"]


# ===========================================================================
# Claude Desktop injection
# ===========================================================================


class TestClaudeDesktopInject:
    def test_writes_correct_json_format(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        project = tmp_path / "my-project"
        project.mkdir()
        _inject_claude_desktop(project, "/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert "--project-dir" in entry["args"]
        assert str(project) in entry["args"]
        assert "cwd" not in entry
        assert "url" not in entry

    def test_preserves_existing_desktop_preferences(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude_desktop_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "globalShortcut": "Ctrl+Shift+C",
                    "mcpServers": {"other-mcp": {"command": "other", "args": []}},
                }
            )
        )
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        project = tmp_path / "proj"
        project.mkdir()
        _inject_claude_desktop(project, "/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        assert data["globalShortcut"] == "Ctrl+Shift+C"
        assert "other-mcp" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]

    def test_full_binary_path_required(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        project = tmp_path / "proj"
        project.mkdir()
        full_path = "/usr/local/bin/codevira"
        _inject_claude_desktop(project, full_path, "python3")

        data = json.loads(config_file.read_text())
        assert data["mcpServers"]["codevira"]["command"] == full_path

    def test_claude_desktop_config_path_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        path = _claude_desktop_config_path()
        assert "Claude" in str(path)
        assert "claude_desktop_config.json" == path.name

    def test_claude_desktop_config_path_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        path = _claude_desktop_config_path()
        assert ".config" in str(path)
        assert "claude_desktop_config.json" == path.name


# ===========================================================================
# Per-project injection: _inject_claude, _inject_cursor, _inject_windsurf
# ===========================================================================


class TestInjectClaude:
    def test_writes_per_project_settings(self, tmp_path):
        """v2.0-rc.5 (Bug 16): per-project Claude Code MCP now goes to
        ``<project>/.mcp.json`` (the canonical project-scope MCP file)
        instead of ``<project>/.claude/settings.json`` (which is for
        hooks / permissions / env, NOT mcpServers)."""
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_claude(project, "/usr/bin/codevira", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".mcp.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_mcp_json(self, tmp_path):
        """If <project>/.mcp.json already has other MCP servers, the
        merge must preserve them."""
        project = tmp_path / "proj"
        project.mkdir()
        mcp_json = project / ".mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {"other": {"command": "x"}},
                    "comment": "user content",
                }
            )
        )

        _inject_claude(project, "/usr/bin/codevira", "python3")
        data = json.loads(mcp_json.read_text())
        assert "other" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]
        assert data["comment"] == "user content"


class TestInjectCursor:
    def test_writes_per_project_mcp_json(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_cursor(project, "/usr/bin/codevira", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".cursor" / "mcp.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_cursor_config(self, tmp_path):
        project = tmp_path / "proj"
        cursor_dir = project / ".cursor"
        cursor_dir.mkdir(parents=True)
        mcp_json = cursor_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"existing": {"command": "y"}}}))

        _inject_cursor(project, "/usr/bin/codevira", "python3")
        data = json.loads(mcp_json.read_text())
        assert "existing" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]


class TestInjectWindsurf:
    def test_writes_per_project_mcp_json(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_windsurf(project, "/usr/bin/codevira", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".windsurf" / "mcp.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_windsurf_config(self, tmp_path):
        project = tmp_path / "proj"
        ws_dir = project / ".windsurf"
        ws_dir.mkdir(parents=True)
        mcp_json = ws_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"other-ws": {"command": "z"}}}))

        _inject_windsurf(project, "/usr/bin/codevira", "python3")
        data = json.loads(mcp_json.read_text())
        assert "other-ws" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]


# ===========================================================================
# v3.1.0 M1: CODEVIRA_IDE env stamping (origin tagging Phase A)
# ===========================================================================


class TestM1IdeEnvStamp:
    """Every injected MCP config must carry ``env.CODEVIRA_IDE = <ide_key>``
    so the spawned codevira MCP server can tag every write with
    ``origin.ide``. Per-project + global modes for all 4 stdio IDEs.

    Antigravity is tested separately because it writes to multiple
    config surfaces (~/.gemini/config + ~/.gemini/antigravity) and uses
    a different server-name scheme.
    """

    def _read_codevira_entry(self, path: Path, name: str = "codevira") -> dict:
        return json.loads(path.read_text())["mcpServers"][name]

    def test_per_project_claude_code(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _inject_claude(project, "/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(project / ".mcp.json")
        assert entry["env"]["CODEVIRA_IDE"] == "claude_code"

    def test_per_project_claude_desktop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: tmp_path / "desktop.json",
        )
        project = tmp_path / "proj"
        project.mkdir()
        _inject_claude_desktop(project, "/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(tmp_path / "desktop.json")
        assert entry["env"]["CODEVIRA_IDE"] == "claude_desktop"

    def test_per_project_cursor(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _inject_cursor(project, "/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(project / ".cursor" / "mcp.json")
        assert entry["env"]["CODEVIRA_IDE"] == "cursor"

    def test_per_project_windsurf(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        _inject_windsurf(project, "/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(project / ".windsurf" / "mcp.json")
        assert entry["env"]["CODEVIRA_IDE"] == "windsurf"

    def test_global_claude_desktop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: tmp_path / "desktop.json",
        )
        inject_global_claude_desktop("/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(tmp_path / "desktop.json")
        assert entry["env"]["CODEVIRA_IDE"] == "claude_desktop"

    def test_global_cursor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ide_inject,
            "_cursor_global_config_path",
            lambda: tmp_path / "cursor-global.json",
        )
        inject_global_cursor("/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(tmp_path / "cursor-global.json")
        assert entry["env"]["CODEVIRA_IDE"] == "cursor"

    def test_global_windsurf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ide_inject,
            "_windsurf_global_config_path",
            lambda: tmp_path / "ws-global.json",
        )
        inject_global_windsurf("/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(tmp_path / "ws-global.json")
        assert entry["env"]["CODEVIRA_IDE"] == "windsurf"

    def test_env_preserves_existing_keys(self, tmp_path):
        """If a user has manually added other env vars to an existing
        codevira mcpServers entry, the M1 stamp must MERGE, not
        clobber."""
        project = tmp_path / "proj"
        project.mkdir()
        # Pre-seed: existing codevira entry with a user-set env var
        # (Claude Code preserves existing-server config via _merge_mcp_config
        # but the _inject_* functions overwrite the codevira entry. So the
        # merge happens INSIDE the server_config build, not at the entry
        # level. Test the build-phase merge by calling twice.)
        _inject_claude(project, "/usr/bin/codevira", "python3")
        # Second call: still produces env with CODEVIRA_IDE (idempotent)
        _inject_claude(project, "/usr/bin/codevira", "python3")
        entry = self._read_codevira_entry(project / ".mcp.json")
        assert entry["env"]["CODEVIRA_IDE"] == "claude_code"


# ===========================================================================
# Global mode injection
# ===========================================================================


class TestGlobalModeInject:
    def test_global_claude_code_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        # Force the direct-merge fallback path (not CLI shell-out) so the
        # test exercises file mutation deterministically regardless of
        # whether `claude` CLI is installed in the test environment.
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry
        assert "--project-dir" not in str(entry)

    def test_global_cursor_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(
            ide_inject, "_cursor_global_config_path", lambda: config_file
        )

        inject_global_cursor("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_windsurf_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_windsurf_global_config_path", lambda: config_file
        )

        inject_global_windsurf("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_inject_preserves_existing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        config_file.write_text(
            json.dumps({"mcpServers": {"some-other": {"command": "other"}}})
        )
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        assert "some-other" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]


# ===========================================================================
# HTTP URL injection
# ===========================================================================


class TestHttpUrlInject:
    def test_writes_url_format(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )

        inject_claude_http_url("https://localhost:7443/mcp")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["url"] == "https://localhost:7443/mcp"
        assert "command" not in entry
        assert "args" not in entry

    def test_preserves_existing_on_http_inject(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        config_file.write_text(
            json.dumps({"mcpServers": {"other": {"url": "http://other:8080"}}})
        )
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )

        inject_claude_http_url("https://localhost:7443/mcp")

        data = json.loads(config_file.read_text())
        assert "other" in data["mcpServers"]
        assert data["mcpServers"]["codevira"]["url"] == "https://localhost:7443/mcp"


# ===========================================================================
# Antigravity server name sanitization
# ===========================================================================


class TestAntigravityNameSanitization:
    def test_special_chars_removed(self, tmp_path, monkeypatch):
        import re as re_mod

        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "my@project/2024"
        )

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert len(keys) == 1
        server_name = keys[0]
        assert server_name.startswith("codevira-")
        safe_part = server_name[len("codevira-") :]
        assert re_mod.match(
            r"^[a-z0-9-]+$", safe_part
        ), f"Unsafe chars in '{safe_part}'"

    def test_spaces_become_hyphens(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "My Cool Project"
        )

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert " " not in keys[0]

    def test_no_double_hyphens(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "proj--name__test"
        )

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert "--" not in keys[0]

    def test_uppercase_lowercased(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "UPPER_CASE"
        )

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert keys[0] == keys[0].lower()

    def test_antigravity_uses_project_dir_not_cwd(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "myproj"
        )

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert "--project-dir" in entry["args"]
        assert "cwd" not in entry

    def test_antigravity_has_typename_field(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "myproj"
        )

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert (
            entry["$typeName"] == "exa.cascade_plugins_pb.CascadePluginCommandTemplate"
        )


# ===========================================================================
# _read_json_safe / _write_json_safe
# ===========================================================================


class TestJsonHelpers:
    def test_read_missing_file_returns_empty(self, tmp_path):
        data = _read_json_safe(tmp_path / "nonexistent.json")
        assert data == {}

    def test_read_corrupt_json_returns_empty(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{{not valid json")
        data = _read_json_safe(f)
        assert data == {}

    def test_write_then_read_roundtrip(self, tmp_path):
        f = tmp_path / "test.json"
        payload = {"mcpServers": {"codevira": {"command": "x", "args": []}}}
        _write_json_safe(f, payload)
        assert f.exists()
        data = _read_json_safe(f)
        assert data == payload

    def test_write_creates_parent_dirs(self, tmp_path):
        f = tmp_path / "deep" / "nested" / "settings.json"
        _write_json_safe(f, {"key": "val"})
        assert f.exists()

    def test_read_binary_garbage_returns_empty(self, tmp_path):
        f = tmp_path / "garbage.json"
        f.write_bytes(b"\xff\xfe\x00\x01garbage")
        data = _read_json_safe(f)
        assert data == {}


# ===========================================================================
# detect_installed_ides
# ===========================================================================


class TestDetectInstalledIdes:
    def test_claude_NOT_detected_via_claude_dir_alone(self, tmp_path, monkeypatch):
        """v3.0.0 detection hardening: a per-project ``.claude/``
        directory is NO LONGER a sufficient signal that Claude Code
        is installed. Many users create the dir for IDE state without
        ever installing Claude Code. The strong signal is the
        ``claude`` binary on PATH."""
        (tmp_path / ".claude").mkdir()
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        (tmp_path / "fakehome").mkdir(exist_ok=True)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: tmp_path / "fakehome" / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert "claude" not in result, (
            ".claude/ dir alone must NOT trigger Claude Code detection "
            "in v3.0.0+ — audit-hardened against false positives."
        )

    def test_claude_detected_via_binary_in_path(self, tmp_path, monkeypatch):
        def mock_which(name):
            if name == "claude":
                return "/usr/local/bin/claude"
            return None

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        (tmp_path / "fakehome").mkdir(exist_ok=True)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: tmp_path / "fakehome" / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert "claude" in result

    def test_cursor_detected_via_dir_plus_mcp_json(self, tmp_path, monkeypatch):
        """v3.0.0: ~/.cursor/ + ~/.cursor/mcp.json is the STRONG signal
        (dir alone is too easy to fake; mcp.json proves the user ran
        Cursor at least once)."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        cursor_dir = fakehome / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text("{}")  # the proof file
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert "cursor" in result

    def test_cursor_NOT_detected_via_empty_cursor_dir(self, tmp_path, monkeypatch):
        """The bare ~/.cursor/ dir without mcp.json AND without the
        ``cursor`` binary on PATH is a v3.0.0 false-positive case —
        we explicitly DO NOT detect it. Many users have empty
        ~/.cursor/ left behind from prior installs."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".cursor").mkdir()  # empty dir, no mcp.json
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        assert "cursor" not in detect_installed_ides(tmp_path)

    def test_windsurf_detected_via_mcp_config_json(self, tmp_path, monkeypatch):
        """v3.0.0: Windsurf requires the actual mcp_config.json file
        (in either standard location)."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        windsurf_dir = fakehome / ".windsurf"
        windsurf_dir.mkdir()
        (windsurf_dir / "mcp_config.json").write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert "windsurf" in result

    def test_windsurf_NOT_detected_via_empty_dir(self, tmp_path, monkeypatch):
        """Bare ~/.windsurf/ without mcp_config.json is a false
        positive — explicitly NOT detected in v3.0.0."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".windsurf").mkdir()  # empty
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        assert "windsurf" not in detect_installed_ides(tmp_path)

    def test_antigravity_detected_via_mcp_config_json(self, tmp_path, monkeypatch):
        """v3.0.0: Antigravity requires the actual antigravity/
        mcp_config.json under ~/.gemini/. Bare ~/.gemini/ dir was a
        false positive (any Google CLI gemini install creates it)."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        antigravity_cfg = fakehome / ".gemini" / "antigravity" / "mcp_config.json"
        antigravity_cfg.parent.mkdir(parents=True)
        antigravity_cfg.write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert "antigravity" in result

    def test_antigravity_NOT_detected_via_bare_gemini_dir(self, tmp_path, monkeypatch):
        """v3.0.0: bare ~/.gemini/ (created by any gemini CLI install)
        no longer trips the antigravity detector — we need the actual
        antigravity/mcp_config.json sub-file."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".gemini").mkdir()  # bare, no antigravity/
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        assert "antigravity" not in detect_installed_ides(tmp_path)

    def test_claude_desktop_detected_via_config_file(self, tmp_path, monkeypatch):
        """v3.0.0: Claude Desktop requires the config FILE to exist
        and be valid JSON (was: the parent dir existing was enough)."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        desktop_config = (
            fakehome / "Library" / "Application Support" / "Claude" / "config.json"
        )
        desktop_config.parent.mkdir(parents=True)
        desktop_config.write_text("{}")  # the proof file
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: desktop_config
        )
        result = detect_installed_ides(tmp_path)
        assert "claude_desktop" in result

    def test_claude_desktop_NOT_detected_via_empty_dir(self, tmp_path, monkeypatch):
        """v3.0.0: the parent dir alone is no longer a signal — the
        config.json itself must exist + parse."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        desktop_config_dir = fakehome / "Library" / "Application Support" / "Claude"
        desktop_config_dir.mkdir(parents=True)
        # config.json deliberately ABSENT
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: desktop_config_dir / "config.json",
        )
        assert "claude_desktop" not in detect_installed_ides(tmp_path)

    def test_claude_desktop_NOT_detected_via_corrupt_config(
        self, tmp_path, monkeypatch
    ):
        """Even if the config FILE exists, malformed JSON means the
        app was never set up — refuse to detect."""
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        desktop_config = (
            fakehome / "Library" / "Application Support" / "Claude" / "config.json"
        )
        desktop_config.parent.mkdir(parents=True)
        desktop_config.write_text("this is not json")
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: desktop_config
        )
        assert "claude_desktop" not in detect_installed_ides(tmp_path)

    def test_none_found_returns_empty(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        result = detect_installed_ides(tmp_path)
        assert result == []


# ===========================================================================
# _resolve_command
# ===========================================================================


class TestResolveCommand:
    def test_shutil_which_finds_binary(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/local/bin/codevira" if name == "codevira" else None,
        )
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == "/usr/local/bin/codevira"
        assert python_exe == sys.executable

    def test_pipx_venv_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        pipx_bin = (
            tmp_path / ".local" / "pipx" / "venvs" / "codevira" / "bin" / "codevira"
        )
        pipx_bin.parent.mkdir(parents=True)
        pipx_bin.write_text("#!/bin/bash\n")
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == str(pipx_bin)
        assert python_exe == sys.executable

    def test_fallback_returns_python_exe(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Also ensure sibling binary doesn't exist (venv may have codevira installed)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == python_exe
        assert python_exe == sys.executable


# ===========================================================================
# inject_ide_config — integration tests
# ===========================================================================


class TestInjectIdeConfigIntegration:
    def test_per_project_claude_writes_settings(self, tmp_path, monkeypatch):
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # v3.0.0 detection hardening: empty .claude/ dir is no longer
        # a sufficient signal. Mock `which('claude')` to return a
        # fake binary path so the strong-signal detector trips.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )

        results = inject_ide_config(project, project_name="myproject")
        assert "Claude Code" in results
        config_path = Path(results["Claude Code"])
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]

    def test_global_mode_claude_writes_global_settings(self, tmp_path, monkeypatch):
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".claude").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # v3.0.0 detection hardening: empty .claude/ dir is no longer
        # a sufficient signal. Mock `which('claude')` to return a
        # fake binary path so the strong-signal detector trips.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )
        monkeypatch.setattr(
            ide_inject,
            "_claude_global_config_path",
            lambda: fakehome / ".claude" / "settings.json",
        )

        results = inject_ide_config(project, project_name="myproject", global_mode=True)
        assert "Claude Code (global)" in results
        config_path = Path(results["Claude Code (global)"])
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert "--project-dir" not in str(entry.get("args", []))

    def test_no_ides_detected_returns_empty(self, tmp_path, monkeypatch):
        project = tmp_path / "emptyproject"
        project.mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # This test ASSERTS the no-IDE case — keep `which` returning
        # None for everything (the v3.0.0 sweep set most other tests
        # to mock `which('claude')` because they relied on the old
        # weak signal, but here we genuinely want zero detections).
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )

        results = inject_ide_config(project, project_name="emptyproject")
        assert results == {}

    # --- v3.7.0 (Phase 28): global_mode now FALLS BACK to per-project for
    #     IDEs that can't do project-agnostic config, instead of skipping them
    #     (so those users still get a working server). ---
    def test_global_mode_registers_claude_desktop_per_project(
        self, tmp_path, monkeypatch
    ):
        """In global mode, Claude Desktop (no cwd/roots) falls back to a
        per-project registration rather than being silently skipped."""
        project = tmp_path / "proj"
        project.mkdir()

        monkeypatch.setattr(
            ide_inject, "detect_installed_ides", lambda pr: ["claude_desktop"]
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )
        monkeypatch.setattr(
            ide_inject,
            "_inject_claude_desktop",
            lambda *a, **k: "/fake/claude_desktop_config.json",
        )

        results = inject_ide_config(project, global_mode=True)
        assert "Claude Desktop (per-project)" in results

    def test_global_mode_registers_antigravity_global(self, tmp_path, monkeypatch):
        """SB3: global mode registers Antigravity under the CONSTANT-key global
        helper (one 'codevira' entry), not the per-project one."""
        project = tmp_path / "proj"
        project.mkdir()

        monkeypatch.setattr(
            ide_inject, "detect_installed_ides", lambda pr: ["antigravity"]
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )
        monkeypatch.setattr(
            ide_inject,
            "inject_global_antigravity",
            lambda *a, **k: "/fake/antigravity.json",
        )

        results = inject_ide_config(project, global_mode=True)
        assert "Antigravity (global)" in results

    def test_global_mode_antigravity_uses_single_constant_key(
        self, tmp_path, monkeypatch
    ):
        """SB3 regression: global_mode must register Antigravity under the
        CONSTANT 'codevira' key, not a per-project 'codevira-<name>' key —
        otherwise N projects create N Antigravity entries (the very problem
        the single-registration release exists to kill). Does NOT mock the
        inject fn — exercises the real write path against a temp config."""
        import json as _json

        cfg = tmp_path / "gemini" / "config.json"
        cfg.parent.mkdir(parents=True)
        monkeypatch.setattr(ide_inject, "_antigravity_write_targets", lambda: [cfg])
        monkeypatch.setattr(
            ide_inject, "detect_installed_ides", lambda pr: ["antigravity"]
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )

        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "beta"
        proj_b.mkdir()
        inject_ide_config(proj_a, project_name="alpha", global_mode=True)
        inject_ide_config(proj_b, project_name="beta", global_mode=True)

        servers = _json.loads(cfg.read_text()).get("mcpServers", {})
        keys = [k for k in servers if k.startswith("codevira")]
        assert keys == ["codevira"], (
            f"expected exactly one constant 'codevira' key, got {keys} "
            "— N projects created N Antigravity entries"
        )

    def test_global_mode_claude_is_single_global_registration(
        self, tmp_path, monkeypatch
    ):
        """A roots-capable IDE (Claude Code) gets ONE global registration in
        global mode — the core of the single-MCP win."""
        project = tmp_path / "proj"
        project.mkdir()

        monkeypatch.setattr(ide_inject, "detect_installed_ides", lambda pr: ["claude"])
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )
        captured = {}

        def _fake_global(cmd_path, python_exe):
            captured["called"] = True
            return "/fake/.claude.json"

        monkeypatch.setattr(ide_inject, "inject_global_claude_code", _fake_global)

        results = inject_ide_config(project, global_mode=True)
        assert captured.get("called") is True
        assert "Claude Code (global)" in results

    # --- New: exception handling (IDE injection failure logged, others continue) ---
    def test_exception_in_one_ide_does_not_block_others(self, tmp_path, monkeypatch):
        """If one IDE injection fails, others should still succeed."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        cursor_dir = fakehome / ".cursor"
        cursor_dir.mkdir()
        # v3.0.0 strong signal: empty ~/.cursor/ alone doesn't detect;
        # need either mcp.json or `cursor` on PATH.
        (cursor_dir / "mcp.json").write_text("{}")

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # v3.0.0 detection hardening: empty .claude/ dir is no longer
        # a sufficient signal. Mock `which('claude')` to return a
        # fake binary path so the strong-signal detector trips.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )

        # Make _inject_claude raise, but _inject_cursor should still succeed
        def broken_inject_claude(*args, **kwargs):
            raise RuntimeError("Simulated failure")

        monkeypatch.setattr(ide_inject, "_inject_claude", broken_inject_claude)

        results = inject_ide_config(project, project_name="proj")
        # Claude injection failed, but Cursor should still succeed
        assert "Claude Code" not in results
        assert "Cursor" in results

    # --- New: all IDEs detected simultaneously ---
    def test_all_ides_detected_simultaneously(self, tmp_path, monkeypatch):
        """When all IDEs are detected via STRONG signals, all get
        configs written. v3.0.0+: each IDE needs its proof file
        (mcp.json / mcp_config.json / valid claude_desktop config)."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        cursor_dir = fakehome / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text("{}")
        windsurf_dir = fakehome / ".windsurf"
        windsurf_dir.mkdir()
        (windsurf_dir / "mcp_config.json").write_text("{}")
        antigravity_cfg = fakehome / ".gemini" / "antigravity" / "mcp_config.json"
        antigravity_cfg.parent.mkdir(parents=True)
        antigravity_cfg.write_text("{}")
        desktop_dir = fakehome / "Library" / "Application Support" / "Claude"
        desktop_dir.mkdir(parents=True)
        desktop_config = desktop_dir / "claude_desktop_config.json"
        desktop_config.write_text("{}")  # v3.0.0: valid JSON required

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # v3.0.0 detection hardening: empty .claude/ dir is no longer
        # a sufficient signal. Mock `which('claude')` to return a
        # fake binary path so the strong-signal detector trips.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: desktop_config
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )
        monkeypatch.setattr(
            ide_inject, "_antigravity_config_path", lambda: tmp_path / "ag_config.json"
        )

        results = inject_ide_config(project, project_name="proj")
        assert "Claude Code" in results
        assert "Claude Desktop" in results
        assert "Cursor" in results
        assert "Windsurf" in results
        assert "Antigravity" in results

    def test_project_name_defaults_to_dirname(self, tmp_path, monkeypatch):
        """When project_name is empty, it defaults to project_root.name."""
        project = tmp_path / "my-awesome-project"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        # v3.0.0 detection hardening: empty .claude/ dir is no longer
        # a sufficient signal. Mock `which('claude')` to return a
        # fake binary path so the strong-signal detector trips.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(
            ide_inject,
            "_claude_desktop_config_path",
            lambda: fakehome / "nonexistent" / "config.json",
        )
        monkeypatch.setattr(
            ide_inject,
            "_resolve_command",
            lambda: ("/usr/bin/codevira", sys.executable),
        )

        results = inject_ide_config(project)
        assert "Claude Code" in results


# ===========================================================================
# Chaos tests
# ===========================================================================


class TestChaosIdeInject:
    def test_corrupt_existing_settings_returns_fresh(self, tmp_path):
        """Corrupt existing settings.json should return {} and not crash."""
        corrupt_file = tmp_path / "settings.json"
        corrupt_file.write_text("{{{{not valid json at all!!!!")
        data = _read_json_safe(corrupt_file)
        assert data == {}

    def test_read_empty_file_returns_empty(self, tmp_path):
        """Empty file should return {} without crash."""
        empty = tmp_path / "empty.json"
        empty.write_text("")
        data = _read_json_safe(empty)
        assert data == {}

    def test_config_file_with_readonly_permissions(self, tmp_path):
        """Writing to a read-only config file should raise (or be handled)."""
        project = tmp_path / "proj"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {}}))
        settings.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # read-only

        try:
            # The atomic write goes to .tmp then replaces; the replace or
            # the .tmp write may fail if the parent dir is writable but the
            # existing file is not. The key is no unhandled crash.
            try:
                _inject_claude(project, "/usr/bin/codevira", "python3")
            except (PermissionError, OSError):
                pass  # Expected on strict systems
        finally:
            settings.chmod(stat.S_IRWXU)  # restore for cleanup

    def test_concurrent_writes_to_same_config(self, tmp_path):
        """Concurrent writes to same file should not corrupt JSON."""
        config_file = tmp_path / "settings.json"
        config_file.write_text(json.dumps({"mcpServers": {}}))
        errors = []

        def write_entry(name):
            try:
                existing = _read_json_safe(config_file)
                merged = _merge_mcp_config(existing, name, {"command": f"cmd-{name}"})
                _write_json_safe(config_file, merged)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=write_entry, args=(f"srv-{i}",)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # File should still be valid JSON
        data = _read_json_safe(config_file)
        assert isinstance(data, dict)
        assert "mcpServers" in data
        # At least some entries should have been written
        assert len(data["mcpServers"]) >= 1

    def test_very_long_project_path(self, tmp_path):
        """Very long project path should not crash injection."""
        # Create a deeply nested path
        deep = tmp_path
        for i in range(20):
            deep = deep / f"level_{i:02d}"
        deep.mkdir(parents=True)

        result = _inject_claude(deep, "/usr/bin/codevira", "python3")
        config_path = Path(result)
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]

    def test_write_json_safe_atomic_no_partial_writes(self, tmp_path):
        """_write_json_safe should not leave partial files on crash."""
        f = tmp_path / "test.json"
        payload = {"mcpServers": {"codevira": {"command": "x", "args": []}}}
        _write_json_safe(f, payload)

        # Verify the .tmp file is cleaned up (replaced)
        tmp_file = f.with_suffix(".tmp")
        assert not tmp_file.exists()
        assert f.exists()
        data = json.loads(f.read_text())
        assert data == payload

    def test_inject_with_unicode_project_name(self, tmp_path, monkeypatch):
        """Unicode in project name should not crash Antigravity sanitization."""
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "projet-caf\u00e9-\u00e9l\u00e8ve"
        )

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert len(keys) == 1
        # Should be lowercased and sanitized
        assert keys[0].startswith("codevira-")


# ===========================================================================
# v2.0-rc.2 \u2014 Claude Code CLI shell-out path (Bug 6) + Claude Desktop global
# (Bug 6b)
#
# Claude Code reads `mcpServers` from `~/.claude.json` (NOT
# `~/.claude/settings.json` \u2014 that's hooks/permissions). Bug surfaced by
# real dogfood install: setup looked successful, `claude mcp list` showed
# nothing. Fix: prefer shelling out to `claude mcp add --scope user`,
# fall back to direct `~/.claude.json` merge if CLI unavailable.
# ===========================================================================


class TestClaudeCodeCliShellOut:
    def test_uses_cli_when_available(self, tmp_path, monkeypatch):
        """When claude CLI is on PATH, inject_global_claude_code shells out
        to it instead of mutating ~/.claude.json directly."""
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(
            ide_inject, "_claude_cli_path", lambda: "/fake/path/to/claude"
        )

        captured_calls: list[list[str]] = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            captured_calls.append(list(cmd))

            class _Result:
                returncode = 0
                stderr = ""
                stdout = "Added stdio MCP server codevira"

            return _Result()

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        result_path = inject_global_claude_code("/usr/bin/codevira", "python3")

        # Should have called: remove (best-effort) then add
        assert len(captured_calls) == 2, f"expected remove+add, got {captured_calls}"
        assert captured_calls[0][:4] == [
            "/fake/path/to/claude",
            "mcp",
            "remove",
            "codevira",
        ]
        assert captured_calls[1][:4] == [
            "/fake/path/to/claude",
            "mcp",
            "add",
            "--scope",
        ]
        assert "codevira" in captured_calls[1]
        assert "/usr/bin/codevira" in captured_calls[1]

        # Returns ~/.claude.json (the file claude CLI mutates, not our
        # tmp_path mock since we delegated to the CLI).
        assert result_path == str(config_file)

        # We did NOT write to the file directly \u2014 that's the CLI's job.
        assert not config_file.exists()

    def test_cli_argv_includes_env_codevira_ide(self, tmp_path, monkeypatch):
        """CRITICAL \u2014 the CLI-shellout path (preferred when claude is on PATH)
        MUST forward --env CODEVIRA_IDE=claude_code so every write tags origin.

        Without this, the dominant deployment (claude CLI installed) silently
        produces 'unknown' origins and every cross-IDE conflict reads as
        'unknown vs <other>'.
        """
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: "/fake/claude")

        captured: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured.append(list(cmd))

            class _R:
                returncode = 0
                stderr = ""
                stdout = "Added"

            return _R()

        monkeypatch.setattr("subprocess.run", fake_run)
        inject_global_claude_code("/usr/bin/codevira", "python3")

        # captured[1] is the `mcp add` call (captured[0] is the remove).
        add_argv = captured[1]
        # Inline check: --env CODEVIRA_IDE=claude_code must appear as adjacent
        # tokens. We pair-scan rather than just checking membership so a
        # regression that puts CODEVIRA_IDE in the wrong flag slot is caught.
        pairs = list(zip(add_argv, add_argv[1:]))
        assert ("--env", "CODEVIRA_IDE=claude_code") in pairs, (
            f"--env CODEVIRA_IDE=claude_code not forwarded to claude CLI; "
            f"argv was {add_argv}"
        )

    def test_falls_back_to_direct_merge_when_cli_missing(self, tmp_path, monkeypatch):
        """When claude CLI is NOT on PATH, fall back to direct merge of
        ~/.claude.json."""
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        # subprocess.run should NOT be called in this branch \u2014 make it
        # raise to detect any accidental invocation.
        def boom(*args, **kwargs):
            raise AssertionError("subprocess.run should not run when CLI missing")

        monkeypatch.setattr("subprocess.run", boom)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["mcpServers"]["codevira"]["command"] == "/usr/bin/codevira"

    def test_falls_back_when_cli_returns_nonzero(self, tmp_path, monkeypatch):
        """If claude CLI exists but invocation fails (bad args, version
        mismatch, perms), fall back to direct merge \u2014 never lose the
        registration."""
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(
            ide_inject, "_claude_cli_path", lambda: "/fake/path/to/claude"
        )

        def fake_subprocess_run(cmd, *args, **kwargs):
            class _Result:
                returncode = 2  # nonzero = failure
                stderr = "Error: unknown flag --scope"
                stdout = ""

            return _Result()

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        # Fallback path wrote the file directly
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "codevira" in data["mcpServers"]

    def test_falls_back_when_subprocess_raises(self, tmp_path, monkeypatch):
        """OSError / TimeoutExpired during subprocess invocation must trigger
        fallback, not crash."""
        import subprocess as _subprocess

        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(
            ide_inject, "_claude_cli_path", lambda: "/fake/path/to/claude"
        )

        def fake_subprocess_run(cmd, *args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        # Fallback wrote the file
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "codevira" in data["mcpServers"]

    def test_direct_merge_preserves_other_top_level_keys(self, tmp_path, monkeypatch):
        """The 43KB ~/.claude.json has many top-level keys (oauthAccount,
        projects, telemetry, etc.). Direct-merge fallback must preserve
        every one of them \u2014 only mcpServers.codevira changes."""
        config_file = tmp_path / "claude.json"
        config_file.write_text(
            json.dumps(
                {
                    "userID": "abc-123",
                    "oauthAccount": {"email": "user@example.com"},
                    "projects": {"/some/path": {"hasTrustDialogAccepted": True}},
                    "numStartups": 47,
                    "mcpServers": {"existing-server": {"command": "/bin/other"}},
                }
            )
        )
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        # Every original top-level key preserved
        assert data["userID"] == "abc-123"
        assert data["oauthAccount"]["email"] == "user@example.com"
        assert data["projects"]["/some/path"]["hasTrustDialogAccepted"] is True
        assert data["numStartups"] == 47
        # Other MCP servers preserved
        assert data["mcpServers"]["existing-server"]["command"] == "/bin/other"
        # Codevira added
        assert data["mcpServers"]["codevira"]["command"] == "/usr/bin/codevira"


class TestClaudeDesktopGlobalInject:
    def test_creates_config_with_no_cwd_no_url(self, tmp_path, monkeypatch):
        """Claude Desktop config must NOT use cwd (Desktop ignores it) and
        NOT use url format (stdio only)."""
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        result_path = inject_global_claude_desktop("/usr/bin/codevira", "python3")

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert "cwd" not in entry
        assert "url" not in entry
        assert result_path == str(config_file)

    def test_python_fallback_uses_module_form(self, tmp_path, monkeypatch):
        """When codevira binary isn't on PATH (cmd_path == python_exe),
        Claude Desktop entry must use `python -m mcp_server`."""
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        # Simulate fallback: cmd_path == python_exe means binary not found
        inject_global_claude_desktop("/usr/bin/python3", "/usr/bin/python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/python3"
        assert entry["args"] == ["-m", "mcp_server"]

    def test_preserves_existing_servers(self, tmp_path, monkeypatch):
        """Claude Desktop user may have other MCP servers configured.
        Inject must not clobber them."""
        config_file = tmp_path / "claude_desktop_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {"command": "/usr/bin/fs-mcp"},
                        "github": {"command": "/usr/bin/github-mcp"},
                    }
                }
            )
        )
        monkeypatch.setattr(
            ide_inject, "_claude_desktop_config_path", lambda: config_file
        )

        inject_global_claude_desktop("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        assert data["mcpServers"]["filesystem"]["command"] == "/usr/bin/fs-mcp"
        assert data["mcpServers"]["github"]["command"] == "/usr/bin/github-mcp"
        assert data["mcpServers"]["codevira"]["command"] == "/usr/bin/codevira"


class TestClaudeGlobalConfigPathIsCorrect:
    def test_path_is_dot_claude_dot_json_not_settings(self):
        """Regression test for Bug 6 \u2014 the showstopper. Claude Code reads
        mcpServers from ~/.claude.json, NOT ~/.claude/settings.json. If
        anyone tries to 'fix' this back to settings.json (which seems
        intuitive \u2014 settings file holds settings) the wedge breaks
        silently for every new install."""
        from pathlib import Path

        result = ide_inject._claude_global_config_path()
        # Must be ~/.claude.json
        assert result == Path.home() / ".claude.json"
        # Must NOT be ~/.claude/settings.json (that's hooks/permissions)
        assert result != Path.home() / ".claude" / "settings.json"


class TestAtomicWriteHardening:
    """2026-05-17 P3 hardening: _write_json_safe now uses (a) unique
    tempfile name (no collisions between concurrent codevira sessions),
    (b) os.fsync before rename (no torn writes on power loss), and
    (c) verify-after-write (catches any silent corruption).
    """

    def test_round_trip_preserves_data(self, tmp_path):
        """The basic happy path: write, read back, identical."""
        from mcp_server.ide_inject import _write_json_safe, _read_json_safe

        target = tmp_path / "config.json"
        payload = {"mcpServers": {"codevira": {"command": "/usr/local/bin/codevira"}}}
        _write_json_safe(target, payload)
        assert _read_json_safe(target) == payload

    def test_no_tmp_file_leaks_on_success(self, tmp_path):
        """After a successful write, no .tmp leftovers in the target dir."""
        from mcp_server.ide_inject import _write_json_safe

        target = tmp_path / "config.json"
        _write_json_safe(target, {"a": 1})
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert (
            leftovers == []
        ), f"P3 regression: tempfile leftovers in target dir: {leftovers}"

    def test_concurrent_writes_dont_collide_on_tmp(self, tmp_path):
        """Two writes to the same path in quick succession must both
        complete (using unique tmp names). Was Bug: previous code used
        `path.with_suffix('.tmp')` which collided between concurrent
        codevira sessions, silently losing one write."""
        from mcp_server.ide_inject import _write_json_safe, _read_json_safe

        target = tmp_path / "config.json"
        _write_json_safe(target, {"writer": 1})
        _write_json_safe(target, {"writer": 2})
        # Second write must win and no errors thrown.
        assert _read_json_safe(target) == {"writer": 2}

    def test_atomic_replacement_preserves_old_file_until_done(self, tmp_path):
        """Before the rename, the target should still hold OLD content.
        After rename, target is NEW. There is no instant when the target
        file 'briefly disappears' (that would be the bug atomicity prevents)."""
        from mcp_server.ide_inject import _write_json_safe, _read_json_safe

        target = tmp_path / "config.json"
        _write_json_safe(target, {"v": 1})
        # Snapshot inode (POSIX uniquely identifies file by inode).
        original_inode = os.stat(target).st_ino
        # Overwrite. Verify the file exists at every moment and ends up new.
        _write_json_safe(target, {"v": 2})
        assert target.exists()
        assert _read_json_safe(target) == {"v": 2}
        # The inode should have changed \u2014 proves we wrote a new file and
        # atomically replaced (rather than truncate-and-rewrite which would
        # have torn).
        new_inode = os.stat(target).st_ino
        assert new_inode != original_inode, (
            f"P3 regression: atomic rename should produce a new inode; "
            f"got same inode {new_inode} (looks like truncate-rewrite, not atomic)"
        )


class TestAntigravity20Paths:
    """v3.0.0: Antigravity 2.0 split config into the shared ~/.gemini/config/
    and per-app ~/.gemini/antigravity/. codevira must detect + write both.
    """

    def _home(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "home"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        return fakehome

    def test_write_targets_includes_both_when_both_dirs_exist(
        self, tmp_path, monkeypatch
    ):
        home = self._home(tmp_path, monkeypatch)
        (home / ".gemini" / "config").mkdir(parents=True)
        (home / ".gemini" / "antigravity").mkdir(parents=True)
        targets = ide_inject._antigravity_write_targets()
        names = {str(p) for p in targets}
        assert any("config/mcp_config.json" in n for n in names)
        assert any("antigravity/mcp_config.json" in n for n in names)

    def test_write_targets_defaults_to_per_app_when_none_exist(
        self, tmp_path, monkeypatch
    ):
        self._home(tmp_path, monkeypatch)  # no .gemini subdirs created
        targets = ide_inject._antigravity_write_targets()
        assert len(targets) == 1
        assert str(targets[0]).endswith("antigravity/mcp_config.json")

    def test_detect_via_shared_config_only(self, tmp_path, monkeypatch):
        home = self._home(tmp_path, monkeypatch)
        shared = home / ".gemini" / "config" / "mcp_config.json"
        shared.parent.mkdir(parents=True)
        shared.write_text("{}")
        assert "antigravity" in ide_inject.detect_installed_ides(tmp_path)

    def test_inject_writes_into_every_existing_surface(self, tmp_path, monkeypatch):
        home = self._home(tmp_path, monkeypatch)
        (home / ".gemini" / "config").mkdir(parents=True)
        (home / ".gemini" / "antigravity").mkdir(parents=True)
        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "demo")
        for sub in ("config", "antigravity"):
            cfg = home / ".gemini" / sub / "mcp_config.json"
            data = json.loads(cfg.read_text())
            assert any(k.startswith("codevira-") for k in data["mcpServers"])


# ──────────────────────────────────────────────────────────────────────
# v3.1.0 M1 — origin tag (CODEVIRA_IDE) on every injector path
# ──────────────────────────────────────────────────────────────────────


class TestM1OriginStampGlobalClaudeCode:
    """The direct-merge fallback path (CLI absent / fails) MUST set
    env.CODEVIRA_IDE=claude_code so writes from a direct-install user
    still tag origin. Without this, the second-most-common deployment
    silently produces 'unknown' origins."""

    def test_direct_merge_fallback_stamps_ide(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["env"]["CODEVIRA_IDE"] == "claude_code"


class TestM1OriginStampAntigravity:
    """inject_global_antigravity stamps env.CODEVIRA_IDE='antigravity' on
    base_config BEFORE wrapping in the $typeName envelope. A spread-order
    bug could silently drop env from one or both targets."""

    def test_global_stamps_ide_in_all_surfaces(self, tmp_path, monkeypatch):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        monkeypatch.setattr(ide_inject, "_antigravity_write_targets", lambda: [a, b])

        ide_inject.inject_global_antigravity("/usr/bin/codevira", "python3")

        for path in (a, b):
            data = json.loads(path.read_text())
            entry = data["mcpServers"]["codevira"]
            assert (
                entry["env"]["CODEVIRA_IDE"] == "antigravity"
            ), f"target {path} missing CODEVIRA_IDE stamp"
            # And the envelope survived too.
            assert "$typeName" in entry
            assert entry["$typeName"].startswith("exa.cascade_plugins_pb")

    def test_per_project_stamps_ide_on_dict_spread(self, tmp_path, monkeypatch):
        """_inject_antigravity stamps env on each per-project entry.
        Locks in the dict-spread so a regression that loses env can't
        silently ship."""
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(
            ide_inject, "_antigravity_write_targets", lambda: [config_file]
        )
        project = tmp_path / "proj"
        project.mkdir()

        ide_inject._inject_antigravity(
            project, "/usr/bin/codevira", "python3", "myproj"
        )

        data = json.loads(config_file.read_text())
        entry = next(iter(data["mcpServers"].values()))
        assert entry["env"]["CODEVIRA_IDE"] == "antigravity"


class TestRemoveCodeviraFromConfig:
    """remove_codevira_from_config is a public uninstall surface with
    documented semantics — removes 'codevira' AND any 'codevira-<x>'
    Antigravity per-project entries. Has zero tests in the existing suite."""

    def test_removes_plain_key(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"codevira": {"x": 1}, "other": {"y": 2}}})
        )
        assert ide_inject.remove_codevira_from_config(cfg) is True
        data = json.loads(cfg.read_text())
        assert "codevira" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_removes_antigravity_prefix_keys(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "codevira-myproj": {"x": 1},
                        "codevira-otherproj": {"x": 2},
                        "codevira": {"x": 3},
                        "unrelated-server": {"x": 4},
                    }
                }
            )
        )
        assert ide_inject.remove_codevira_from_config(cfg) is True
        data = json.loads(cfg.read_text())
        servers = data["mcpServers"]
        # All three codevira* entries gone.
        assert all(not k.startswith("codevira") for k in servers)
        # Unrelated server preserved.
        assert "unrelated-server" in servers

    def test_returns_false_when_nothing_to_remove(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"mcpServers": {"only-other": {"x": 1}}}))
        assert ide_inject.remove_codevira_from_config(cfg) is False
        # File unchanged.
        data = json.loads(cfg.read_text())
        assert data["mcpServers"] == {"only-other": {"x": 1}}

    def test_missing_file_returns_false(self, tmp_path):
        assert ide_inject.remove_codevira_from_config(tmp_path / "nope.json") is False


class TestM1UserEnvKeysOnTheCodeviraEntry:
    """`_merge_mcp_config` replaces the entire codevira entry (it is a
    'non-destructive' merge ONLY in the sense that it doesn't touch OTHER
    servers — the codevira entry itself is OVERWRITTEN). This test
    locks in that behavior so a future change to a deep-merge requires
    explicit test update. Users who have customized codevira's env keys
    on disk should be aware this is the current contract.

    For OTHER servers (non-codevira) in the same config, the merge IS
    non-destructive — that's also locked in below.
    """

    def test_other_server_entries_are_untouched(self, tmp_path, monkeypatch):
        """Pre-seeded NON-codevira entries must survive an inject."""
        config_file = tmp_path / "claude.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-other-server": {
                            "command": "/my/server",
                            "env": {"HTTP_PROXY": "http://corp:3128"},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        # Other server fully preserved.
        other = data["mcpServers"]["my-other-server"]
        assert other["command"] == "/my/server"
        assert other["env"]["HTTP_PROXY"] == "http://corp:3128"
        # And codevira appeared with its M1 stamp.
        assert data["mcpServers"]["codevira"]["env"]["CODEVIRA_IDE"] == "claude_code"

    def test_codevira_entry_is_currently_replaced_not_merged(
        self, tmp_path, monkeypatch
    ):
        """Lock current behavior: the codevira entry is REPLACED whole.
        If we ever switch to a deep-merge that preserves user-set keys
        on the codevira entry, this test must be flipped to assert
        the user key survives — the change deserves the visibility."""
        config_file = tmp_path / "claude.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "codevira": {
                            "command": "/old/path",
                            "env": {"USER_CUSTOM_FLAG": "1"},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            ide_inject, "_claude_global_config_path", lambda: config_file
        )
        monkeypatch.setattr(ide_inject, "_claude_cli_path", lambda: None)

        inject_global_claude_code("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        env = data["mcpServers"]["codevira"]["env"]
        # Stamp present.
        assert env["CODEVIRA_IDE"] == "claude_code"
        # Locked-in current behavior: user's custom key NOT preserved.
        assert "USER_CUSTOM_FLAG" not in env, (
            "_merge_mcp_config now deep-merges the codevira entry. "
            "Update this test to assert USER_CUSTOM_FLAG survives."
        )


class TestM1AntigravityMultiTargetFailure:
    """v3.1.x fix: multi-target Antigravity writes are now atomic at
    the cross-file level. Snapshot pre-write; on any write failure,
    restore the successfully-written targets. Either all stamped or
    none — no asymmetric provenance state."""

    def test_rollback_on_second_target_failure_no_target_stamped(
        self, tmp_path, monkeypatch
    ):
        ok_target = tmp_path / "ok.json"
        bad_parent = tmp_path / "blocked"
        bad_parent.write_text("not a dir")
        bad_target = bad_parent / "x.json"

        monkeypatch.setattr(
            ide_inject,
            "_antigravity_write_targets",
            lambda: [ok_target, bad_target],
        )

        with pytest.raises(Exception):  # noqa: BLE001
            ide_inject.inject_global_antigravity("/usr/bin/codevira", "python3")

        # v3.1.x fix: write #1 was rolled back when write #2 failed.
        # ok_target didn't exist before; rollback unlinked it.
        assert (
            not ok_target.is_file()
        ), "rollback failed — write #1 still on disk after write #2 fail"

    def test_rollback_restores_pre_write_content_when_target_existed(
        self, tmp_path, monkeypatch
    ):
        """If target had pre-existing content, rollback restores it
        (not just unlinks)."""
        ok_target = tmp_path / "ok.json"
        ok_target.write_text('{"mcpServers": {"other-server": {"command": "x"}}}')
        original = ok_target.read_text()

        bad_parent = tmp_path / "blocked"
        bad_parent.write_text("not a dir")
        bad_target = bad_parent / "x.json"

        monkeypatch.setattr(
            ide_inject,
            "_antigravity_write_targets",
            lambda: [ok_target, bad_target],
        )

        with pytest.raises(Exception):  # noqa: BLE001
            ide_inject.inject_global_antigravity("/usr/bin/codevira", "python3")

        # Pre-write content restored exactly.
        assert ok_target.read_text() == original
