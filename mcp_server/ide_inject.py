"""
ide_inject.py — Auto-detect installed AI tools and inject MCP configuration.

Detects Claude Code, Cursor, Windsurf, and Google Antigravity, then writes
the correct MCP server config to each tool's settings file. Non-destructive
merge: only touches the 'codevira' entry, preserves everything else.
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IDE detection
# ---------------------------------------------------------------------------

def detect_installed_ides(project_root: Path) -> list[str]:
    """Detect which AI coding tools are installed."""
    found: list[str] = []

    # Claude Code: per-project .claude/ or claude binary in PATH
    if (project_root / ".claude").is_dir() or shutil.which("claude"):
        found.append("claude")

    # Cursor: global ~/.cursor/ or cursor binary
    if (Path.home() / ".cursor").is_dir() or shutil.which("cursor"):
        found.append("cursor")

    # Windsurf: global ~/.windsurf/ or ~/.codeium/windsurf/
    if (Path.home() / ".windsurf").is_dir() or (Path.home() / ".codeium" / "windsurf").is_dir():
        found.append("windsurf")

    # Google Antigravity: global ~/.gemini/
    if (Path.home() / ".gemini").is_dir():
        found.append("antigravity")

    return found


# ---------------------------------------------------------------------------
# Config file paths
# ---------------------------------------------------------------------------

def _claude_config_path(project_root: Path) -> Path:
    return project_root / ".claude" / "settings.json"

def _cursor_config_path(project_root: Path) -> Path:
    return project_root / ".cursor" / "mcp.json"

def _windsurf_config_path(project_root: Path) -> Path:
    return project_root / ".windsurf" / "mcp.json"

def _antigravity_config_path() -> Path:
    return Path.home() / ".gemini" / "settings" / "mcp_config.json"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _read_json_safe(path: Path) -> dict:
    """Read a JSON file, returning {} if missing or corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Could not parse %s: %s (will create fresh)", path, e)
        return {}


def _write_json_safe(path: Path, data: dict) -> None:
    """Atomic write: write to .tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # Atomic on POSIX, best-effort on Windows


def _merge_mcp_config(existing: dict, server_name: str, server_config: dict) -> dict:
    """Non-destructive merge: only touch the server_name entry."""
    result = json.loads(json.dumps(existing))  # deep copy
    if "mcpServers" not in result:
        result["mcpServers"] = {}
    result["mcpServers"][server_name] = server_config
    return result


# ---------------------------------------------------------------------------
# Resolve the best command to run codevira-mcp
# ---------------------------------------------------------------------------

def _resolve_command() -> tuple[str, str]:
    """
    Returns (cmd_path, python_exe).
    cmd_path is the absolute path to codevira-mcp if found, else 'codevira-mcp'.
    python_exe is the current Python interpreter.
    """
    exe = shutil.which("codevira-mcp")
    cmd_path = exe if exe else "codevira-mcp"
    python_exe = sys.executable
    return cmd_path, python_exe


# ---------------------------------------------------------------------------
# Per-IDE injection
# ---------------------------------------------------------------------------

def _inject_claude(project_root: Path, cmd_path: str) -> str | None:
    """Inject MCP config into Claude Code settings."""
    config_path = _claude_config_path(project_root)
    existing = _read_json_safe(config_path)

    server_config = {
        "command": cmd_path,
        "cwd": str(project_root),
    }

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_cursor(project_root: Path, cmd_path: str) -> str | None:
    """Inject MCP config into Cursor settings."""
    config_path = _cursor_config_path(project_root)
    existing = _read_json_safe(config_path)

    server_config = {
        "command": cmd_path,
        "cwd": str(project_root),
    }

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_windsurf(project_root: Path, cmd_path: str) -> str | None:
    """Inject MCP config into Windsurf settings."""
    config_path = _windsurf_config_path(project_root)
    existing = _read_json_safe(config_path)

    server_config = {
        "command": cmd_path,
        "cwd": str(project_root),
    }

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_antigravity(project_root: Path, python_exe: str, project_name: str) -> str | None:
    """Inject MCP config into Google Antigravity settings (global, unique server name)."""
    config_path = _antigravity_config_path()
    existing = _read_json_safe(config_path)

    # Unique server name to avoid collisions across projects
    safe_name = project_name.lower().replace(" ", "-").replace("_", "-")
    server_name = f"codevira-{safe_name}"

    server_config = {
        "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
        "command": python_exe,
        "args": ["-m", "mcp_server", "--project-dir", str(project_root)],
    }

    merged = _merge_mcp_config(existing, server_name, server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def inject_ide_config(project_root: Path, project_name: str = "") -> dict[str, str]:
    """
    Detect installed AI tools and auto-inject MCP configuration.

    Returns dict of {ide_name: config_path_written} for each configured tool.
    """
    project_root = project_root.resolve()
    if not project_name:
        project_name = project_root.name

    cmd_path, python_exe = _resolve_command()
    ides = detect_installed_ides(project_root)
    results: dict[str, str] = {}

    for ide in ides:
        try:
            if ide == "claude":
                path = _inject_claude(project_root, cmd_path)
                if path:
                    results["Claude Code"] = path

            elif ide == "cursor":
                path = _inject_cursor(project_root, cmd_path)
                if path:
                    results["Cursor"] = path

            elif ide == "windsurf":
                path = _inject_windsurf(project_root, cmd_path)
                if path:
                    results["Windsurf"] = path

            elif ide == "antigravity":
                path = _inject_antigravity(project_root, python_exe, project_name)
                if path:
                    results["Antigravity"] = path

        except Exception as e:
            logger.warning("Failed to inject %s config: %s", ide, e)

    return results
