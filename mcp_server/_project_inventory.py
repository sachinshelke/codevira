"""Canonical project inventory — P0-2/P0-3/P0-4/P2-9 (rc.5, 2026-05-13 audit).

Pre-fix, ``status --global``, ``projects``, and ``clean --dry-run`` each
counted a different thing — and the JSON output of ``projects`` reported
``in_global_db: false`` for projects that WERE registered (the lookup was
keyed on a metadata.json field that ghost dirs don't have).

This module is the single source of truth for "what projects does codevira
know about on this machine?". Every CLI surface that talks about project
counts/listings reads from :func:`enumerate_projects`.

Definitions
-----------
* **tracked**  — has a row in ``global.db.projects`` whose ``path`` (or, as a
  fallback, ``git_remote``) matches a directory that's still a valid project
  root on disk. The canonical "I'm using codevira here" state.
* **ghost**    — has a directory under ``~/.codevira/projects/`` but is
  missing ``config.yaml`` and/or ``metadata.json``. Created as a side effect
  of an MCP tool call that touched ``get_data_dir()`` (Bug 21 family).
* **orphan**   — has a row in ``global.db.projects`` whose ``path`` is no
  longer a valid project root (deleted, moved, or v1.8.0-era $HOME bootstrap).
* **stale**    — has a directory under ``~/.codevira/projects/`` with
  nothing recognisable in it (no graph, no codeindex, no metadata).

A row can carry multiple flags simultaneously (e.g. tracked + has-data-dir).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectEntry:
    """One row of the unified inventory."""
    # Identity
    slug: str | None              # ~/.codevira/projects/<slug> if present, else None
    canonical_path: str | None    # original_path from metadata.json OR projects.path
    name: str | None
    git_remote: str | None
    last_synced_at: str | None    # ISO timestamp from global.db, if registered

    # Disk presence
    has_data_dir: bool            # ~/.codevira/projects/<slug>/ exists
    has_config: bool
    has_metadata: bool
    has_graph: bool               # graph/graph.db file present
    has_codeindex: bool           # codeindex/ has at least one file
    has_roadmap: bool             # roadmap.yaml present (Bug 21 side-effect signal)
    size_bytes: int

    # Registration presence
    in_global_db: bool

    # Validity of canonical_path right now
    canonical_path_valid: bool    # exists + not a refused root

    @property
    def status(self) -> str:
        """Single-word classification used by all CLI surfaces.

        Precedence:
          1. tracked — in global.db AND canonical_path is currently valid
          2. orphan  — in global.db BUT canonical_path is invalid (deleted / refused root)
          3. ghost   — on disk WITH some real state (graph / codeindex / metadata / config)
                       but bookkeeping is incomplete
          4. stale   — directory exists but is essentially empty (nothing recognisable)
        """
        if self.in_global_db and self.canonical_path_valid:
            return "tracked"
        if self.in_global_db and not self.canonical_path_valid:
            return "orphan"
        # No-registration cases — disambiguate by how much real state is on disk.
        has_real_state = (
            self.has_graph
            or self.has_codeindex
            or self.has_config
            or self.has_metadata
            or self.has_roadmap
        )
        if self.has_data_dir and has_real_state:
            return "ghost"
        return "stale"


def enumerate_projects() -> list[ProjectEntry]:
    """Enumerate every project codevira knows about.

    Walks both ``~/.codevira/projects/`` (disk) and ``global.db.projects``
    (registration) and returns one entry per logical project. Logical
    identity is established by joining on:

    1. ``metadata.json::original_path`` ↔ ``projects.path`` (preferred).
    2. ``metadata.json::git_remote``    ↔ ``projects.git_remote`` (fallback —
       fixes P0-4 false negatives where a ghost dir lacks metadata.json
       but the project IS registered under its canonical path).

    Always safe — never raises; missing files / corrupt DBs degrade to empty.
    """
    from mcp_server.paths import get_global_home, get_global_db_path, is_invalid_project_root

    home = get_global_home()
    projects_dir = home / "projects"
    db_path = get_global_db_path()

    # ---- Load registered rows ----------------------------------------
    registered_by_path: dict[str, dict] = {}
    registered_by_remote: dict[str, dict] = {}
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                "SELECT path, name, language, git_remote, last_synced_at "
                "FROM projects"
            ).fetchall():
                row = dict(r)
                registered_by_path[row["path"]] = row
                rem = row.get("git_remote")
                if rem:
                    registered_by_remote[rem] = row
            conn.close()
        except sqlite3.DatabaseError:
            pass

    # ---- Walk disk -----------------------------------------------------
    matched_db_paths: set[str] = set()
    entries: list[ProjectEntry] = []

    if projects_dir.is_dir():
        for child in sorted(projects_dir.iterdir()):
            if not child.is_dir():
                continue
            entry = _inspect_disk(child, registered_by_path, registered_by_remote)
            entries.append(entry)
            if entry.in_global_db and entry.canonical_path:
                matched_db_paths.add(entry.canonical_path)

    # ---- Add registered-but-no-disk-dir rows (the AgentStore case
    #      from the audit: in global.db, no local data dir) ---------
    for path, row in registered_by_path.items():
        if path in matched_db_paths:
            continue
        # Was this row matched via git_remote already?
        if any(
            e.canonical_path == path
            for e in entries
            if e.canonical_path
        ):
            continue
        try:
            valid = (
                Path(path).is_dir()
                and is_invalid_project_root(Path(path)) is None
            )
        except Exception:
            valid = False
        entries.append(ProjectEntry(
            slug=None,
            canonical_path=path,
            name=row.get("name"),
            git_remote=row.get("git_remote"),
            last_synced_at=row.get("last_synced_at"),
            has_data_dir=False,
            has_config=False,
            has_metadata=False,
            has_graph=False,
            has_codeindex=False,
            has_roadmap=False,
            size_bytes=0,
            in_global_db=True,
            canonical_path_valid=valid,
        ))

    return entries


def _inspect_disk(
    slug_dir: Path,
    by_path: dict[str, dict],
    by_remote: dict[str, dict],
) -> ProjectEntry:
    """Build a ProjectEntry from a ~/.codevira/projects/<slug> directory."""
    from mcp_server.paths import is_invalid_project_root

    metadata_path = slug_dir / "metadata.json"
    config_path = slug_dir / "config.yaml"
    graph_db = slug_dir / "graph" / "graph.db"
    codeindex_dir = slug_dir / "codeindex"

    has_metadata = metadata_path.is_file()
    has_config = config_path.is_file()
    has_graph = graph_db.is_file()
    has_codeindex = codeindex_dir.is_dir() and any(codeindex_dir.iterdir())
    has_roadmap = (slug_dir / "roadmap.yaml").is_file()

    canonical_path: str | None = None
    git_remote: str | None = None
    if has_metadata:
        try:
            meta = json.loads(metadata_path.read_text())
            canonical_path = meta.get("original_path")
            git_remote = meta.get("git_remote")
        except Exception:
            pass

    # Two-step registration lookup (P0-4 fix): try canonical_path first,
    # then fall back to git_remote — so ghost dirs without metadata can
    # still match if they share a git_remote with a registered row.
    db_row: dict | None = None
    if canonical_path and canonical_path in by_path:
        db_row = by_path[canonical_path]
    elif git_remote and git_remote in by_remote:
        db_row = by_remote[git_remote]
        canonical_path = db_row["path"]
    in_global_db = db_row is not None

    name = (db_row or {}).get("name")
    last_synced_at = (db_row or {}).get("last_synced_at")

    canonical_path_valid = False
    if canonical_path:
        try:
            p = Path(canonical_path)
            canonical_path_valid = (
                p.is_dir() and is_invalid_project_root(p) is None
            )
        except Exception:
            canonical_path_valid = False

    return ProjectEntry(
        slug=slug_dir.name,
        canonical_path=canonical_path,
        name=name,
        git_remote=git_remote,
        last_synced_at=last_synced_at,
        has_data_dir=True,
        has_config=has_config,
        has_metadata=has_metadata,
        has_graph=has_graph,
        has_codeindex=has_codeindex,
        has_roadmap=has_roadmap,
        size_bytes=_dir_size(slug_dir),
        in_global_db=in_global_db,
        canonical_path_valid=canonical_path_valid,
    )


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for child in p.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def summarize(entries: list[ProjectEntry]) -> dict:
    """Compute the canonical summary numbers used everywhere.

    Returns a dict with keys:
      tracked, ghost, orphan, stale, total
    All four are non-overlapping (one entry → one status).
    """
    counts = {"tracked": 0, "ghost": 0, "orphan": 0, "stale": 0}
    for e in entries:
        counts[e.status] = counts.get(e.status, 0) + 1
    counts["total"] = sum(counts.values())
    return counts
