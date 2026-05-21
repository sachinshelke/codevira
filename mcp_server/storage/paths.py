"""
paths.py — file-path resolver for v2.2.0's in-repo storage.

Single source of truth for WHERE every storage file lives:

  <project_root>/.codevira/          ← source of truth (committed)
    decisions.jsonl
    digest.jsonl
    manifest.yaml
    outcomes.jsonl
    sessions.jsonl
    changesets.jsonl
    preferences.jsonl
    learned_rules.jsonl
    roadmap.yaml
    enforcement.yaml
    config.yaml

  <project_root>/.codevira-cache/    ← gitignored cache (rebuildable)
    fts5.sqlite                       ← FTS5 search index over decisions
    graph.sqlite                      ← tree-sitter code graph
    hash-cache.db                     ← file-hash change detection

Every other module that needs to read or write storage files MUST go
through this module. Hardcoded paths elsewhere are a bug.

Per-user state (e.g. ``~/.codevira/projects/<key>/global.db``) is
``mcp_server.paths``'s responsibility (unchanged in v2.2.0). This
module covers ONLY the project-local storage that lives in the repo.
"""

from __future__ import annotations

from pathlib import Path

from mcp_server.paths import get_project_root

CODEVIRA_DIR_NAME = ".codevira"
CODEVIRA_CACHE_DIR_NAME = ".codevira-cache"


def codevira_dir(project_root: Path | None = None) -> Path:
    """Return ``<project>/.codevira/`` — the in-repo source-of-truth dir."""
    if project_root is None:
        project_root = get_project_root()
    return project_root / CODEVIRA_DIR_NAME


def codevira_cache_dir(project_root: Path | None = None) -> Path:
    """Return ``<project>/.codevira-cache/`` — gitignored rebuildables."""
    if project_root is None:
        project_root = get_project_root()
    return project_root / CODEVIRA_CACHE_DIR_NAME


# ─── Source-of-truth files (in .codevira/) ────────────────────────────


def decisions_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "decisions.jsonl"


def digest_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "digest.jsonl"


def manifest_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "manifest.yaml"


def outcomes_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "outcomes.jsonl"


def sessions_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "sessions.jsonl"


def changesets_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "changesets.jsonl"


def preferences_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "preferences.jsonl"


def learned_rules_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "learned_rules.jsonl"


def roadmap_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "roadmap.yaml"


def enforcement_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "enforcement.yaml"


def config_path(project_root: Path | None = None) -> Path:
    return codevira_dir(project_root) / "config.yaml"


# ─── Cache files (in .codevira-cache/, gitignored) ────────────────────


def fts5_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "fts5.sqlite"


def graph_cache_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "graph.sqlite"


def hash_cache_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "hash-cache.db"


# ─── Convenience operations ───────────────────────────────────────────


def ensure_dirs(project_root: Path | None = None) -> None:
    """Create both ``.codevira/`` and ``.codevira-cache/`` if missing.

    Idempotent. Called by storage write helpers on first use so callers
    don't have to track whether the dirs exist.
    """
    codevira_dir(project_root).mkdir(parents=True, exist_ok=True)
    codevira_cache_dir(project_root).mkdir(parents=True, exist_ok=True)


def is_initialized(project_root: Path | None = None) -> bool:
    """True iff the source-of-truth ``.codevira/`` dir exists.

    Cache dir presence isn't required (it's rebuildable on demand).
    """
    return codevira_dir(project_root).is_dir()
