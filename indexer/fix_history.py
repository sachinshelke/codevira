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
import threading
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
#
# Thread-safety: ``_conn_cache_lock`` serializes both READ and WRITE of
# ``_conn_cache``. Without it, concurrent ``_connect`` calls race on the
# get → check → connect → set sequence and create N distinct connection
# objects (verified in QA test). The cost of locking the read side is
# trivial — it's a dict lookup behind a Python lock — and prevents
# leaked connections + duplicated CREATE TABLE statements.
# ----------------------------------------------------------------------

_conn_cache: dict[Path, sqlite3.Connection] = {}
# RLock (reentrant) instead of plain Lock — defensive against future code
# paths that nest _connect calls (e.g., a policy that needs fixes for
# multiple files could hit a cascade). Plain Lock would deadlock on
# same-thread reentry; RLock allows it. (Round-2 QA finding P2 #5.)
_conn_cache_lock = threading.RLock()


def _db_path(project_root: Path) -> Path:
    """Resolve the fixes.db location for a project."""
    from mcp_server.paths import _sanitize_path_key, get_global_home

    key = _sanitize_path_key(project_root)
    return get_global_home() / "projects" / key / "graph" / "fixes.db"


def _connect(project_root: Path) -> sqlite3.Connection:
    """Open (or return cached) connection to the fixes DB.

    Schema is created lazily on first connect. Thread-safe — concurrent
    callers receive the same cached connection (subsequent SQL on it
    is serialized by SQLite's own per-connection lock).
    """
    pr = project_root.resolve()
    # Fast path — already cached. Take the lock anyway because dict
    # mutation by another thread could race with the read.
    with _conn_cache_lock:
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


#: Maximum size of a proposed change we'll analyze. Anything bigger is
#: a sign the AI is working with a generated/data file, not source code;
#: bail to False rather than burn CPU. (Round-2 QA finding P2 #3.)
_MAX_CHANGE_BYTES = 100_000


#: Regex for the Claude Code Edit-format envelope. Anchors at line start
#: with re.MULTILINE so embedded ``--- before`` / ``--- after`` lines
#: inside the user's ``old_string`` / ``new_string`` don't break parsing.
#: (Round-2 QA finding P1 #2.)
import re as _re

_EDIT_FORMAT_RE = _re.compile(
    r"^--- before\n(?P<before>.*?)\n^--- after\n(?P<after>.*)\Z",
    _re.DOTALL | _re.MULTILINE,
)


def is_revert(proposed_change: str, fix: FixRecord | dict[str, Any]) -> bool:
    """Heuristic: does ``proposed_change`` move ``fix`` toward the pre-fix state?

    Accepts two input shapes (the wiring layer produces different formats
    for different tools):

      1. **Unified diff** — string containing ``@@ -<line>[,<count>]`` hunk
         headers. Used for git-derived diffs (Week-2 git scanning will
         produce these). Heuristic: hunk header overlaps the fix's line
         range AND a deletion line is present.

      2. **Claude Code Edit format** — string with ``--- before`` and
         ``--- after`` markers (built by ``claude_code_hooks._build_event``
         for Edit/Write tool calls). Heuristic: the ``--- after`` block
         contains text from ``fix.description`` keywords more than
         ``--- before`` does. Word-boundary keyword matching avoids the
         "infinite" matches "reconnection" false-positive.

    For either format, the goal is high-recall / moderate-precision —
    Hero 2's policy presents the warning to the user before blocking, so
    false positives are tolerable. False negatives (missed reverts) are
    the failure mode we minimize.

    Bails to False (not crash) on:
      - Empty/None input
      - Input larger than 100 KB (round-2 QA P2 #3)
      - Malformed format markers
      - Generic descriptions with no actionable keywords

    Week-2 expansion will add: proper diff parsing, content-similarity
    against git pre-fix state, and AST-aware revert detection. This
    Week-1 baseline catches the obvious cases.

    Args:
        proposed_change: a unified-diff string OR Claude Code Edit-format
            string. Empty/None/oversized → False.
        fix: FixRecord or dict-shaped fix record (from ``lookup()``).

    Returns:
        True if the change appears to revert the fix; False otherwise.
    """
    if not proposed_change:
        return False
    if len(proposed_change) > _MAX_CHANGE_BYTES:
        # Don't burn CPU on huge inputs (e.g., AI editing a generated
        # 1 MB JSON file). Conservative bail; Hero 2 can refine later.
        return False

    line_start = (
        fix.line_start if isinstance(fix, FixRecord) else fix.get("line_start", 0)
    )
    description = (
        fix.description if isinstance(fix, FixRecord) else fix.get("description", "")
    ) or ""

    # Format detection: try the strict edit-format regex first; if it
    # matches, dispatch to the edit handler. Otherwise treat as unified
    # diff.
    edit_match = _EDIT_FORMAT_RE.match(proposed_change)
    if edit_match is not None:
        before_block = edit_match.group("before")
        after_block = edit_match.group("after")
        return _is_revert_edit_format(before_block, after_block, description)

    return _is_revert_unified_diff(proposed_change, line_start)


def _is_revert_unified_diff(diff: str, line_start: int) -> bool:
    """Heuristic for unified-diff input.

    True iff the diff's hunk header overlaps the fix's line range AND
    a deletion is present. We use a word-boundary regex so ``@@ -10``
    doesn't match ``@@ -100``.
    """
    range_pattern = _re.compile(rf"@@ -{line_start}(?:,| )")
    has_range = bool(range_pattern.search(diff))
    has_deletion = any(
        line.startswith("-") and not line.startswith("---")
        for line in diff.splitlines()
    )
    return has_range and has_deletion


def _is_revert_edit_format(
    before_block: str, after_block: str, description: str
) -> bool:
    """Heuristic for Claude Code Edit-format input.

    Args:
        before_block: the ``old_string`` (what's being replaced)
        after_block: the ``new_string`` (what's replacing it)
        description: free-text fix description (used to extract keywords)

    The simple intuition: if ``before`` looks like the FIX code and
    ``after`` looks like the BROKEN code, this is a revert. We
    approximate "looks like buggy code" via keyword overlap with the
    fix description, with word-boundary matching to avoid false
    positives like ``"infinite"`` matching ``"reconnection"``.

    Special cases:
      - ``after`` empty and ``before`` non-empty → deletion of fix code
        → revert.
      - ``before`` empty and ``after`` non-empty → addition (NOT a revert).
      - Description has no actionable keywords → can't decide → False.

    Round-2 QA findings P1 #1 and P1 #2 are addressed by the
    word-boundary matching and the regex-based parser respectively.
    """
    before_block = before_block.strip()
    after_block = after_block.strip()

    # Pure deletion of fix code → revert
    if before_block and not after_block:
        return True
    # Pure addition is never a revert
    if after_block and not before_block:
        return False

    # Keyword-overlap heuristic. Strip the verb-y words that describe
    # "fixing"; keep nouns that describe what was buggy.
    skip_words = {
        "fix", "fixed", "fixes", "fixing", "bug", "bugs", "error",
        "errors", "issue", "issues", "the", "a", "an", "to", "of",
        "in", "on", "for", "by", "with", "and", "or", "but", "is",
        "was", "were", "be", "been", "being", "now", "have",
    }
    desc_tokens = {
        t.lower().strip(".,;:!?")
        for t in description.split()
        if t.lower().strip(".,;:!?") not in skip_words and len(t) > 2
    }
    if not desc_tokens:
        return False  # description too generic to make a call

    # Word-boundary regex matching — avoids "infinite" matching inside
    # "reconnection" (round-2 QA P1 #1). re.escape() handles tokens
    # that contain regex metacharacters (e.g., "C++").
    before_hits = 0
    after_hits = 0
    for token in desc_tokens:
        pattern = _re.compile(rf"\b{_re.escape(token)}\b", _re.IGNORECASE)
        if pattern.search(before_block):
            before_hits += 1
        if pattern.search(after_block):
            after_hits += 1

    # If `after` mentions the buggy keywords MORE than `before`, it's
    # likely reverting toward the bug.
    return after_hits > before_hits and after_hits > 0


def reset(project_root: Path) -> None:
    """Tests only — drop the cached connection and delete the DB."""
    pr = project_root.resolve()
    with _conn_cache_lock:
        conn = _conn_cache.pop(pr, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    db_path = _db_path(pr)
    if db_path.exists():
        db_path.unlink()
