"""
_sqlite_util.py — shared SQLite plumbing for codevira's DB modules.

Pillar 3.3 of the v2.0 master plan. Until v2.0-rc.1, the
``_enable_wal_with_retry`` helper was duplicated verbatim in
``indexer/sqlite_graph.py`` and ``indexer/global_db.py`` (~25 lines each).
Factored out here.

Public API:

    enable_wal_with_retry(conn, db_path, *, attempts=10, initial_delay=0.02)
        Best-effort enable of SQLite WAL journal mode. Survives
        concurrent-open races via short-backoff retry. Returns None;
        logs a warning if WAL couldn't be enabled (caller continues in
        default mode — non-fatal).

The bound on retries is conservative: 10 attempts × geometric backoff
(0.02s → 0.2s cap) — total worst case ~1.5s. Master plan called for the
cap to be raised from 200ms to 500ms based on heavily-contended-system
feedback; we ship 200ms cap here matching the historical default.
v2.0.x can raise it via env var if real-world signal demands.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def enable_wal_with_retry(
    conn: sqlite3.Connection,
    db_path: Path | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.02,
) -> None:
    """Best-effort enable of WAL journal mode on a SQLite connection.

    Why a helper at all: ``PRAGMA journal_mode=WAL`` requires an
    EXCLUSIVE lock; if another process is mid-open on the same file,
    SQLite returns ``OperationalError: database is locked``. We retry
    with short geometric backoff. If WAL is already active (set by
    a prior open) we short-circuit — every connection sees the WAL
    journal regardless of who set it.

    Non-fatal failure mode: if all retries exhaust, we log a warning
    and let the caller continue in the default journal mode. WAL's
    benefit (readers don't block writers) is lost but write-write
    serialization still works.

    Args:
        conn: an open sqlite3 connection.
        db_path: the DB file path (used only for the warning message).
        attempts: number of retry attempts (default 10).
        initial_delay: backoff start (default 0.02s; geometric ×1.5
            up to 0.2s cap → ~1.5s total worst case).
    """
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            return  # already WAL — nothing to do
    except sqlite3.OperationalError:
        pass  # fall through to the retry loop

    delay = initial_delay
    for _ in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.2)

    # All attempts exhausted — log + continue in default mode.
    logger.warning(
        "Could not enable WAL on %s after %d retries; "
        "continuing in default journal mode",
        db_path, attempts,
    )
