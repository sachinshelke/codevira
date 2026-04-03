"""
Tests for Codevira v1.6 IDE injection changes.

Covers:
  - Claude Desktop: correct JSON format (command+args with --project-dir, not cwd/url)
  - Global mode: no project path in config, args=[]
  - HTTP URL injection for Claude Code
  - Antigravity server name sanitization
  - _merge_mcp_config: preserves existing preferences
  - _build_server_config / _build_global_server_config shapes
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp_server.ide_inject import (
    _build_global_server_config,
    _build_server_config,
    _claude_desktop_config_path,
    _inject_claude_desktop,
    _merge_mcp_config,
    _read_json_safe,
    _write_json_safe,
    inject_claude_http_url,
    inject_global_claude_code,
    inject_global_cursor,
    inject_global_windsurf,
)


# ---------------------------------------------------------------------------
# _merge_mcp_config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _build_server_config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _build_global_server_config
# ---------------------------------------------------------------------------

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
        # No --project-dir in global mode
        assert "--project-dir" not in config["args"]


# ---------------------------------------------------------------------------
# Claude Desktop injection
# ---------------------------------------------------------------------------

class TestClaudeDesktopInject:
    def test_writes_correct_json_format(self, tmp_path, monkeypatch):
        """Claude Desktop config uses command+args with --project-dir, NOT cwd."""
        import mcp_server.ide_inject as ide_inject
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
        # Claude Desktop does NOT use cwd
        assert "cwd" not in entry
        # Claude Desktop does NOT use url
        assert "url" not in entry

    def test_preserves_existing_desktop_preferences(self, tmp_path, monkeypatch):
        """Existing entries in claude_desktop_config.json must be preserved."""
        import mcp_server.ide_inject as ide_inject
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
        # Existing entries preserved
        assert data["globalShortcut"] == "Ctrl+Shift+C"
        assert "other-mcp" in data["mcpServers"]
        # New entry added
        assert "codevira" in data["mcpServers"]

    def test_full_binary_path_required(self, tmp_path, monkeypatch):
        """Claude Desktop requires the full absolute binary path."""
        import mcp_server.ide_inject as ide_inject
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


# ---------------------------------------------------------------------------
# Global mode injection
# ---------------------------------------------------------------------------

class TestGlobalModeInject:
    def test_global_claude_code_has_no_project_path(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "settings.json"
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

        inject_global_claude_code("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry
        assert "--project-dir" not in str(entry)

    def test_global_cursor_has_no_project_path(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(ide_inject, "_cursor_global_config_path", lambda: config_file)

        inject_global_cursor("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_windsurf_has_no_project_path(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_windsurf_global_config_path", lambda: config_file)

        inject_global_windsurf("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["args"] == []
        assert "cwd" not in entry

    def test_global_inject_preserves_existing(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "settings.json"
        config_file.write_text(json.dumps({
            "mcpServers": {"some-other": {"command": "other"}}
        }))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

        inject_global_claude_code("/usr/bin/codevira-mcp", "python3")

        data = json.loads(config_file.read_text())
        assert "some-other" in data["mcpServers"]
        assert "codevira" in data["mcpServers"]


# ---------------------------------------------------------------------------
# HTTP URL injection
# ---------------------------------------------------------------------------

class TestHttpUrlInject:
    def test_writes_url_format(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "settings.json"
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

        inject_claude_http_url("https://localhost:7443/mcp")

        data = json.loads(config_file.read_text())
        entry = data["mcpServers"]["codevira"]
        assert entry["url"] == "https://localhost:7443/mcp"
        # URL format should NOT have command or args
        assert "command" not in entry
        assert "args" not in entry

    def test_preserves_existing_on_http_inject(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "settings.json"
        config_file.write_text(json.dumps({
            "mcpServers": {"other": {"url": "http://other:8080"}}
        }))
        monkeypatch.setattr(ide_inject, "_claude_global_config_path", lambda: config_file)

        inject_claude_http_url("https://localhost:7443/mcp")

        data = json.loads(config_file.read_text())
        assert "other" in data["mcpServers"]
        assert data["mcpServers"]["codevira"]["url"] == "https://localhost:7443/mcp"


# ---------------------------------------------------------------------------
# Antigravity server name sanitization
# ---------------------------------------------------------------------------

class TestAntiraviryNameSanitization:
    def test_special_chars_removed(self, tmp_path, monkeypatch):
        """Server name must be safe: special chars become hyphens."""
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "my@project/2024")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert len(keys) == 1
        server_name = keys[0]
        # Must start with codevira-
        assert server_name.startswith("codevira-")
        # No special chars
        import re
        safe_part = server_name[len("codevira-"):]
        assert re.match(r"^[a-z0-9-]+$", safe_part), f"Unsafe chars in '{safe_part}'"

    def test_spaces_become_hyphens(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "My Cool Project")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert " " not in keys[0]

    def test_no_double_hyphens(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "proj--name__test")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert "--" not in keys[0]

    def test_uppercase_lowercased(self, tmp_path, monkeypatch):
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "UPPER_CASE")

        data = json.loads(config_file.read_text())
        keys = list(data["mcpServers"].keys())
        assert keys[0] == keys[0].lower()

    def test_antigravity_uses_project_dir_not_cwd(self, tmp_path, monkeypatch):
        """Antigravity always uses --project-dir (no cwd support)."""
        import mcp_server.ide_inject as ide_inject
        config_file = tmp_path / "mcp_config.json"
        monkeypatch.setattr(ide_inject, "_antigravity_config_path", lambda: config_file)

        project = tmp_path / "proj"
        project.mkdir()
        ide_inject._inject_antigravity(project, "/usr/bin/codevira-mcp", "python3", "myproj")

        data = json.loads(config_file.read_text())
        entry = list(data["mcpServers"].values())[0]
        assert "--project-dir" in entry["args"]
        assert "cwd" not in entry


# ---------------------------------------------------------------------------
# _read_json_safe / _write_json_safe
# ---------------------------------------------------------------------------

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
