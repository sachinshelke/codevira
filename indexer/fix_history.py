"""
fix_history.py — track "this code is the fix for bug X" so Hero 2
(Anti-Regression Memory) can warn / block when AI proposes to revert it.

Sources of fix records (Week-2 wires both; Week-1 just supports manual):

  1. Manual: ``codevira fix-noted`` CLI flag the user adds after a hand-fix.
  2. Git log: commits whose subject matches /^fix(.*)?:|^bug(.*)?:|fixes
     #\\d+/i — backfilled on `codevira hooks install` and on user demand.

Storage: a small SQLite database at ``<data_dir>/graph/fixes.db`` —
separate from the main graph.db so a corrupted fix history can be wiped
without hurting other state.

Public API:

    record_fix(project_root, file, lines, description, source, commit_sha=None)
    lookup(project_root, file_path) -> list[FixRecord]
    is_revert(proposed_diff: str, fix: FixRecord) -> bool

Week-1 deliverable: minimal record + lookup. Empty lookup is fine; Hero 2
just won't fire until git scanning lands.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FixRecord:
    """One recorded fix.

    Attributes:
        id: row id in the SQLite DB
        file_path: project-relative path the fix touches
        line_start: starting line of the fix region (1-indexed)
        line_end: end line (inclusive) of the fix region
        description: human-readable description ("connection retries
            weren't decrementing counter, fixed by adding -=1 in finally")
        source: ``"manual"`` (user flagged) or ``"git"`` (commit subject)
        commit_sha: git commit SHA if source=="git"; None otherwise
        recorded_at: epoch seconds when fix was recorded
    """

    id: int
    file_path: str
    line_start: int
    line_end: int
    description: str
    source: str
    commit_sha: str | None = None
    recorded_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "description": self.description,
            "source": self.source,
            "commit_sha": self.commit_sha,
            "recorded_at": self.recorded_at,
        }


# ----------------------------------------------------------------------
# Storage helpers — open a per-project fixes.db on demand. Connections
# are cached per project_root for the life of the process.
# ----------------------------------------------------------------------

_conn_cache: dict[Path, sqlite3.Connection] = {}


def _db_path(project_root: Path) -> Path:
    """Resolve the fixes.db location for a project."""
    from mcp_server.paths import _sanitize_path_key, get_global_home

    key = _sanitize_path_key(project_root)
    return get_global_home() / "projects" / key / "graph" / "fixes.db"


def _connect(project_root: Path) -> sqlite3.Connection:
    """Open (or return cached) connection to the fixes DB.

    Schema is created lazily on first connect.
    """
    pr = project_root.resolve()
    cached = _conn_cache.get(pr)
    if cached is not None:
        return cached
    db_path = _db_path(pr)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fixes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path    TEXT NOT NULL,
            line_start   INTEGER NOT NULL,
            line_end     INTEGER NOT NULL,
            description  TEXT NOT NULL,
            source       TEXT NOT NULL CHECK(source IN ('manual', 'git')),
            commit_sha   TEXT,
            recorded_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fixes_file ON fixes(file_path)"
    )
    conn.commit()
    _conn_cache[pr] = conn
    return conn


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def record_fix(
    project_root: Path,
    file_path: str,
    line_start: int,
    line_end: int,
    description: str,
    *,
    source: str = "manual",
    commit_sha: str | None = None,
) -> int:
    """Record a fix. Returns the new row id.

    ``source`` must be ``"manual"`` or ``"git"``.
    """
    if source not in ("manual", "git"):
        raise ValueError(f"source must be 'manual' or 'git', got {source!r}")
    if source == "git" and not commit_sha:
        raise ValueError("commit_sha required for source='git'")
    if line_end < line_start:
        raise ValueError(f"line_end ({line_end}) < line_start ({line_start})")

    import time
    conn = _connect(project_root)
    cur = conn.execute(
        """
        INSERT INTO fixes
          (file_path, line_start, line_end, description, source, commit_sha, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (file_path, line_start, line_end, description, source, commit_sha, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def lookup(project_root: Path, file_path: str | Path) -> list[dict[str, Any]]:
    """Return fixes touching ``file_path``, newest first.

    Path is normalized to project-relative if it falls under project_root.
    Empty list if no fixes recorded yet (the common case until Week 2's
    git-scanning work).
    """
    if isinstance(file_path, Path):
        try:
            rel = str(file_path.resolve().relative_to(project_root.resolve()))
        except ValueError:
            rel = str(file_path)
    else:
        rel = str(file_path)

    try:
        conn = _connect(project_root)
        rows = conn.execute(
            """
            SELECT id, file_path, line_start, line_end, description,
                   source, commit_sha, recorded_at
            FROM fixes
            WHERE file_path = ?
            ORDER BY recorded_at DESC
            """,
            (rel,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def is_revert(proposed_diff: str, fix: FixRecord | dict[str, Any]) -> bool:
    """Heuristic: does ``proposed_diff`` move ``fix`` toward the pre-fix state?

    Week-1 implementation is intentionally simple — it returns True if
    the diff TOUCHES the fix's line range AND looks like a deletion of
    fix-shaped patterns (lines starting with ``-`` in unified diff form).

    Week-2 will replace with proper diff-application + content comparison
    against git history. This stub is enough for unit tests to verify the
    interface contract.

    A simple-but-real heuristic is strictly better than nothing —
    overzealous false positives are filtered by Hero 2's policy logic
    (which presents the warning to the user before blocking).
    """
    desc = fix.description if isinstance(fix, FixRecord) else fix.get("description", "")
    line_start = fix.line_start if isinstance(fix, FixRecord) else fix.get("line_start", 0)
    line_end = fix.line_end if isinstance(fix, FixRecord) else fix.get("line_end", 0)

    if not proposed_diff:
        return False

    # Cheapest signal: the diff explicitly mentions the fix's line range
    # and contains a deletion. Real anti-regression detection arrives in
    # Week 2; this is the minimum useful check.
    #
    # Match the unified-diff hunk header for the fix's line range. We
    # need a word-boundary match — `@@ -10` should not match `@@ -100`.
    # Hunk headers in unified diff are ``@@ -<start>[,<count>] +...``
    # so the char after <start> is `,` or ` ` (the +).
    import re as _re
    range_pattern = _re.compile(rf"@@ -{line_start}(?:,| )")
    has_range = bool(range_pattern.search(proposed_diff))
    has_deletion = any(
        line.startswith("-") and not line.startswith("---")
        for line in proposed_diff.splitlines()
    )
    return has_range and has_deletion


def reset(project_root: Path) -> None:
    """Tests only — drop the cached connection and delete the DB."""
    pr = project_root.resolve()
    conn = _conn_cache.pop(pr, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    db_path = _db_path(pr)
    if db_path.exists():
        db_path.unlink()
