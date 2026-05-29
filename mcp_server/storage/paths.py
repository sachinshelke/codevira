"""
paths.py — file-path resolver for v2.2.0's in-repo storage.

Single source of truth for WHERE every storage file lives:

  <project_root>/.codevira/          ← source of truth (committed)
    decisions.jsonl
    digest.jsonl
    manifest.yaml
    outcomes.jsonl
    sessions.jsonl
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

from mcp_server.paths import get_project_root, is_invalid_project_root

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


def skills_path(project_root: Path | None = None) -> Path:
    """v3.1.0 M3: skill library store.

    Canonical (lives in ``.codevira/``, committed) because skills are
    team-shareable procedural knowledge. Schema-versioned per the
    v3.0.1 forward-compat convention (records carry ``_schema_v: 1``).

    See ``working_archived_path`` for the D000012 lock note — same
    reasoning applies (additive path computation, ensure_dirs still
    owns root validation).
    """
    return codevira_dir(project_root) / "skills.jsonl"


def working_archived_path(session_id: str, project_root: Path | None = None) -> Path:
    """v3.1.0 M2: opt-in commit target for working-memory entries.

    Default working memory lives in ``.codevira-cache/working.jsonl``
    (per-machine, ephemeral). When a session produces scratchpad worth
    team-sharing, ``codevira working commit <session_id>`` copies the
    non-evicted entries here under a session-named file so the rest of
    the repo (and other developers) can see them.

    NOTE on locked decision D000012: that decision protects the
    JSONL WRITE path's forbidden-root validation via ``ensure_dirs()``.
    This helper is pure path computation — no write, no bypass — so it
    does not conflict. Writers landing on this path MUST still call
    ``ensure_dirs()`` first (the working_archived subdir is created
    lazily there).

    The session_id is interpolated into the filename; callers MUST
    ensure it's filesystem-safe — the v3.0.1 default-session-id helper
    produces ``ad-hoc-XXXXXX`` which is safe by construction.
    """
    return codevira_dir(project_root) / "working_archived" / f"{session_id}.jsonl"


# ─── Cache files (in .codevira-cache/, gitignored) ────────────────────


def fts5_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "fts5.sqlite"


def graph_cache_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "graph.sqlite"


def hash_cache_path(project_root: Path | None = None) -> Path:
    return codevira_cache_dir(project_root) / "hash-cache.db"


def working_path(project_root: Path | None = None) -> Path:
    """v3.1.0 M2: working-memory entries.

    Lives in the cache dir because working memory is intra-session and
    ephemeral by definition; committing it would leak the agent's
    scratchpad into git. The opt-in commit path
    (``working_archived_path``) is the canonical surface when a
    session produces something worth team-sharing.

    See ``working_archived_path`` for the D000012 lock note — same
    reasoning applies (additive path computation, ensure_dirs still
    owns root validation).
    """
    return codevira_cache_dir(project_root) / "working.jsonl"


def activity_path(project_root: Path | None = None) -> Path:
    """v3.1.0 M4: spatial-activity log (per-machine, gitignored).

    Stores ``edit`` / ``decision_ref`` rows as the agent works through
    the codebase. The ``codevira spatial export-activity`` CLI is the
    opt-in path to share aggregated heat with a team; the raw log
    itself stays local because attention patterns are per-developer.

    See ``working_archived_path`` for the D000012 lock note — same
    reasoning applies.
    """
    return codevira_cache_dir(project_root) / "activity.jsonl"


# ─── Convenience operations ───────────────────────────────────────────


def ensure_dirs(project_root: Path | None = None) -> None:
    """Create both ``.codevira/`` and ``.codevira-cache/`` if missing.

    Idempotent. Called by storage write helpers on first use so callers
    don't have to track whether the dirs exist.

    v3.0.0 (2026-05-25 G5 dogfood finding): refuses to scaffold the store
    at a forbidden project root (``$HOME``, ``/`` and other system
    top-levels). This is the WRITE-side counterpart of the guard
    ``mcp_server.paths.get_data_dir`` already applies to the legacy
    centralized store — the v3.0.0 JSONL write path previously bypassed
    it. Without this, a codevira launched with no project anchor (most
    often a *global* MCP config in Claude Desktop: no ``cwd`` option and
    no ``CODEVIRA_PROJECT_DIR``) resolves the root to ``/`` — or whatever
    cwd the GUI subprocess inherited — and silently creates ``/.codevira``
    or ``$HOME/.codevira``, detaching decisions from the real project.
    Read paths (``is_initialized``, list/search) deliberately stay
    guard-free so they degrade to empty rather than raise (P9).

    Raises:
        ValueError: with a WHAT + WHY + FIX message if the resolved root
            is a forbidden system directory or ``$HOME``.
    """
    root = (project_root if project_root is not None else get_project_root()).resolve()
    rejection = is_invalid_project_root(root)
    if rejection:
        raise ValueError(
            f"Refusing to create the codevira store: {rejection} "
            f"(project root resolved to {root}). codevira was launched "
            f"without a project anchor — most often a global MCP config "
            f"(e.g. Claude Desktop) with no working directory. "
            f"Fix: set CODEVIRA_PROJECT_DIR=<your project path> in the MCP "
            f"server's env block, or run codevira from inside a real "
            f"project directory."
        )
    codevira_dir(root).mkdir(parents=True, exist_ok=True)
    codevira_cache_dir(root).mkdir(parents=True, exist_ok=True)


def is_initialized(project_root: Path | None = None) -> bool:
    """True iff the source-of-truth ``.codevira/`` dir exists.

    Cache dir presence isn't required (it's rebuildable on demand).
    """
    return codevira_dir(project_root).is_dir()
