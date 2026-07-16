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
    """Detect which AI coding tools are installed.

    Returns a list of stable string keys identifying each detected
    tool. Keys are consumed by the setup wizard and the doctor.
    Keep additions backward-compatible — existing keys must keep
    their meaning.

    Tier 1 (have specific MCP-config path support): claude,
    claude_desktop, cursor, windsurf, antigravity.

    Tier 2 (AGENTS.md-style integration, no MCP-config injection):
    codex, copilot.

    v3.0.0 detection hardening (2026-05-22+ audit):
    The detection rules below require STRONG signals (binary on PATH
    AND a config file/dir present) wherever possible. The v2.x version
    treated "directory exists at the expected path" as a sufficient
    signal — that produced false positives (stale dirs from previous
    installs, hand-created folders) and we configured IDEs the user
    didn't actually have. Now we cross-check.

    The ``continue`` and ``aider`` v2.x detections were also removed
    in this hardening pass — neither IDE has a configured-for-codevira
    integration path; their entries existed only as advisory listings.
    """
    found: list[str] = []

    # ---- Tier 1 (MCP config support) ----

    # Claude Code: must be installed (binary on PATH). The per-project
    # `.claude/` dir is NOT a reliable signal — many users create one
    # manually for IDE state without having installed Claude Code.
    if shutil.which("claude") is not None:
        found.append("claude")

    # Claude Desktop: require the config FILE to exist (not just the
    # parent dir), and parse as valid JSON. A stale install can leave
    # the directory but the file alone proves the app was set up.
    desktop_cfg = _claude_desktop_config_path()
    if desktop_cfg.is_file() and _is_valid_json(desktop_cfg):
        found.append("claude_desktop")

    # Cursor: directory + (binary OR mcp.json config file). The
    # binary check is the strong signal; the mcp.json fallback
    # covers cases where the user installed Cursor via the .app
    # bundle and didn't add it to PATH.
    cursor_dir = Path.home() / ".cursor"
    if cursor_dir.is_dir() and (
        shutil.which("cursor") is not None or (cursor_dir / "mcp.json").is_file()
    ):
        found.append("cursor")

    # Windsurf: require the actual mcp_config.json to exist (not just
    # the parent directory). Windsurf writes this file the first time
    # the app runs.
    windsurf_paths = (
        Path.home() / ".windsurf" / "mcp_config.json",
        Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
    )
    if any(p.is_file() for p in windsurf_paths):
        found.append("windsurf")

    # Google Antigravity 2.0: the MCP config lives in the shared
    # ~/.gemini/config/ dir and/or the per-app ~/.gemini/antigravity/ dir
    # (not the bare ~/.gemini/, which any Gemini-CLI install creates).
    # Detect either specific config file.
    if any(p.is_file() and _is_valid_json(p) for p in _antigravity_config_candidates()):
        found.append("antigravity")

    # ---- Tier 2 (nudge-file integration only) ----

    # OpenAI Codex CLI: binary on PATH (strong) or AGENTS.md present
    # in the project root (the canonical Codex format).
    if shutil.which("codex") is not None:
        found.append("codex")
    elif (project_root / "AGENTS.md").is_file():
        # Weaker signal — AGENTS.md alone doesn't prove Codex is
        # installed, but it's the standard integration point. Mark
        # so the user can drop --ide codex if they don't have it.
        found.append("codex")

    # GitHub Copilot: any of three strong signals.
    if (project_root / ".github" / "copilot-instructions.md").is_file():
        found.append("copilot")
    elif _gh_copilot_extension_present():
        found.append("copilot")
    elif shutil.which("copilot") is not None:
        found.append("copilot")

    # v3.0.0 removed continue + aider detection: neither had a
    # codevira-configurable integration path; the keys only existed
    # so the setup wizard could print "detected" — pure noise.

    return found


def _is_valid_json(path: Path) -> bool:
    """True if ``path`` is a readable file whose contents parse as
    JSON. Used by the IDE detector to distinguish "the IDE was
    actually set up" from "an empty dir exists at the expected path."

    Best-effort: any read error or parse error returns False (we
    treat "we can't tell" as "not installed" rather than risk
    writing config for an absent IDE).
    """
    try:
        import json

        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False


def _gh_copilot_extension_present() -> bool:
    """Return True if `gh extension list` indicates the Copilot
    extension is installed. Fast best-effort check — any error is
    treated as 'not installed'.
    """
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        import subprocess

        result = subprocess.run(
            [gh, "extension", "list"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return "copilot" in result.stdout.lower()
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Config file paths
# ---------------------------------------------------------------------------


def _claude_config_path(project_root: Path) -> Path:
    """Return the file Claude Code reads PROJECT-SCOPE ``mcpServers`` from.

    v2.0-rc.5 (Bug 16): same shape as Bug 6 at project scope. Pre-rc.5
    this returned ``<project>/.claude/settings.json`` — but Claude Code
    reads project-scope MCP from ``<project>/.mcp.json``. ``settings.json``
    is for project-scope hooks/permissions/env, NOT mcpServers.

    Confirmed via Claude Code docs: a committed ``.mcp.json`` is the
    canonical project-scope MCP mechanism. The user is prompted to
    trust it on first use of the project.
    """
    return project_root / ".mcp.json"


def _claude_global_config_path() -> Path:
    """Return the file Claude Code reads ``mcpServers`` from.

    User-scope MCP servers live in ``~/.claude.json`` (the JSON file at
    home root), NOT ``~/.claude/settings.json`` (which is for hooks /
    permissions / env). Confirmed via ``claude mcp list`` returning
    empty when the entry was in settings.json — Claude Code did not
    discover it. The 43KB ``~/.claude.json`` is the authoritative
    user-scope MCP config; we mutate it cooperatively (preserving all
    other top-level keys: oauthAccount, projects, telemetry, etc.).

    Caught in v2.0-rc.1 → rc.2 dogfood on Sachin's UDAP machine: setup
    looked successful but Claude Code didn't see codevira at all.
    """
    return Path.home() / ".claude.json"


def _claude_cli_path() -> str | None:
    """Return absolute path to the ``claude`` CLI binary if installed.

    Used to prefer ``claude mcp add --scope user codevira <path>`` over
    direct mutation of ~/.claude.json (43KB user state). Returns None
    if Claude Code CLI isn't installed (some users only run Claude
    Desktop) — caller falls back to direct merge.
    """
    return shutil.which("claude")


def _claude_desktop_config_path() -> Path:
    """Return the Claude Desktop config file path (platform-aware)."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
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


def _antigravity_config_candidates() -> list[Path]:
    """Antigravity 2.0 MCP-config locations, in priority order.

    Antigravity 2.0 unified configuration under the shared
    ``~/.gemini/config/`` directory (used across the Gemini CLI, the IDE
    and the SDK) while keeping a per-app ``~/.gemini/antigravity/`` file.
    Which one a given install reads can vary, so codevira reads from /
    writes to BOTH wherever they're present rather than guessing.
    """
    gemini = Path.home() / ".gemini"
    return [
        gemini / "config" / "mcp_config.json",  # 2.0 shared (CLI+IDE+SDK)
        gemini / "antigravity" / "mcp_config.json",  # per-app
    ]


def _antigravity_write_targets() -> list[Path]:
    """Config files to write codevira into.

    Every candidate whose parent directory already exists (so we only
    write where the user actually has that Antigravity surface); or, if
    none exist yet, the per-app path as the create target (preserves the
    pre-2.0 behavior of bootstrapping ``~/.gemini/antigravity/``).
    """
    candidates = _antigravity_config_candidates()
    targets = [p for p in candidates if p.parent.is_dir()]
    return targets or [candidates[-1]]


def _antigravity_config_path() -> Path:
    """Primary Antigravity write target (kept for callers needing one path)."""
    return _antigravity_write_targets()[0]


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
    """Atomic write with verify-after-write.

    Why this matters (P3 atomic state mutations, 2026-05-17 hardening):
    1. **Unique tmp name** — was ``path.with_suffix(".tmp")`` which two
       concurrent processes (e.g. two codevira sessions running
       ``setup`` simultaneously) would collide on, producing a torn
       write and losing one process's intent.
    2. **fsync before rename** — without fsync, a power loss between
       rename and kernel buffer flush could leave the file pointing
       at unflushed pages with arbitrary contents. Sachin's earlier
       report of "Claude Desktop config got cleared once" matched
       exactly this race — rare but real.
    3. **Verify-after-write** — re-read the file post-rename and
       assert content matches; if mismatch, raise. Better to fail
       loudly than to leave the user with a silently corrupted config.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"

    # Unique tempfile in the same directory as the target (so rename is
    # cross-device-safe — same filesystem).
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())  # force kernel pages to disk before rename
        tmp.replace(path)  # atomic on POSIX
        # Verify-after-write: re-read and assert. If something else clobbered
        # the file between our rename and now, this catches it.
        try:
            roundtrip = json.loads(path.read_text(encoding="utf-8"))
            if roundtrip != data:
                raise RuntimeError(
                    f"_write_json_safe: post-write verify failed for {path} — "
                    f"on-disk content differs from intended payload (concurrent writer?)"
                )
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"_write_json_safe: post-write read failed for {path}: {e}"
            )
    except Exception:
        # P9 (graceful degradation): clean up tmp on any failure so we don't
        # litter the user's IDE config directory with .tmp orphans.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _merge_mcp_config(existing: dict, server_name: str, server_config: dict) -> dict:
    """Non-destructive merge: only touch the server_name entry."""
    result = json.loads(json.dumps(existing))  # deep copy
    if "mcpServers" not in result:
        result["mcpServers"] = {}
    result["mcpServers"][server_name] = server_config
    return result


def remove_codevira_from_config(
    config_path: Path, key_prefix: str = "codevira"
) -> bool:
    """Remove all codevira entries from an IDE config file.

    Deletes keys from mcpServers that match `key_prefix` exactly or start
    with `key_prefix-` (for Antigravity per-project entries like codevira-udap).

    Returns True if any keys were removed, False if nothing to do.
    """
    if not config_path.exists():
        return False

    data = _read_json_safe(config_path)
    servers = data.get("mcpServers", {})
    if not servers:
        return False

    keys_to_remove = [
        k for k in servers if k == key_prefix or k.startswith(f"{key_prefix}-")
    ]
    if not keys_to_remove:
        return False

    for k in keys_to_remove:
        del servers[k]

    _write_json_safe(config_path, data)
    return True


def remove_codevira_project_from_config(
    config_path: Path, project_root: Path, *, dry_run: bool = False
) -> list[str]:
    """Remove ONLY the codevira entries in ``config_path`` that bind to
    ``project_root`` via ``--project-dir <project_root>``.

    Unlike :func:`remove_codevira_from_config` (which strips every codevira
    key), this is project-scoped: the bare global ``codevira`` entry and every
    other project's ``codevira-<name>`` entry are left untouched. This is what
    ``codevira untrack <project>`` uses to prune a single project's entries
    (chiefly the per-project Antigravity entries fix B now writes) without
    disturbing the rest.

    Returns the list of removed server keys (computed even in ``dry_run``,
    but nothing is written then).
    """
    if not config_path.exists():
        return []
    data = _read_json_safe(config_path)
    servers = data.get("mcpServers", {})
    if not servers:
        return []

    def _norm(p: str | Path) -> str:
        try:
            return str(Path(p).resolve())
        except (OSError, RuntimeError, ValueError):
            return str(p)

    target = _norm(project_root)
    removed: list[str] = []
    for key in list(servers):
        if not (key == "codevira" or key.startswith("codevira-")):
            continue
        args = servers[key].get("args", []) or []
        if "--project-dir" not in args:
            continue  # bare global entry — not project-scoped, leave it
        idx = args.index("--project-dir")
        if idx + 1 >= len(args):
            continue
        bound = args[idx + 1]
        if _norm(bound) == target or str(bound) == str(project_root):
            removed.append(key)

    if removed and not dry_run:
        for key in removed:
            del servers[key]
        _write_json_safe(config_path, data)
    return removed


def _has_codevira_entry(config_path: Path) -> bool:
    """True if `config_path` has a `codevira` (or `codevira-*`) mcpServers key."""
    if not config_path.exists():
        return False
    servers = _read_json_safe(config_path).get("mcpServers", {})
    return any(k == "codevira" or k.startswith("codevira-") for k in servers)


def heal_stale_registration(
    project_root: Path, require_global: bool = False
) -> list[str]:
    """Remove the stale per-project `codevira` MCP entry a pre-3.7 `init` wrote.

    v3.7.0 switched to ONE user-scope registration. An existing per-project
    entry (in `<project>/.mcp.json`, `.cursor/mcp.json`, `.windsurf/mcp.json`)
    otherwise lingers alongside the new global one — a duplicate server, and its
    hardcoded ``--project-dir`` can even pin the server to one project regardless
    of the workspace. This surgically removes just the codevira key (atomic,
    every other mcpServers entry preserved).

    NON-ORPHANING: with ``require_global=True`` (the startup path), a per-project
    entry is removed ONLY when a global codevira entry already exists for that
    IDE — so a user who only ever had a per-project registration is never left
    with no server. ``require_global=False`` (the init path) is used right after
    we've written the global entry ourselves, so removal is unconditional.

    Returns the list of config files cleaned.
    """
    project_root = project_root.resolve()
    cleaned: list[str] = []
    targets = [
        (_claude_config_path(project_root), _claude_global_config_path()),
        (_cursor_config_path(project_root), _cursor_global_config_path()),
        (_windsurf_config_path(project_root), _windsurf_global_config_path()),
    ]
    for per_project, global_path in targets:
        if not _has_codevira_entry(per_project):
            continue
        if require_global and not _has_codevira_entry(global_path):
            continue  # never orphan a user whose only registration is per-project
        try:
            if remove_codevira_from_config(per_project):
                cleaned.append(str(per_project))
        except Exception:
            pass  # best-effort; a heal failure must never break init/startup
    return cleaned


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
    pipx_bin = (
        Path.home() / ".local" / "pipx" / "venvs" / "codevira" / "bin" / "codevira"
    )
    if sys.platform == "win32":
        pipx_bin = (
            Path.home()
            / ".local"
            / "pipx"
            / "venvs"
            / "codevira"
            / "Scripts"
            / "codevira.exe"
        )
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


def _build_server_config(
    cmd_path: str, python_exe: str, project_root: Path, use_cwd: bool = True
) -> dict:
    """
    Build the MCP server config dict for per-project mode.

    If cmd_path is the Python interpreter (fallback), use `-m mcp_server --project-dir`.
    If cmd_path is the codevira binary:
      - use_cwd=True:  {"command": ..., "args": [], "cwd": ...}   (Claude / Cursor / Windsurf)
      - use_cwd=False: {"command": ..., "args": ["--project-dir", ...]}  (tools that ignore cwd)
    """
    is_python_fallback = cmd_path == python_exe

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
    is_python_fallback = cmd_path == python_exe
    if is_python_fallback:
        return {"command": cmd_path, "args": ["-m", "mcp_server"]}
    return {"command": cmd_path, "args": []}


def _inject_claude(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Claude Code per-project settings."""
    config_path = _claude_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(
        cmd_path, python_exe, project_root, use_cwd=True
    )
    # v3.1.0 M1: stamp CODEVIRA_IDE so the spawned MCP server tags
    # every write with origin.ide="claude_code".
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "claude_code",
    }
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_claude_desktop(
    project_root: Path, cmd_path: str, python_exe: str
) -> str | None:
    """Inject MCP config into Claude Desktop (stdio-only, requires full binary path).

    Claude Desktop:
      - Does NOT support the 'url' format — only 'command' + 'args'
      - Does NOT support 'cwd' — must use '--project-dir' arg
      - Requires the FULL absolute binary path (not just 'codevira')
    """
    config_path = _claude_desktop_config_path()
    existing = _read_json_safe(config_path)

    # Always use --project-dir for Claude Desktop (no cwd support)
    server_config = _build_server_config(
        cmd_path, python_exe, project_root, use_cwd=False
    )
    # v3.1.0 M1: origin.ide stamp.
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "claude_desktop",
    }

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_cursor(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Cursor per-project settings."""
    config_path = _cursor_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(
        cmd_path, python_exe, project_root, use_cwd=True
    )
    # v3.1.0 M1: origin.ide stamp.
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "cursor",
    }
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_windsurf(project_root: Path, cmd_path: str, python_exe: str) -> str | None:
    """Inject MCP config into Windsurf per-project settings."""
    config_path = _windsurf_config_path(project_root)
    existing = _read_json_safe(config_path)
    server_config = _build_server_config(
        cmd_path, python_exe, project_root, use_cwd=True
    )
    # v3.1.0 M1: origin.ide stamp.
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "windsurf",
    }
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _inject_antigravity(
    project_root: Path, cmd_path: str, python_exe: str, project_name: str
) -> str | None:
    """Inject MCP config into Google Antigravity settings (global file, unique server name per project).

    Antigravity does not support 'cwd', so always use --project-dir args.
    """
    # Sanitize project name: lowercase, replace anything non-alphanumeric with hyphens
    safe_name = re.sub(r"[^a-z0-9-]", "-", project_name.lower())
    safe_name = re.sub(r"-{2,}", "-", safe_name).strip("-")
    server_name = f"codevira-{safe_name}"

    base_config = _build_server_config(
        cmd_path, python_exe, project_root, use_cwd=False
    )
    # v3.1.0 M1: origin.ide stamp.
    base_config["env"] = {
        **(base_config.get("env") or {}),
        "CODEVIRA_IDE": "antigravity",
    }
    server_config = {
        "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
        **base_config,
    }

    # Antigravity 2.0: write into every config surface the user has
    # (shared ~/.gemini/config/ and/or per-app ~/.gemini/antigravity/).
    # v3.1.x: cross-target atomicity — snapshot pre-write, rollback on
    # any failure (see inject_global_antigravity for rationale).
    targets = _antigravity_write_targets()
    snapshots: list[tuple[Path, str | None]] = []
    written: list[Path] = []
    try:
        for config_path in targets:
            existing = _read_json_safe(config_path)
            merged = _merge_mcp_config(existing, server_name, server_config)
            snapshots.append(
                (
                    config_path,
                    config_path.read_text(encoding="utf-8")
                    if config_path.is_file()
                    else None,
                )
            )
            _write_json_safe(config_path, merged)
            written.append(config_path)
    except Exception:
        for p in written:
            orig = next((s for sp, s in snapshots if sp == p), None)
            try:
                if orig is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(orig, encoding="utf-8")
            except OSError:
                pass
        raise
    return "; ".join(str(p) for p in written) if written else None


# ---------------------------------------------------------------------------
# Global mode injection (v1.6) — one-time, no project path
# ---------------------------------------------------------------------------


def inject_global_claude_code(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Claude Code (``~/.claude.json``).

    Two-tier strategy:

      1. **Preferred** — shell out to ``claude mcp add --scope user
         codevira <cmd_path>``. Delegates the merge to the official
         Claude Code CLI, which owns ``~/.claude.json``'s file format.
         Safer than mutating 43KB of user state ourselves.

      2. **Fallback** — if ``claude`` CLI isn't on PATH or the
         subprocess fails, merge ``mcpServers.codevira`` into
         ``~/.claude.json`` directly via ``_read_json_safe`` /
         ``_write_json_safe`` (atomic via tempfile + os.replace,
         preserves every other top-level key).

    Returns the path of the file Claude Code now reads codevira from
    (always ``~/.claude.json`` regardless of which tier wrote it).
    """
    config_path = _claude_global_config_path()
    server_config = _build_global_server_config(cmd_path, python_exe)
    # v3.1.0 M1: origin.ide stamp (global Claude Code).
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "claude_code",
    }

    cli = _claude_cli_path()
    if cli is not None:
        # Prefer CLI shell-out — it knows the file format and handles
        # idempotency (re-adding overwrites the existing entry).
        if _claude_cli_add_codevira(cli, cmd_path, server_config):
            return str(config_path)
        # CLI failed (unsupported flag, version mismatch, perms, etc.)
        # Fall through to direct merge.

    # Fallback: direct cooperative merge of ~/.claude.json
    existing = _read_json_safe(config_path)
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def _claude_cli_add_codevira(
    cli: str,
    cmd_path: str,
    server_config: dict,
) -> bool:
    """Run ``claude mcp add --scope user codevira <cmd_path>``.

    Returns True on success. Returns False on any failure (caller
    falls back to direct ~/.claude.json merge).

    Note: ``claude mcp add`` overwrites an existing entry with the
    same name silently, so it's safe to run repeatedly. We pass
    args separately if the server_config has any (e.g. python
    fallback path uses ``-m mcp_server``).
    """
    import subprocess

    # If we need to call as ``python -m mcp_server`` (fallback when
    # codevira binary not on PATH), append `-- -m mcp_server`. The
    # ``--`` separator tells claude CLI everything after is args to
    # the spawned MCP server, not flags to ``claude mcp add``.
    extra_args = list(server_config.get("args") or [])

    # v3.1.0 M1: forward CODEVIRA_IDE env into the CLI-driven install
    # so the `claude mcp add` write carries provenance too. Best-effort:
    # older claude versions without --env will fail this call; the
    # caller falls back to direct ~/.claude.json merge which sets env
    # the same way.
    env_pairs: list[str] = []
    for k, v in (server_config.get("env") or {}).items():
        env_pairs.extend(["--env", f"{k}={v}"])

    # IMPORTANT (arg order): the claude CLI's `-e, --env <env...>` is a
    # VARIADIC option — it greedily consumes every following non-flag token.
    # If `--env K=V` precedes the positional `name` / `commandOrUrl`, the CLI
    # swallows `codevira` and `<cmd_path>` as extra env values and dies with
    # "error: missing required argument 'name'" (observed on claude 2.1.x —
    # this is exactly why `codevira setup` failed to register Claude Code).
    # So the positionals MUST come first; the variadic `--env` goes last
    # (immediately before the `--` server-args separator, if any).
    cmd = [
        cli,
        "mcp",
        "add",
        "--scope",
        "user",
        "codevira",
        cmd_path,
        *env_pairs,
    ]
    if extra_args:
        cmd.extend(["--", *extra_args])

    try:
        # First, remove any existing entry to ensure a clean overwrite
        # (some claude versions error on duplicate add). Best-effort.
        subprocess.run(
            [cli, "mcp", "remove", "codevira", "-s", "user"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Now add fresh.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "claude mcp add returned %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.warning("claude mcp add invocation failed: %s", exc)
        return False


def inject_global_claude_desktop(cmd_path: str, python_exe: str) -> str | None:
    """Inject codevira into Claude Desktop's ``claude_desktop_config.json``.

    Claude Desktop is detected separately from Claude Code (the CLI)
    and reads MCP server config from a different file managed by the
    desktop app itself — not ``~/.claude.json`` (CLI-scope) and not
    ``~/.claude/settings.json`` (CLI hooks/permissions).

    Constraints (from Claude Desktop docs):
      - stdio only (no HTTP url format)
      - no ``cwd`` field — must use ``--project-dir`` arg if scoping
      - requires the FULL absolute binary path

    For global mode here we use ``--project-dir`` set to the user's
    cwd at install time. The server still auto-detects the actual
    project from the spawning process's cwd at session start, so
    this default is largely cosmetic (and matches the behaviour of
    the per-project ``_inject_claude_desktop`` injector).

    Caught in v2.0-rc.1 → rc.2 dogfood: Claude Desktop was detected
    but ``_mcp_config_path_for()`` had no case for ``claude_desktop``,
    so the wizard silently skipped it.
    """
    config_path = _claude_desktop_config_path()
    existing = _read_json_safe(config_path)

    is_python_fallback = cmd_path == python_exe
    # v3.1.0 M1: origin.ide stamp included in the literal so mypy
    # infers a wide-enough value type (env is a nested dict, not a
    # list[str]).
    if is_python_fallback:
        server_config = {
            "command": cmd_path,
            "args": ["-m", "mcp_server"],
            "env": {"CODEVIRA_IDE": "claude_desktop"},
        }
    else:
        server_config = {
            "command": cmd_path,
            "args": [],
            "env": {"CODEVIRA_IDE": "claude_desktop"},
        }

    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_global_cursor(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Cursor (~/.cursor/mcp.json)."""
    config_path = _cursor_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = _build_global_server_config(cmd_path, python_exe)
    # v3.1.0 M1: origin.ide stamp.
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "cursor",
    }
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_global_windsurf(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Windsurf."""
    config_path = _windsurf_global_config_path()
    existing = _read_json_safe(config_path)
    server_config = _build_global_server_config(cmd_path, python_exe)
    # v3.1.0 M1: origin.ide stamp.
    server_config["env"] = {
        **(server_config.get("env") or {}),
        "CODEVIRA_IDE": "windsurf",
    }
    merged = _merge_mcp_config(existing, "codevira", server_config)
    _write_json_safe(config_path, merged)
    return str(config_path)


def inject_global_antigravity(cmd_path: str, python_exe: str) -> str | None:
    """Inject global codevira config into Google Antigravity.

    Uses a single 'codevira' entry with no project path.

    DEPRECATED (v3.7.1 fix B): DO NOT USE for the injection path. Antigravity
    has no `cwd` field and does not send MCP `roots` (confirmed against the
    shipped binary + official docs — D00011M / D00011N), so a project-less
    entry resolves the root to `/` and hits the forbidden-root guard — the
    server can't bind a project and every tool goes inert. The premise
    'Antigravity sets the working directory' is false. `inject_ide_config`
    and the setup wizard now write per-project `_inject_antigravity` entries
    instead. Retained only so existing origin-stamp / rollback unit tests keep
    exercising the multi-surface write helper.
    """
    base_config = _build_global_server_config(cmd_path, python_exe)
    # v3.1.0 M1: origin.ide stamp.
    base_config["env"] = {
        **(base_config.get("env") or {}),
        "CODEVIRA_IDE": "antigravity",
    }
    server_config = {
        "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
        **base_config,
    }
    # Antigravity 2.0: write into every config surface the user has
    # (shared ~/.gemini/config/ and/or per-app ~/.gemini/antigravity/).
    # v3.1.x: snapshot each target's pre-write content; on any write
    # failure, restore the previously-written targets so the user never
    # ends up with one stamped config and one un-stamped (asymmetric
    # provenance breaks cross-IDE consensus).
    targets = _antigravity_write_targets()
    snapshots: list[tuple[Path, str | None]] = []
    written: list[Path] = []
    try:
        for config_path in targets:
            existing = _read_json_safe(config_path)
            merged = _merge_mcp_config(existing, "codevira", server_config)
            snapshots.append(
                (
                    config_path,
                    config_path.read_text(encoding="utf-8")
                    if config_path.is_file()
                    else None,
                )
            )
            _write_json_safe(config_path, merged)
            written.append(config_path)
    except Exception:
        for p in written:
            orig = next((s for sp, s in snapshots if sp == p), None)
            try:
                if orig is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(orig, encoding="utf-8")
            except OSError:
                pass
        raise
    return "; ".join(str(p) for p in written) if written else None


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
                    # Claude Desktop can't do project-agnostic config (no cwd,
                    # no workspace roots, no url). v3.7.0: rather than skip it
                    # (leaving the user with NO server), fall back to a
                    # per-project registration so it still works.
                    path = _inject_claude_desktop(project_root, cmd_path, python_exe)
                    if path:
                        results["Claude Desktop (per-project)"] = path
                elif ide == "antigravity":
                    # v3.7.1 fix B (corrects SB3): Antigravity CANNOT use a bare
                    # global 'codevira' entry. It has no `cwd` field and does
                    # not send MCP `roots` (confirmed against the shipped
                    # binary + official docs — D00011M / D00011N), so a
                    # project-less entry resolves the root to `/` and the
                    # forbidden-root guard refuses it (server now degrades to
                    # inert instead of crashing — fix A — but it is still
                    # useless). Only a per-project `--project-dir` entry works.
                    # The N-entries tradeoff SB3 worried about is handled by
                    # `codevira untrack` (fix C), which prunes stale entries.
                    path = _inject_antigravity(
                        project_root, cmd_path, python_exe, project_name
                    )
                    if path:
                        results["Antigravity"] = path
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
                    path = _inject_antigravity(
                        project_root, cmd_path, python_exe, project_name
                    )
                    if path:
                        results["Antigravity"] = path

        except Exception as e:
            logger.warning("Failed to inject %s config: %s", ide, e)
            try:
                from mcp_server.crash_logger import log_crash

                log_crash(
                    e,
                    context=f"IDE config inject: {ide}",
                    project_path=str(project_root),
                )
            except Exception:
                pass

    return results
