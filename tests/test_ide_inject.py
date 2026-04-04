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
        config = _build_server_config("/usr/bin/codevira-mcp", "python3", tmp_path, use_cwd=True)
        assert config["command"] == "/usr/bin/codevira-mcp"
        assert config["args"] == []
        assert config["cwd"] == str(tmp_path)
        assert "--project-dir" not in config.get("args", [])

    def test_binary_without_cwd_uses_project_dir_arg(self, tmp_path):
        config = _build_server_config("/usr/bin/codevira-mcp", "python3", tmp_path, use_cwd=False)
        assert config["command"] == "/usr/bin/codevira-mcp"
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
        config = _build_server_config("/usr/bin/codevira-mcp", "python3", tmp_path, use_cwd=True)
        assert "--project-dir" not in config["args"]


# ===========================================================================
# _build_global_server_config
# ===========================================================================

class TestBuildGlobalServerConfig:
    def test_binary_global_has_empty_args(self):
        config = _build_global_server_config("/usr/bin/codevira-mcp", "python3")
        assert config["command"] == "/usr/bin/codevira-mcp"
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
        _inject_claude_desktop(project, "/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira-mcp"
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
        _inject_claude_desktop(project, "/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        assert data["globalShortcut"] == "Ctrl+Shift+C"
        assert "other-mcp" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]

    def test_full_binary_path_required(self, tmp_path, monkeypatch):
        config_file = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr(ide_inject, "_claude_desktop_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        full_path = "/usr/local/bin/codevira-mcp"
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
        result = _inject_claude(project, "/usr/bin/codevira-mcp", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".claude" / "settings.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira-mcp"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_settings(self, tmp_path):
        project = tmp_path / "proj"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))

        _inject_claude(project, "/usr/bin/codevira-mcp", "python3")
        data = json.loads(settings.read_text())
        assert "other" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]
        assert data["theme"] == "dark"


class TestInjectCursor:
    def test_writes_per_project_mcp_json(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_cursor(project, "/usr/bin/codevira-mcp", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".cursor" / "mcp.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira-mcp"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_cursor_config(self, tmp_path):
        project = tmp_path / "proj"
        cursor_dir = project / ".cursor"
        cursor_dir.mkdir(parents=True)
        mcp_json = cursor_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"existing": {"command": "y"}}}))

        _inject_cursor(project, "/usr/bin/codevira-mcp", "python3")
        data = json.loads(mcp_json.read_text())
        assert "existing" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]


class TestInjectWindsurf:
    def test_writes_per_project_mcp_json(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        result = _inject_windsurf(project, "/usr/bin/codevira-mcp", "python3")
        config_path = Path(result)
        assert config_path.exists()
        assert config_path == project / ".windsurf" / "mcp.json"
        data = json.loads(config_path.read_text())
        assert "codevira" in data["mcpServers"]
        entry = data["mcpServers"]["codevira"]
        assert entry["command"] == "/usr/bin/codevira-mcp"
        assert entry["cwd"] == str(project)

    def test_preserves_existing_windsurf_config(self, tmp_path):
        project = tmp_path / "proj"
        ws_dir = project / ".windsurf"
        ws_dir.mkdir(parents=True)
        mcp_json = ws_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"other-ws": {"command": "z"}}}))

        _inject_windsurf(project, "/usr/bin/codevira-mcp", "python3")
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

        inject_global_claude_code("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry
        assert "--project-dir" not in str(entry)

    def test_global_cursor_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(ide_inject, "_cursor_global_config_path", lambda: config_file)

        inject_global_cursor("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_windsurf_has_no_project_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_windsurf_global_config_path", lambda: config_file)

        inject_global_windsurf("/usr/bin/codevira-mcp", "python3")

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

        inject_global_claude_code("/usr/bin/codevira-mcp", "python3")

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
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "my@project/2024")

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
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "My Cool Project")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert " " not in keys[0]

    def test_no_double_hyphens(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "proj--name__test")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert "--" not in keys[0]

    def test_uppercase_lowercased(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "UPPER_CASE")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert keys[0] == keys[0].lower()

    def test_antigravity_uses_project_dir_not_cwd(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "myproj")

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert "--project-dir" in entry["args"]
        assert "cwd" not in entry

    def test_antigravity_has_typename_field(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "myproj")

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
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codevira-mcp" if name == "codevira-mcp" else None)
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == "/usr/local/bin/codevira-mcp"
        assert python_exe == sys.executable

    def test_pipx_venv_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        pipx_bin = tmp_path / ".local" / "pipx" / "venvs" / "codevira-mcp" / "bin" / "codevira-mcp"
        pipx_bin.parent.mkdir(parents=True)
        pipx_bin.write_text("#!/bin/bash\n")
        cmd_path, python_exe = _resolve_command()
        assert cmd_path == str(pipx_bin)
        assert python_exe == sys.executable

    def test_fallback_returns_python_exe(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))
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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))
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
                            lambda: ("/usr/bin/codevira-mcp", sys.executable))

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
                _inject_claude(project, "/usr/bin/codevira-mcp", "python3")
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

        result = _inject_claude(deep, "/usr/bin/codevira-mcp", "python3")
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
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "projet-caf\u00e9-\u00e9l\u00e8ve")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert len(keys) == 1
        # Should be lowercased and sanitized
        assert keys[0].startswith("codevira-")
