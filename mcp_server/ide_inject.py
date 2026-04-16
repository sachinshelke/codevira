"""
ide_inject.py — Auto-detect installed AI tools and inject MCP configuration.

Detects Claude Code, Claude Desktop, Cursor, Windsurf, and Google Antigravity,
then writes the correct MCP server config to each tool's settings file.
Non-destructive merge: only touches the 'codevira' entry, preserves everything else.

v1.6 additions:
  - Claude Desktop support (stdio-only, requires full binary path + --project-dir)
  - Global mode: inject once with no project path, works for every project
  - HTTP URL injection for Claude Code CLI
  - Windows cross-platform fix for sysconfig path resolution
  - Antigravity server name sanitization (handles special chars)
"""
from __future__ import annotations

import json
import logging
import re
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

    # Claude Desktop: check for its config directory
    desktop_cfg_dir = _claude_desktop_config_path().parent
    if desktop_cfg_dir.exists():
        found.append("claude_desktop")

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

def _claude_global_config_path() -> Path:
    return Path.home() / ".claude" / "settings.json"

def _claude_desktop_config_path() -> Path:
    """Return the Claude Desktop config file path (platform-aware)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        appdata = Path(sys.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return appdata / "Claude" / "claude_desktop_config.json"
    else:
        # Linux / other
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

def _cursor_config_path(project_root: Path) -> Path:
    return project_root / ".cursor" / "mcp.json"

def _cursor_global_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"

def _windsurf_config_path(project_root: Path) -> Path:
    return project_root / ".windsurf" / "mcp.json"

def _windsurf_global_config_path() -> Path:
    """Return global Windsurf MCP config path."""
    if (Path.home() / ".codeium" / "windsurf").is_dir():
        return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    return Path.home() / ".windsurf" / "mcp_config.json"

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
# Resolve the best command to run codevira
# ---------------------------------------------------------------------------

def _resolve_command() -> tuple[str, str]:
    """
    Returns (cmd_path, python_exe).
    cmd_path is the absolute path to codevira binary.
    python_exe is the Python interpreter that runs this process.

    Search order for the binary:
      1. shutil.which (works when ~/.local/bin is in PATH)
      2. pipx default venv location  ~/.local/pipx/venvs/codevira/bin/
      3. pip --user install location  ~/Library/Python/X.Y/bin/ (macOS) or %APPDATA% (Windows)
      4. Same bin dir as current Python interpreter
      5. Fallback: run as `python -m mcp_server` using current interpreter
    """
    python_exe = sys.executable

    # 1. Standard PATH lookup
    exe = shutil.which("codevira")
    if exe:
        return exe, python_exe

    # 2. pipx default venv
    pipx_bin = Path.home() / ".local" / "pipx" / "venvs" / "codevira" / "bin" / "codevira"
    if sys.platform == "win32":
        pipx_bin = Path.home() / ".local" / "pipx" / "venvs" / "codevira" / "Scripts" / "codevira.exe"
    if pipx_bin.exists():
        return str(pipx_bin), python_exe

    # 3. pip --user install location (cross-platform)
    try:
        import sysconfig
        if sys.platform == "win32":
            scripts_scheme = "nt_user"
        else:
            scripts_scheme = "posix_user"
        user_scripts = sysconfig.get_path("scripts", scripts_scheme)
        if user_scripts:
            suffix = ".exe" if sys.platform == "win32" else ""
            user_bin = Path(user_scripts) / f"codevira{suffix}"
            if user_bin.exists():
                return str(user_bin), python_exe
    except Exception:
        pass

    # 4. Same bin dir as current Python
    suffix = ".exe" if sys.platform == "win32" else ""
    sibling_bin = Path(python_exe).parent / f"codevira{suffix}"
    if sibling_bin.exists():
        return str(sibling_bin), python_exe

    # 5. Fallback: use current interpreter with -m flag (always works)
    return python_exe, python_exe


# ---------------------------------------------------------------------------
# Per-IDE injection (per-project mode)
# ---------------------------------------------------------------------------

def _build_server_config(cmd_path: str, python_exe: str, project_root: Path, use_cwd: bool = True) -> dict:
    """
    Build the MCP server config dict for per-project mode.

    If cmd_path is the Python interpreter (fallback), use `-m mcp_server --project-dir`.
    If cmd_path is the codevira binary:
      - use_cwd=True:  {"command": ..., "args": [], "cwd": ...}   (Claude / Cursor / Windsurf)
      - use_cwd=False: {"command": ..., "args": ["--project-dir", ...]}  (tools that ignore cwd)
    """
    is_python_fallback = (cmd_path == python_exe)

    if is_python_fallback:
        return {
            "command": cmd_path,
            "args": ["-m", "mcp_server", "--project-dir", str(project_root)],
        }

    if use_cwd:
        return {
            "command": cmd_path,
            "args": [],
            "cwd": str(project_root),
        }
    else:
        return {
            "command": cmd_path,
            "args": ["--project-dir", str(project_root)],
        }


def _build_global_server_config(cmd_path: str, python_exe: str) -> dict:
    """
    Build the MCP server config dict for global mode (v1.6).

    Global mode: no project path — the server detects the project from cwd
    when each AI tool opens a project. Works for every project automatically.
    """
    is_python_fallback = (cmd_path == python_exe)
    if is_python_fallback:
        return {"command": cmd_path, "args": ["-m", "mcp_server"]}
    return {"command": cmd_path, "args": []}


def _inject_claude(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Claude Code per-project settings."""
    config_path = _claude_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(cmd_path, python_exe, project_root, use_cwd=True)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_claude_desktop(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Claude Desktop (stdio-only, requires full binary path).

    Claude Desktop:
      - Does NOT support the 'url' format — only 'command' + 'args'
      - Does NOT support 'cwd' — must use '--project-dir' arg
      - Requires the FULL absolute binary path (not just 'codevira')
    """
    config_path = _claude_desktop_config_path()
    existing = _read_json_safe(config_path)

    # Always use --project-dir for Claude Desktop (no cwd support)
    server_config = _build_server_config(cmd_path, python_exe, project_root, use_cwd=False)

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_cursor(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Cursor per-project settings."""
    config_path = _cursor_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(cmd_path, python_exe, project_root, use_cwd=True)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_windsurf(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Windsurf per-project settings."""
    config_path = _windsurf_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(cmd_path, python_exe, project_root, use_cwd=True)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_antigravity(project_root: Path, cmd_path: str, python_exe: str, project_name: str) -> str | None:
    """Inject MCP config into Google Antigravity settings (global file, unique server name per project).

    Antigravity does not support 'cwd', so always use --project-dir args.
    """
    config_path = _antigravity_config_path()
    existing = _read_json_safe(config_path)

    # Sanitize project name: lowercase, replace anything non-alphanumeric with hyphens
    safe_name = re.sub(r"[^a-z0-9-]", "-", project_name.lower())
    safe_name = re.sub(r"-{2,}", "-", safe_name).strip("-")
    server_name = f"codevira-{safe_name}"

    base_config = _build_server_config(cmd_path, python_exe, project_root, use_cwd=False)
    server_config = {"$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate", **base_config}

    merged = _merge_mcp_config(existing, server_name, server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


# ---------------------------------------------------------------------------
# Global mode injection (v1.6) — one-time, no project path
# ---------------------------------------------------------------------------

def inject_global_claude_code(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Claude Code (~/.claude/settings.json).

    Global mode: no cwd, no --project-dir. The server auto-detects the project
    from cwd when Claude Code opens each project directory.
    """
    config_path = _claude_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = _build_global_server_config(cmd_path, python_exe)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_global_cursor(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Cursor (~/.cursor/mcp.json)."""
    config_path = _cursor_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = _build_global_server_config(cmd_path, python_exe)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_global_windsurf(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Windsurf."""
    config_path = _windsurf_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = _build_global_server_config(cmd_path, python_exe)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_claude_http_url(url: str) -> str | None:
    """Inject HTTP URL config into Claude Code global settings.

    Only for Claude Code CLI — Cursor/Windsurf do not support URL format.
    Claude Desktop does not support URL format either (stdio only).

    Args:
        url: Full MCP URL e.g. 'https://localhost:7443/mcp'
    """
    config_path = _claude_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = {"url": url}
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


# ---------------------------------------------------------------------------
# Main orchestrators
# ---------------------------------------------------------------------------

def inject_ide_config(
    project_root: Path,
    project_name: str = "",
    global_mode: bool = False,
) -> dict[str, str]:
    """
    Detect installed AI tools and auto-inject MCP configuration.

    Args:
        project_root: Project directory (used in per-project mode).
        project_name: Display name for the project.
        global_mode: If True, inject global config (no project path) instead of
                     per-project config. Use for 'codevira register'.

    Returns:
        Dict of {ide_name: config_path_written} for each configured tool.
    """
    project_root = project_root.resolve()
    if not project_name:
        project_name = project_root.name

    cmd_path, python_exe = _resolve_command()
    ides = detect_installed_ides(project_root)
    results: dict[str, str] = {}

    for ide in ides:
        try:
            if global_mode:
                # Global mode: register once, works for every project
                if ide == "claude":
                    path = inject_global_claude_code(cmd_path, python_exe)
                    if path:
                        results["Claude Code (global)"] = path
                elif ide == "cursor":
                    path = inject_global_cursor(cmd_path, python_exe)
                    if path:
                        results["Cursor (global)"] = path
                elif ide == "windsurf":
                    path = inject_global_windsurf(cmd_path, python_exe)
                    if path:
                        results["Windsurf (global)"] = path
                elif ide == "claude_desktop":
                    # Claude Desktop always needs --project-dir (no cwd + no url support)
                    # In global mode, skip Claude Desktop (it can't do project-agnostic config)
                    pass
                elif ide == "antigravity":
                    # Antigravity always uses per-project keys; skip in global mode
                    pass
            else:
                # Per-project mode (existing behavior)
                if ide == "claude":
                    path = _inject_claude(project_root, cmd_path, python_exe)
                    if path:
                        results["Claude Code"] = path
                elif ide == "claude_desktop":
                    path = _inject_claude_desktop(project_root, cmd_path, python_exe)
                    if path:
                        results["Claude Desktop"] = path
                elif ide == "cursor":
                    path = _inject_cursor(project_root, cmd_path, python_exe)
                    if path:
                        results["Cursor"] = path
                elif ide == "windsurf":
                    path = _inject_windsurf(project_root, cmd_path, python_exe)
                    if path:
                        results["Windsurf"] = path
                elif ide == "antigravity":
                    path = _inject_antigravity(project_root, cmd_path, python_exe, project_name)
                    if path:
                        results["Antigravity"] = path

        except Exception as e:
            logger.warning("Failed to inject %s config: %s", ide, e)
            try:
                from mcp_server.crash_logger import log_crash
                log_crash(e, context=f"IDE config inject: {ide}",
                          project_path=str(project_root))
            except Exception:
                pass

    return results
