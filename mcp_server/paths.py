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
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Allow overriding project directory via CLI flag (e.g. for Google Antigravity
# which doesn't support the `cwd` option in its MCP config).
_project_dir_override: Path | None = None


def set_project_dir(path: str | Path) -> None:
    """Override the project directory (called by CLI when --project-dir is passed)."""
    global _project_dir_override
    _project_dir_override = Path(path).resolve()


# ---------------------------------------------------------------------------
# Path-key helpers (for centralized storage)
# ---------------------------------------------------------------------------

#: Markers that identify a project root when walking upward.
_PROJECT_MARKERS = frozenset({
    ".git",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    ".codevira",
})


def _sanitize_path_key(abs_path: str | Path) -> str:
    """Convert an absolute path to a filesystem-safe key string.

    Uses a short hash suffix to prevent collisions between paths that
    differ only in separator characters (e.g. /foo-bar vs /foo/bar) or
    drive letters (D:\\Projects\\Foo vs C:\\Projects\\Foo).

    Examples:
        /Users/sachin/Projects/Foo  → Users_sachin_Projects_Foo_a1b2c3d4
        C:\\Users\\sachin\\Projects → Users_sachin_Projects_a1b2c3d4
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

    Uses --project-dir override if set (for Google Antigravity),
    otherwise falls back to the current working directory.
    """
    if _project_dir_override is not None:
        return _discover_project_root(_project_dir_override)
    return _discover_project_root(Path.cwd())


# ---------------------------------------------------------------------------
# Data directory resolution (v1.6 centralized + legacy fallback)
# ---------------------------------------------------------------------------

def get_data_dir() -> Path:
    """Return the Codevira data directory for the current project.

    Resolution chain:
      1. Centralized ~/.codevira/projects/<key>/ if config.yaml exists there
      2. Git remote lookup — finds centralized dir even after directory rename
      3. Legacy <project_root>/.codevira/ if config.yaml exists there
      4. Default to centralized path (new projects land here automatically)
    """
    project_root = get_project_root()
    key = _sanitize_path_key(project_root)
    centralized = get_global_home() / "projects" / key

    # 1. Centralized dir already initialized?
    if (centralized / "config.yaml").is_file():
        return centralized

    # 2. Try git remote lookup (survives directory renames)
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
