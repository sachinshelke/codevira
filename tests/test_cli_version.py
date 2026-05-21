"""Test for `codevira --version` flag — Bug 17 regression guard.

Sachin tried to verify the installed version with ``codevira --version``
and got ``error: unrecognized arguments: --version``. Every standard
Python CLI exposes a version flag; codevira should too. This test
ensures it stays wired to ``mcp_server.__version__`` so a single bump
to ``__init__.py`` updates both ``--version`` output and the package
metadata.
"""
from __future__ import annotations

import subprocess
import sys


from mcp_server import __version__ as expected_version


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run codevira CLI as a subprocess to exercise the real argv path."""
    return subprocess.run(
        [sys.executable, "-m", "mcp_server", *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestVersionFlag:
    def test_long_form(self):
        result = _run_cli("--version")
        # argparse prints version to stdout (or stderr depending on Python
        # version). Accept either.
        out = (result.stdout + result.stderr)
        assert expected_version in out
        assert "codevira" in out
        assert result.returncode == 0

    def test_short_form(self):
        result = _run_cli("-V")
        out = (result.stdout + result.stderr)
        assert expected_version in out
        assert result.returncode == 0

    def test_version_string_is_not_dev_or_unknown(self):
        """Defense: catch the case where __version__ accidentally becomes
        '0.0.0' or 'dev' due to a refactor."""
        assert expected_version
        assert expected_version not in ("0.0.0", "dev", "unknown")
        assert expected_version[0].isdigit()
