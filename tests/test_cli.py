"""
Tests for mcp_server/cli.py

Covers:
  - _set_project_dir_early(): parse --project-dir from raw args
  - _detect_project_root_markers(): check for project root markers
  - main(): argument parsing and subcommand dispatch
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.cli import _set_project_dir_early, _detect_project_root_markers


# ---------------------------------------------------------------------------
# _set_project_dir_early
# ---------------------------------------------------------------------------

class TestSetProjectDirEarly:
    """Parse --project-dir from a raw args list."""

    def test_space_separated(self):
        result = _set_project_dir_early(["--project-dir", "/tmp/foo"])
        assert result == Path("/tmp/foo").resolve()

    def test_equals_separated(self):
        result = _set_project_dir_early(["--project-dir=/tmp/foo"])
        assert result == Path("/tmp/foo").resolve()

    def test_no_project_dir(self):
        result = _set_project_dir_early(["init"])
        assert result is None

    def test_flag_without_value(self):
        result = _set_project_dir_early(["--project-dir"])
        assert result is None

    def test_flag_at_end_of_args(self):
        """--project-dir as the last arg with no following value."""
        result = _set_project_dir_early(["init", "--project-dir"])
        assert result is None

    def test_empty_args(self):
        result = _set_project_dir_early([])
        assert result is None

    def test_mixed_args(self):
        """--project-dir buried among other flags."""
        result = _set_project_dir_early(
            ["serve", "--port", "8080", "--project-dir", "/tmp/bar", "--https"]
        )
        assert result == Path("/tmp/bar").resolve()


# ---------------------------------------------------------------------------
# _detect_project_root_markers
# ---------------------------------------------------------------------------

class TestDetectProjectRootMarkers:
    """Check whether a directory contains project root markers."""

    def test_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _detect_project_root_markers(tmp_path) is True

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert _detect_project_root_markers(tmp_path) is True

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='test'\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_empty_dir(self, tmp_path):
        assert _detect_project_root_markers(tmp_path) is False

    def test_unrelated_files(self, tmp_path):
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / "notes.txt").write_text("hello")
        assert _detect_project_root_markers(tmp_path) is False


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

# All subcommand handlers that main() dispatches to.
_PATCH_TARGETS = [
    "mcp_server.cli.cmd_init",
    "mcp_server.cli.cmd_index",
    "mcp_server.cli.cmd_status",
    "mcp_server.cli.cmd_report",
    "mcp_server.cli.cmd_serve",
    "mcp_server.cli.cmd_register",
    "mcp_server.cli.cmd_server",
]


def _make_patches():
    """Return a dict of (target_name -> patcher) for all subcommand handlers."""
    return {t: patch(t, new_callable=MagicMock) for t in _PATCH_TARGETS}


class TestMainDispatch:
    """Verify that main() routes sys.argv to the correct subcommand handler."""

    def _run_main(self, argv_list: list[str], mocks: dict):
        """Invoke main() with the given argv and all handlers mocked."""
        from mcp_server.cli import main

        with patch.object(sys, "argv", ["codevira-mcp"] + argv_list):
            # Also patch set_project_dir to avoid side effects
            with patch("mcp_server.cli.set_project_dir", create=True):
                main()
        return mocks

    def test_init(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["init"], mocks)
            mocks["mcp_server.cli.cmd_init"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_index_full(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["index", "--full"], mocks)
            mocks["mcp_server.cli.cmd_index"].assert_called_once_with(
                full=True, quiet=False
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_status(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["status"], mocks)
            mocks["mcp_server.cli.cmd_status"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_report_clear(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["report", "--clear"], mocks)
            mocks["mcp_server.cli.cmd_report"].assert_called_once_with(
                limit=20, clear=True
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_register(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["register"], mocks)
            mocks["mcp_server.cli.cmd_register"].assert_called_once_with(
                claude_desktop=False, http_url=None
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_register_http_url(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(
                ["register", "--http-url", "https://localhost:7443/mcp"], mocks
            )
            mocks["mcp_server.cli.cmd_register"].assert_called_once_with(
                claude_desktop=False, http_url="https://localhost:7443/mcp"
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_serve_with_port_and_https(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["serve", "--port", "8080", "--https"], mocks)
            call_kwargs = mocks["mcp_server.cli.cmd_serve"].call_args
            assert call_kwargs[1]["port"] == 8080 or call_kwargs.kwargs["port"] == 8080
            assert call_kwargs[1].get("use_https") is True or call_kwargs.kwargs.get("use_https") is True
        finally:
            for p in patchers.values():
                p.stop()

    def test_no_subcommand_defaults_to_server(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main([], mocks)
            mocks["mcp_server.cli.cmd_server"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_index_without_full(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["index"], mocks)
            mocks["mcp_server.cli.cmd_index"].assert_called_once_with(
                full=False, quiet=False
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_report_default(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["report"], mocks)
            mocks["mcp_server.cli.cmd_report"].assert_called_once_with(
                limit=20, clear=False
            )
        finally:
            for p in patchers.values():
                p.stop()
