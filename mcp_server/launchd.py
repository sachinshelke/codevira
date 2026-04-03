"""
launchd.py — macOS launchd service management for Codevira MCP HTTP server.

Generates a launchd plist so that `codevira-mcp serve` starts automatically
on login and stays running as a background service.

Usage (via CLI):
    codevira-mcp serve --install-service    # install + load
    codevira-mcp serve --uninstall-service  # unload + remove

This is macOS-only. Windows and Linux service support is planned for v2.0.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PLIST_LABEL = "com.codevira.mcp-serve"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"


def install_launchd(
    port: int = 7007,
    use_https: bool = False,
    host: str = "127.0.0.1",
) -> Path:
    """Generate and load a launchd plist for the Codevira MCP HTTP server.

    The plist starts `codevira-mcp serve` on login with the given options.
    Logs go to ~/Library/Logs/codevira-mcp.log.

    Args:
        port:      TCP port for the server (default: 7007).
        use_https: If True, adds --https flag (requires mkcert CA to be trusted).
        host:      Bind address (default: 127.0.0.1).

    Returns:
        Path to the installed plist file.

    Raises:
        RuntimeError on non-macOS platforms or if launchctl fails.
    """
    if sys.platform != "darwin":
        raise RuntimeError("launchd auto-start is only supported on macOS.")

    from mcp_server.ide_inject import _resolve_command
    cmd_path, _ = _resolve_command()

    args = [cmd_path, "serve", "--host", host, "--port", str(port)]
    if use_https:
        args.append("--https")

    log_dir = Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "codevira-mcp.log"

    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        {"".join(f"<string>{a}</string>" + chr(10) + "        " for a in args).rstrip()}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""

    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Unload existing service if present (ignore errors)
    subprocess.run(
        ["launchctl", "unload", str(_PLIST_PATH)],
        capture_output=True,
    )

    _PLIST_PATH.write_text(plist_content, encoding="utf-8")
    logger.info("Wrote launchd plist: %s", _PLIST_PATH)

    # Load the new service
    result = subprocess.run(
        ["launchctl", "load", str(_PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl load failed:\n{result.stderr or result.stdout}"
        )

    logger.info("Launchd service loaded: %s", _PLIST_LABEL)
    return _PLIST_PATH


def uninstall_launchd() -> bool:
    """Unload and remove the Codevira launchd plist.

    Returns:
        True if the service was removed, False if it wasn't installed.

    Raises:
        RuntimeError on non-macOS platforms.
    """
    if sys.platform != "darwin":
        raise RuntimeError("launchd management is only supported on macOS.")

    if not _PLIST_PATH.exists():
        return False

    subprocess.run(
        ["launchctl", "unload", str(_PLIST_PATH)],
        capture_output=True,
    )
    _PLIST_PATH.unlink(missing_ok=True)
    logger.info("Launchd service removed: %s", _PLIST_LABEL)
    return True


def launchd_status() -> dict:
    """Return the current status of the launchd service."""
    if sys.platform != "darwin":
        return {"platform": "not_macos", "installed": False}

    installed = _PLIST_PATH.exists()
    running = False
    if installed:
        result = subprocess.run(
            ["launchctl", "list", _PLIST_LABEL],
            capture_output=True,
            text=True,
        )
        running = result.returncode == 0

    return {
        "installed": installed,
        "running": running,
        "plist_path": str(_PLIST_PATH) if installed else None,
        "label": _PLIST_LABEL,
    }
