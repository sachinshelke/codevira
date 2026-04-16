"""
Tests for mcp_server/launchd.py — macOS launchd service management.

Mocks sys.platform, subprocess.run, _PLIST_PATH, and _resolve_command
to avoid real system interactions.
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from mcp_server.launchd import (
    install_launchd,
    uninstall_launchd,
    launchd_status,
    _PLIST_LABEL,
)


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def mock_darwin():
    """Patch sys.platform to 'darwin' for macOS tests."""
    with patch("mcp_server.launchd.sys") as mock_sys:
        mock_sys.platform = "darwin"
        yield mock_sys


@pytest.fixture
def mock_plist_path(tmp_path):
    """Redirect _PLIST_PATH to a temp directory so we can inspect the plist."""
    plist_file = tmp_path / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
    plist_file.parent.mkdir(parents=True, exist_ok=True)
    with patch("mcp_server.launchd._PLIST_PATH", plist_file):
        yield plist_file


@pytest.fixture
def mock_resolve_command():
    """Mock _resolve_command at the source module (it's imported lazily inside install_launchd)."""
    with patch("mcp_server.ide_inject._resolve_command",
               return_value=("/usr/local/bin/codevira", "codevira")):
        yield


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run to prevent real launchctl calls."""
    with patch("mcp_server.launchd.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield mock_run


# ---------------------------------------------------------------
# install_launchd()
# ---------------------------------------------------------------

class TestInstallLaunchd:
    def test_generates_valid_plist(self, mock_darwin, mock_plist_path,
                                   mock_resolve_command, mock_subprocess):
        """install_launchd writes a valid plist file and calls launchctl."""
        result_path = install_launchd(port=7007)

        assert result_path == mock_plist_path
        assert mock_plist_path.exists()

        # Parse the plist back
        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert plist["Label"] == _PLIST_LABEL
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] is True

    def test_calls_launchctl_with_timeout(self, mock_darwin, mock_plist_path,
                                           mock_resolve_command, mock_subprocess):
        """install_launchd calls launchctl unload (cleanup) then launchctl load."""
        install_launchd()

        calls = mock_subprocess.call_args_list
        # First call: unload existing (cleanup)
        assert calls[0][0][0] == ["launchctl", "unload", str(mock_plist_path)]
        assert calls[0][1]["timeout"] == 10
        # Second call: load new service
        assert calls[1][0][0] == ["launchctl", "load", str(mock_plist_path)]
        assert calls[1][1]["timeout"] == 10

    def test_plist_content_validates(self, mock_darwin, mock_plist_path,
                                      mock_resolve_command, mock_subprocess):
        """Plist has all required keys: Label, ProgramArguments, RunAtLoad,
        KeepAlive, StandardOutPath, StandardErrorPath."""
        install_launchd(port=8080, host="0.0.0.0")

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        required_keys = {"Label", "ProgramArguments", "RunAtLoad",
                         "KeepAlive", "StandardOutPath", "StandardErrorPath"}
        assert required_keys.issubset(set(plist.keys()))

    def test_program_arguments_contain_serve_flags(self, mock_darwin, mock_plist_path,
                                                    mock_resolve_command, mock_subprocess):
        """ProgramArguments contains the command path plus serve, --host, --port flags."""
        install_launchd(port=9090, host="0.0.0.0")

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        args = plist["ProgramArguments"]
        assert args[0] == "/usr/local/bin/codevira"
        assert "serve" in args
        assert "--host" in args
        assert "0.0.0.0" in args
        assert "--port" in args
        assert "9090" in args

    def test_https_flag_appended(self, mock_darwin, mock_plist_path,
                                  mock_resolve_command, mock_subprocess):
        """When use_https=True, --https flag is appended to ProgramArguments."""
        install_launchd(use_https=True)

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert "--https" in plist["ProgramArguments"]

    def test_no_https_flag_by_default(self, mock_darwin, mock_plist_path,
                                       mock_resolve_command, mock_subprocess):
        """When use_https=False (default), --https is NOT in ProgramArguments."""
        install_launchd(use_https=False)

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert "--https" not in plist["ProgramArguments"]

    def test_custom_host_and_port(self, mock_darwin, mock_plist_path,
                                   mock_resolve_command, mock_subprocess):
        """Custom host and port are reflected in ProgramArguments."""
        install_launchd(port=3000, host="192.168.1.10")

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        args = plist["ProgramArguments"]
        assert "192.168.1.10" in args
        assert "3000" in args

    def test_launchctl_failure_raises_runtime_error(self, mock_darwin, mock_plist_path,
                                                     mock_resolve_command, mock_subprocess):
        """If launchctl load fails, a RuntimeError is raised."""
        # First call (unload) succeeds, second call (load) fails
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # unload
            MagicMock(returncode=1, stderr="service already loaded", stdout=""),  # load
        ]

        with pytest.raises(RuntimeError, match="launchctl load failed"):
            install_launchd()

    def test_project_dir_adds_flag_and_working_directory(
        self, mock_darwin, mock_plist_path, mock_resolve_command, mock_subprocess
    ):
        """When project_dir is provided, plist includes --project-dir and WorkingDirectory."""
        install_launchd(project_dir=Path("/tmp/my-project"))

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        args = plist["ProgramArguments"]
        assert "--project-dir" in args
        assert "/tmp/my-project" in args
        assert plist["WorkingDirectory"] == "/tmp/my-project"

    def test_no_project_dir_omits_working_directory(
        self, mock_darwin, mock_plist_path, mock_resolve_command, mock_subprocess
    ):
        """When project_dir is omitted, plist has no WorkingDirectory or --project-dir."""
        install_launchd()

        with open(mock_plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert "WorkingDirectory" not in plist
        assert "--project-dir" not in plist["ProgramArguments"]


# ---------------------------------------------------------------
# uninstall_launchd()
# ---------------------------------------------------------------

class TestUninstallLaunchd:
    def test_calls_launchctl_unload_and_removes_plist(self, mock_darwin, mock_plist_path,
                                                       mock_subprocess):
        """uninstall_launchd calls launchctl unload and deletes the plist file."""
        # Create the plist file so uninstall finds it
        mock_plist_path.write_bytes(b"fake plist content")
        assert mock_plist_path.exists()

        result = uninstall_launchd()

        assert result is True
        assert not mock_plist_path.exists()
        mock_subprocess.assert_called_once_with(
            ["launchctl", "unload", str(mock_plist_path)],
            capture_output=True,
            timeout=10,
        )

    def test_uninstall_when_not_installed_returns_false(self, mock_darwin, mock_plist_path,
                                                         mock_subprocess):
        """If plist file doesn't exist, uninstall returns False without calling launchctl."""
        # Ensure plist does NOT exist
        if mock_plist_path.exists():
            mock_plist_path.unlink()

        result = uninstall_launchd()

        assert result is False
        mock_subprocess.assert_not_called()


# ---------------------------------------------------------------
# launchd_status()
# ---------------------------------------------------------------

class TestLaunchdStatus:
    def test_returns_installed_running(self, mock_darwin, mock_plist_path, mock_subprocess):
        """When plist exists and launchctl list succeeds, returns installed+running."""
        mock_plist_path.write_bytes(b"plist")
        mock_subprocess.return_value = MagicMock(returncode=0)

        status = launchd_status()

        assert status["installed"] is True
        assert status["running"] is True
        assert status["plist_path"] == str(mock_plist_path)
        assert status["label"] == _PLIST_LABEL

    def test_installed_but_not_running(self, mock_darwin, mock_plist_path, mock_subprocess):
        """When plist exists but launchctl list fails, installed=True but running=False."""
        mock_plist_path.write_bytes(b"plist")
        mock_subprocess.return_value = MagicMock(returncode=1)

        status = launchd_status()

        assert status["installed"] is True
        assert status["running"] is False

    def test_not_installed(self, mock_darwin, mock_plist_path, mock_subprocess):
        """When plist doesn't exist, installed=False, running=False, plist_path=None."""
        if mock_plist_path.exists():
            mock_plist_path.unlink()

        status = launchd_status()

        assert status["installed"] is False
        assert status["running"] is False
        assert status["plist_path"] is None
        assert status["label"] == _PLIST_LABEL

    def test_calls_launchctl_list_with_label(self, mock_darwin, mock_plist_path, mock_subprocess):
        """launchd_status calls launchctl list with the correct label."""
        mock_plist_path.write_bytes(b"plist")

        launchd_status()

        mock_subprocess.assert_called_once_with(
            ["launchctl", "list", _PLIST_LABEL],
            capture_output=True,
            text=True,
            timeout=5,
        )


# ---------------------------------------------------------------
# Non-macOS platform
# ---------------------------------------------------------------

class TestNonMacosPlatform:
    def test_install_raises_on_linux(self):
        """install_launchd raises RuntimeError on non-macOS platforms."""
        with patch("mcp_server.launchd.sys") as mock_sys:
            mock_sys.platform = "linux"
            with pytest.raises(RuntimeError, match="only supported on macOS"):
                install_launchd()

    def test_uninstall_raises_on_linux(self):
        """uninstall_launchd raises RuntimeError on non-macOS platforms."""
        with patch("mcp_server.launchd.sys") as mock_sys:
            mock_sys.platform = "linux"
            with pytest.raises(RuntimeError, match="only supported on macOS"):
                uninstall_launchd()

    def test_status_returns_not_macos(self):
        """launchd_status returns platform=not_macos on non-macOS."""
        with patch("mcp_server.launchd.sys") as mock_sys:
            mock_sys.platform = "win32"
            status = launchd_status()

        assert status["platform"] == "not_macos"
        assert status["installed"] is False


# ---------------------------------------------------------------
# launchctl failure handling
# ---------------------------------------------------------------

class TestLaunchctlFailureHandling:
    def test_unload_failure_during_install_is_ignored(self, mock_darwin, mock_plist_path,
                                                       mock_resolve_command, mock_subprocess):
        """The initial unload during install is a cleanup step; failures are ignored."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=3, stderr="not loaded"),  # unload fails (normal)
            MagicMock(returncode=0, stdout="", stderr=""),  # load succeeds
        ]

        # Should NOT raise despite unload failure
        result = install_launchd()
        assert result == mock_plist_path

    def test_load_failure_stderr_in_error_message(self, mock_darwin, mock_plist_path,
                                                    mock_resolve_command, mock_subprocess):
        """RuntimeError from launchctl load includes stderr content."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # unload
            MagicMock(returncode=1, stderr="Bootstrap failed: 5", stdout=""),  # load
        ]

        with pytest.raises(RuntimeError, match="Bootstrap failed: 5"):
            install_launchd()

    def test_load_failure_stdout_fallback(self, mock_darwin, mock_plist_path,
                                           mock_resolve_command, mock_subprocess):
        """When stderr is empty, stdout is used in the error message."""
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # unload
            MagicMock(returncode=1, stderr="", stdout="some stdout error"),  # load
        ]

        with pytest.raises(RuntimeError, match="some stdout error"):
            install_launchd()
