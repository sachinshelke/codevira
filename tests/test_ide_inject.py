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
from unittest.mock import patch

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
        config = _build_server_config("/usr/bin/codevira", "python3", tmp_path, use_cwd=True)
        assert config["command"] == "/usr/bin/codevira"
        assert config["args"] == []
        assert config["cwd"] == str(tmp_path)
        assert "--project-dir" not in config.get("args", [])

    def test_binary_without_cwd_uses_project_dir_arg(self, tmp_path):
        config = _build_server_config("/usr/bin/codevira", "python3", tmp_path, use_cwd=False)
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
        config = _build_server_config("/usr/bin/codevira", "python3", tmp_path, use_cwd=True)
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
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

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
        config_file.write_text(json.dumps({
            "globalShortcut": "Ctrl+Shift+C",
            "mcpServers": {"other-mcp": {"command": "other", "args": []}},
        }))
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        _inject_claude_desktop(project, "/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        assert data["globalShortcut"] == "Ctrl+Shift+C"
        assert "other-mcp" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]

    def test_full_binary_path_required(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

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
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_claude(project, "/usr/bin/codevira", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".claude" / "settings.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_settings(self, tmp_path):
        project = tmp_path / "proj"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))

        _inject_claude(project, "/usr/bin/codevira", "python3")
        data = json.loads(settings.read_text())
        assert "other" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]
        assert data["theme"] == "dark"


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
# Global mode injection
# ===========================================================================

class TestGlobalModeInject:
    def test_global_claude_code_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
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
        monkeypatch.setattr(ide_inject, "_cursor_global_config_path", lambda: config_file)

        inject_global_cursor("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_windsurf_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_windsurf_global_config_path", lambda: config_file)

        inject_global_windsurf("/usr/bin/codevira", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_inject_preserves_existing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        config_file.write_text(json.dumps({
            "mcpServers": {"some-other": {"command": "other"}}
        }))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
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
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

        inject_claude_http_url("https://localhost:7443/mcp")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["url"] == "https://localhost:7443/mcp"
        assert "command" not in entry
        assert "args" not in entry

    def test_preserves_existing_on_http_inject(self, tmp_path, monkeypatch):
        config_file = tmp_path / "settings.json"
        config_file.write_text(json.dumps({
            "mcpServers": {"other": {"url": "http://other:8080"}}
        }))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

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
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "my@project/2024")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert len(keys) == 1
        server_name = keys[0]
        assert server_name.startswith("codevira-")
        safe_part = server_name[len("codevira-"):]
        assert re_mod.match(r"^[a-z0-9-]+$", safe_part), f"Unsafe chars in '{safe_part}'"

    def test_spaces_become_hyphens(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "My Cool Project")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert " " not in keys[0]

    def test_no_double_hyphens(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "proj--name__test")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert "--" not in keys[0]

    def test_uppercase_lowercased(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "UPPER_CASE")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert keys[0] == keys[0].lower()

    def test_antigravity_uses_project_dir_not_cwd(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "myproj")

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert "--project-dir" in entry["args"]
        assert "cwd" not in entry

    def test_antigravity_has_typename_field(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "myproj")

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert entry["$typeName"] == "exa.cascade_plugins_pb.CascadePluginCommandTemplate"


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
    def test_claude_detected_via_claude_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".claude").mkdir()
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        (tmp_path / "fakehome").mkdir(exist_ok=True)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: tmp_path / "fakehome" / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert "claude" in result

    def test_claude_detected_via_binary_in_path(self, tmp_path, monkeypatch):
        def mock_which(name):
            if name == "claude":
                return "/usr/local/bin/claude"
            return None
        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        (tmp_path / "fakehome").mkdir(exist_ok=True)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: tmp_path / "fakehome" / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert "claude" in result

    def test_cursor_detected_via_cursor_dir(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".cursor").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert "cursor" in result

    def test_windsurf_detected_via_windsurf_dir(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".windsurf").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert "windsurf" in result

    def test_antigravity_detected_via_gemini_dir(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".gemini").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert "antigravity" in result

    def test_claude_desktop_detected_via_config_dir(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        desktop_config = fakehome / "Library" / "Application Support" / "Claude" / "config.json"
        desktop_config.parent.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: desktop_config)
        result = detect_installed_ides(tmp_path)
        assert "claude_desktop" in result

    def test_none_found_returns_empty(self, tmp_path, monkeypatch):
        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        result = detect_installed_ides(tmp_path)
        assert result == []


# ===========================================================================
# _resolve_command
# ===========================================================================

class TestResolveCommand:
    def test_shutil_which_finds_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codevira" if name == "codevira" else None)
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == "/usr/local/bin/codevira"
        assert python_exe == sys.executable

    def test_pipx_venv_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        pipx_bin = tmp_path / ".local" / "pipx" / "venvs" / "codevira" / "bin" / "codevira"
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
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

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
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path",
                            lambda: fakehome / ".claude" / "settings.json")

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
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

        results = inject_ide_config(project, project_name="emptyproject")
        assert results == {}

    # --- New: global_mode=True skips Claude Desktop and Antigravity ---
    def test_global_mode_skips_claude_desktop(self, tmp_path, monkeypatch):
        """In global mode, Claude Desktop should be skipped (can't do project-agnostic)."""
        project = tmp_path / "proj"
        project.mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        # Set up Claude Desktop as detected
        desktop_config_dir = fakehome / "Library" / "Application Support" / "Claude"
        desktop_config_dir.mkdir(parents=True)
        desktop_config = desktop_config_dir / "claude_desktop_config.json"

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: desktop_config)
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

        results = inject_ide_config(project, global_mode=True)
        # Claude Desktop should NOT appear in global mode results
        assert "Claude Desktop" not in results

    def test_global_mode_skips_antigravity(self, tmp_path, monkeypatch):
        """In global mode, Antigravity should be skipped."""
        project = tmp_path / "proj"
        project.mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".gemini").mkdir()

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

        results = inject_ide_config(project, global_mode=True)
        assert "Antigravity" not in results

    # --- New: exception handling (IDE injection failure logged, others continue) ---
    def test_exception_in_one_ide_does_not_block_others(self, tmp_path, monkeypatch):
        """If one IDE injection fails, others should still succeed."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".cursor").mkdir()

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

        # Make _inject_claude raise, but _inject_cursor should still succeed
        original_inject_claude = ide_inject._inject_claude
        def broken_inject_claude(*args, **kwargs):
            raise RuntimeError("Simulated failure")
        monkeypatch.setattr(ide_inject, "_inject_claude", broken_inject_claude)

        results = inject_ide_config(project, project_name="proj")
        # Claude injection failed, but Cursor should still succeed
        assert "Claude Code" not in results
        assert "Cursor" in results

    # --- New: all IDEs detected simultaneously ---
    def test_all_ides_detected_simultaneously(self, tmp_path, monkeypatch):
        """When all IDEs are detected, all get configs written."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".claude").mkdir()

        fakehome = tmp_path / "fakehome"
        fakehome.mkdir()
        (fakehome / ".cursor").mkdir()
        (fakehome / ".windsurf").mkdir()
        (fakehome / ".gemini").mkdir()
        desktop_dir = fakehome / "Library" / "Application Support" / "Claude"
        desktop_dir.mkdir(parents=True)
        desktop_config = desktop_dir / "claude_desktop_config.json"

        monkeypatch.setattr(Path, "home", lambda: fakehome)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: desktop_config)
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))
        monkeypatch.setattr(ide_inject, "_antigravity_config_path",
                            lambda: tmp_path / "ag_config.json")

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
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path",
                            lambda: fakehome / "nonexistent" / "config.json")
        monkeypatch.setattr(ide_inject, "_resolve_command",
                            lambda: ("/usr/bin/codevira", sys.executable))

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

        threads = [threading.Thread(target=write_entry, args=(f"srv-{i}",)) for i in range(10)]
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
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira", "python3", "projet-caf\u00e9-\u00e9l\u00e8ve")

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
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
        monkeypatch.setattr(ide_inject, "_claude_cli_path",
                            lambda: "/fake/path/to/claude")

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
        assert captured_calls[0][:4] == ["/fake/path/to/claude", "mcp", "remove", "codevira"]
        assert captured_calls[1][:4] == ["/fake/path/to/claude", "mcp", "add", "--scope"]
        assert "codevira" in captured_calls[1]
        assert "/usr/bin/codevira" in captured_calls[1]

        # Returns ~/.claude.json (the file claude CLI mutates, not our
        # tmp_path mock since we delegated to the CLI).
        assert result_path == str(config_file)

        # We did NOT write to the file directly \u2014 that's the CLI's job.
        assert not config_file.exists()

    def test_falls_back_to_direct_merge_when_cli_missing(self, tmp_path, monkeypatch):
        """When claude CLI is NOT on PATH, fall back to direct merge of
        ~/.claude.json."""
        config_file = tmp_path / "claude.json"
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
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
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
        monkeypatch.setattr(ide_inject, "_claude_cli_path",
                            lambda: "/fake/path/to/claude")

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
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
        monkeypatch.setattr(ide_inject, "_claude_cli_path",
                            lambda: "/fake/path/to/claude")

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
        config_file.write_text(json.dumps({
            "userID": "abc-123",
            "oauthAccount": {"email": "user@example.com"},
            "projects": {"/some/path": {"hasTrustDialogAccepted": True}},
            "numStartups": 47,
            "mcpServers": {"existing-server": {"command": "/bin/other"}},
        }))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)
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
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

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
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

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
        config_file.write_text(json.dumps({
            "mcpServers": {
                "filesystem": {"command": "/usr/bin/fs-mcp"},
                "github": {"command": "/usr/bin/github-mcp"},
            }
        }))
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

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
