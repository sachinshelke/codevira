"""
paths.py — Centralized path resolution for Codevira.

Resolution priority for get_data_dir():
  1. Centralized ~/.codevira/projects/<key>/ — new in v1.6 (keyed by project path)
  2. Git remote lookup — survives directory renames
  3. Legacy <project_root>/.codevira/ — backward compat for existing projects
  4. Default to centralized path for brand-new projects

Project root discovery (get_project_root()):
  - Uses --project-dir CLI override if set
  - Walks upward from cwd looking for project markers:
    .git, pyproject.toml, package.json, go.mod, Cargo.toml, .codevira/
  - Falls back to cwd if no marker found

Performance notes:
  - get_data_dir() result is cached per project root (_data_dir_cache).
    First call may spawn one `git remote` subprocess and scan metadata files;
    every subsequent call for the same root is a dict lookup (~0µs).
  - Call invalidate_data_dir_cache() after init/migration to force re-resolution.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Allow overriding project directory via CLI flag (e.g. for Google Antigravity
# which doesn't support the `cwd` option in its MCP config) or via the
# CODEVIRA_PROJECT_DIR env var (v3.0.0 round-3 — for IDE MCP configs like
# Claude Desktop's mcpServers entry that pass no CLI args and have no
# meaningful working directory to anchor on).
_project_dir_override: Path | None = None
_PROJECT_DIR_ENV = "CODEVIRA_PROJECT_DIR"


def set_project_dir(path: str | Path) -> None:
    """Override the project directory (called by CLI when --project-dir is passed).

    Also clears the data-dir cache so subsequent get_data_dir() calls
    resolve against the new project root, not a stale cached entry.
    """
    global _project_dir_override
    _project_dir_override = Path(path).resolve()
    invalidate_data_dir_cache()


# ---------------------------------------------------------------------------
# Path-key helpers (for centralized storage)
# ---------------------------------------------------------------------------

#: Markers that identify a project root when walking upward.
_PROJECT_MARKERS = frozenset(
    {
        ".git",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        ".codevira",
    }
)


#: Maximum length of the human-readable portion of a project key.
#: Filesystem `ENAMETOOLONG` limit is typically 255 bytes total for any
#: single path component on macOS APFS / Linux ext4. The slug is used as
#: a directory name under ``~/.codevira/projects/<slug>/`` and we also
#: append ``/graph/graph.db`` etc., so the slug itself must be well below
#: 255. Cap at 180 chars; the 8-char hash suffix preserves uniqueness
#: for collisions between paths that differ only after the truncation
#: point. Caught by Week-2 edge-case test (50-level-deep paths).
_MAX_KEY_LEN = 180


def _sanitize_path_key(abs_path: str | Path) -> str:
    """Convert an absolute path to a filesystem-safe key string.

    Uses a short hash suffix to prevent collisions between paths that
    differ only in separator characters (e.g. /foo-bar vs /foo/bar),
    drive letters (D:\\Projects\\Foo vs C:\\Projects\\Foo), or — since
    Week 2 — paths that differ only after the truncation point.

    Examples:
        /Users/alice/Projects/Foo            → Users_alice_Projects_Foo_a1b2c3d4
        /very/deeply/nested/.../50-levels    → very_deeply_nested..._a1b2c3d4
                                                (truncated at 180 chars; hash
                                                preserves uniqueness)

    Filesystem safety: the full slug (key + hash) is capped to ~189 chars,
    well under 255-byte ENAMETOOLONG limit on common filesystems.
    """
    import hashlib

    resolved = str(Path(abs_path).resolve())
    # Hash the FULL resolved path (before any lossy transforms) for uniqueness
    path_hash = hashlib.sha256(resolved.encode()).hexdigest()[:8]
    # Strip drive letter and leading separators for the human-readable part
    stripped = re.sub(r"^[A-Za-z]:", "", resolved)  # Windows drive letter
    stripped = stripped.lstrip("/\\")
    # Replace path separators with underscores (not hyphens — preserves
    # literal hyphens in directory names as distinct from separators)
    key = re.sub(r"[/\\]", "_", stripped)
    # Replace any remaining non-safe chars with hyphens
    key = re.sub(r"[^a-zA-Z0-9._-]", "-", key)
    # Collapse consecutive underscores/hyphens
    key = re.sub(r"[_-]{2,}", "_", key)
    key = key.strip("_-")
    # Cap human-readable portion to keep total slug under 255 bytes
    # (key + "_" + 8-char hash → max ~189). Caught by Week-2 deep-path
    # edge-case test — without this, deeply nested project paths
    # produced ENAMETOOLONG when codevira tried to mkdir the project dir.
    if len(key) > _MAX_KEY_LEN:
        key = key[:_MAX_KEY_LEN].rstrip("_-")
    return f"{key}_{path_hash}"


def _get_git_remote_url(project_root: Path) -> str | None:
    """Return the git remote 'origin' URL for project_root, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url if url else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _find_project_by_git_remote(remote_url: str) -> Path | None:
    """Scan ~/.codevira/projects/ for a project whose metadata.json matches remote_url."""
    projects_dir = get_global_home() / "projects"
    if not projects_dir.exists():
        return None
    for meta_file in projects_dir.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_file.read_text())
            if meta.get("git_remote") == remote_url:
                # Return the centralized data dir (the directory containing metadata.json)
                return meta_file.parent
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------


def _discover_project_root(start: Path) -> Path:
    """Walk upward from *start* to find the nearest project root.

    A project root is any ancestor directory that contains at least one
    of: .git, pyproject.toml, package.json, go.mod, Cargo.toml, .codevira/

    Stops at the first match so that nested repos return the inner root.
    Falls back to *start* if no marker is found.
    """
    start = start.resolve()
    for candidate in (start, *start.parents):
        for marker in _PROJECT_MARKERS:
            if (candidate / marker).exists():
                return candidate
    return start


def get_project_root() -> Path:
    """Return the project root directory.

    Resolution order:
      1. ``--project-dir`` CLI flag (via ``set_project_dir()``) — wins
         over everything else.
      2. ``$CODEVIRA_PROJECT_DIR`` env var — for IDE MCP configs
         (Claude Desktop, Codex, etc.) that spawn ``codevira serve``
         with no CLI args and no meaningful cwd to anchor on. Added in
         v3.0.0 round-3 after the AgentStore Claude-Desktop pin caught
         that ``env`` blocks in MCP config were being silently ignored.
      3. ``Path.cwd()`` — discover from current working directory by
         walking upward looking for project markers (.git,
         pyproject.toml, package.json, etc.). Falls back to cwd
         if no marker found.
    """
    if _project_dir_override is not None:
        return _discover_project_root(_project_dir_override)
    env_override = os.environ.get(_PROJECT_DIR_ENV)
    if env_override:
        return _discover_project_root(Path(env_override).resolve())
    return _discover_project_root(Path.cwd())


# ---------------------------------------------------------------------------
# Project-root validation (v1.8.1 — refuse $HOME and system dirs)
# ---------------------------------------------------------------------------

#: Absolute paths that must never be a project root. These are top-level user
#: or system directories where treating them as a project causes the watcher
#: to walk huge unrelated trees (``~/Library/Group Containers/...``,
#: ``/var/log``, etc.) and crash on EINTR / permission errors. macOS
#: aggressively symlinks system top-levels (``/etc -> /private/etc``,
#: ``/var -> /private/var``, ``/tmp -> /private/tmp``,
#: ``/home -> /System/Volumes/Data/home``) so the resolved forms are
#: listed too — ``Path("/etc").resolve()`` returns ``/private/etc`` on
#: macOS, and our equality check has to catch that.
_FORBIDDEN_PROJECT_ROOTS: frozenset[Path] = frozenset(
    {
        Path("/"),
        Path("/Users"),
        Path("/home"),
        Path("/System/Volumes/Data/home"),  # macOS resolved /home
        Path("/tmp"),
        Path("/private/tmp"),  # macOS resolved /tmp
        Path("/var"),
        Path("/private/var"),  # macOS resolved /var
        Path("/etc"),
        Path("/private/etc"),  # macOS resolved /etc
        Path("/opt"),
    }
)


def is_invalid_project_root(p: Path) -> str | None:
    """Return a human-readable rejection reason if ``p`` shouldn't be a
    project root, else ``None``.

    Refuses ``$HOME`` and known system top-levels (``/``, ``/Users``,
    ``/home``, ``/tmp``, ``/var``, ``/etc``, ``/opt``, plus the
    macOS-resolved ``/private/...`` and ``/System/Volumes/Data/home``
    forms). Treating any of these as a project causes the background
    watcher to walk huge unrelated trees — see v1.8.1 crash-log
    analysis: 41 ``InterruptedError`` crashes traced to a rogue
    ``$HOME``-rooted project bootstrapped on v1.8.0.

    Symlink-aware via ``Path.resolve()``: a symlinked ``$HOME`` or a path
    that resolves into ``/private/tmp`` is correctly rejected. We also
    check the *unresolved* input — on platforms where a forbidden top
    isn't a symlink, that's the only form we'd see. If ``.resolve()``
    itself raises (filesystem race, dangling symlink) we still check the
    raw input and return ``None`` only if neither matches; we never want
    to mask weirdness as "valid".
    """
    candidates: list[Path] = [p]
    try:
        resolved = p.resolve()
        candidates.append(resolved)
    except (OSError, RuntimeError):
        resolved = None
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        for cand in candidates:
            if cand == home:
                return (
                    f"$HOME ({cand}) is not a project. "
                    f"cd into a real project subdirectory first."
                )
    for cand in candidates:
        if cand in _FORBIDDEN_PROJECT_ROOTS:
            return f"{cand} is a system directory, not a project."
    return None


def is_ephemeral_project_path(p: Path) -> bool:
    """Return True if ``p`` lives under an OS temp directory.

    Used to keep transient pytest / scratch projects out of the
    cross-machine registry that ``codevira projects`` lists. Best-effort —
    never raises.

    Detection is by **temp-directory ancestry ONLY** — deliberately NOT
    by substring markers like ``pytest-``. A real project named
    ``pytest-django`` (or any path that merely *contains* such a token)
    must never be classified ephemeral, or codevira would silently hide a
    user's real project. The genuine pytest / scratch dirs always live
    under the system temp root
    (``/private/var/folders/.../pytest-of-<user>/...`` on macOS,
    ``/tmp/pytest-of-...`` on Linux), so ancestry is both sufficient and
    safe.

    Args:
        p: The candidate project root.

    Returns:
        True only when ``p`` resolves under a temp root; False otherwise.
    """
    import tempfile

    try:
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            resolved = p

        temp_roots: list[Path] = []
        try:
            temp_roots.append(Path(tempfile.gettempdir()).resolve())
        except (OSError, RuntimeError):
            pass
        temp_roots += [
            Path("/tmp"),
            Path("/private/tmp"),
            Path("/var/folders"),
            Path("/private/var/folders"),
        ]
        for root in temp_roots:
            if resolved == root or root in resolved.parents:
                return True
    except Exception:  # noqa: BLE001 — classification must never raise
        return False
    return False


# ---------------------------------------------------------------------------
# Data directory resolution (v1.6 centralized + legacy fallback)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Data directory cache — avoids re-running subprocess + glob on every tool call.
# Keyed by resolved project root Path.  Invalidated after init/migration.
# ---------------------------------------------------------------------------
_data_dir_cache: dict[Path, Path] = {}


def invalidate_data_dir_cache(project_root: Path | None = None) -> None:
    """Clear the data-dir cache so the next call re-resolves from disk.

    Call this after codevira init or after a migration completes, when the
    centralized directory has just been created and the cache entry would
    still point to the old (non-existent) default path.

    Args:
        project_root: If given, only invalidate that project's entry.
                      If None, clear the entire cache.
    """
    if project_root is None:
        _data_dir_cache.clear()
    else:
        _data_dir_cache.pop(Path(project_root).resolve(), None)


def get_data_dir() -> Path:
    """Return the Codevira data directory for the current project.

    Resolution chain (run once per project root, then cached):
      1. Centralized ~/.codevira/projects/<key>/ if config.yaml exists there
      2. Git remote lookup — finds centralized dir even after directory rename
      3. Legacy <project_root>/.codevira/ if config.yaml exists there
      4. Default to centralized path (new projects land here automatically)

    After the first call for a given project root the result is cached in
    _data_dir_cache.  Subsequent calls are O(1) dict lookups with no I/O.
    Call invalidate_data_dir_cache() after init or migration to force refresh.

    v3.0 hardening (2026-05-23 audit): refuses invalid roots (``$HOME``,
    system top-levels). Pre-fix, this function happily resolved to
    ``~/.codevira/projects/<sanitized-$HOME-key>/`` for an invalid root
    and callers then created ghost dirs there — the v1.8.0 production
    crash class. Now raises ``ValueError`` with the rejection reason
    instead. Callers that need graceful degradation should call
    ``is_invalid_project_root(p)`` first.
    """
    project_root = get_project_root()

    rejection = is_invalid_project_root(project_root)
    if rejection:
        raise ValueError(
            f"get_data_dir() refuses invalid project root: {rejection} "
            f"(root resolved to {project_root}). Set CODEVIRA_PROJECT_DIR "
            f"or cd into a real project subdirectory."
        )

    # Fast path — already resolved for this root
    if project_root in _data_dir_cache:
        return _data_dir_cache[project_root]

    result = _resolve_data_dir(project_root)
    _data_dir_cache[project_root] = result
    return result


def _resolve_data_dir(project_root: Path) -> Path:
    """Perform the actual (potentially slow) data-dir resolution.

    This is the only place that spawns subprocesses or reads metadata files.
    Always call get_data_dir() in production code — it caches this result.
    """
    key = _sanitize_path_key(project_root)
    centralized = get_global_home() / "projects" / key

    # 1. Centralized dir already initialized?
    if (centralized / "config.yaml").is_file():
        return centralized

    # 2. Try git remote lookup (survives directory renames).
    #    _get_git_remote_url() and _find_project_by_git_remote() are the
    #    potentially expensive operations — subprocess + metadata file scan.
    #    They only run once per project root thanks to the cache above.
    remote_url = _get_git_remote_url(project_root)
    if remote_url:
        found = _find_project_by_git_remote(remote_url)
        if found is not None:
            return found

    # 3. Legacy in-project .codevira/ (backward compat for v1.5 and earlier)
    legacy = project_root / ".codevira"
    if (legacy / "config.yaml").is_file():
        return legacy

    # 4. Default to centralized path — new project, will be created on init
    return centralized


def get_package_data_dir() -> Path:
    """Return the bundled data directory that ships with the pip package.

    Contains: rules/, agents/, config.example.yaml
    These are read-only assets installed alongside the package.
    """
    return Path(__file__).parent / "data"


def get_global_home() -> Path:
    """Return ~/.codevira/ global data directory. Creates it if needed."""
    home = Path.home() / ".codevira"
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_global_db_path() -> Path:
    """Return path to the global SQLite database for cross-project intelligence."""
    return get_global_home() / "global.db"
