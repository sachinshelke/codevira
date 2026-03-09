"""
paths.py — Centralized path resolution for Codevira.

After `pip install codevira-mcp`, code lives in site-packages.
Project data (.codevira/) lives in the user's project directory.

All tools should import from here instead of computing
Path(__file__).parent.parent... chains.
"""
from pathlib import Path

# Allow overriding project directory via CLI flag (e.g. for Google Antigravity
# which doesn't support the `cwd` option in its MCP config).
_project_dir_override: Path | None = None


def set_project_dir(path: str | Path) -> None:
    """Override the project directory (called by CLI when --project-dir is passed)."""
    global _project_dir_override
    _project_dir_override = Path(path).resolve()


def get_project_root() -> Path:
    """Return the project root directory.

    Uses --project-dir override if set (for Google Antigravity),
    otherwise falls back to the current working directory.
    """
    if _project_dir_override is not None:
        return _project_dir_override
    return Path.cwd()


def get_data_dir() -> Path:
    """Return the .codevira/ data directory inside the project root."""
    return get_project_root() / ".codevira"


def get_package_data_dir() -> Path:
    """Return the bundled data directory that ships with the pip package.

    Contains: rules/, agents/, config.example.yaml
    These are read-only assets installed alongside the package.
    """
    return Path(__file__).parent / "data"
