"""
log_retention.py — Enforce logs.retention_days from project config.yaml.

By default (retention_days=0) Codevira keeps session logs and decisions
forever — that's the core "persistent memory" value prop. Users who set
retention_days > 0 opt into time-based cleanup, usually for privacy
reasons on sensitive projects.

Implementation notes:
- Deletes both sessions and their linked decisions (FK cascade)
- Runs at most once per 24h per process (tracked via a marker file)
  to avoid scanning on every server startup
- Failure is non-fatal: cleanup errors are logged to crash_logger but
  don't prevent the server from starting
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Run cleanup at most every 24 hours to avoid churning the DB on every
# tool call / server restart.
_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


def _marker_path(data_dir: Path) -> Path:
    """Location of the last-cleanup timestamp marker file."""
    return data_dir / "logs" / ".last_retention_cleanup"


def _should_run_cleanup(data_dir: Path) -> bool:
    """True if >= 24h have passed since the last cleanup."""
    marker = _marker_path(data_dir)
    if not marker.exists():
        return True
    try:
        last_run = float(marker.read_text().strip())
        return (time.time() - last_run) >= _CLEANUP_INTERVAL_SECONDS
    except (ValueError, OSError):
        return True


def _mark_cleanup_done(data_dir: Path) -> None:
    """Record that cleanup just ran (atomic)."""
    from mcp_server.storage.atomic import atomic_write_text

    marker = _marker_path(data_dir)
    try:
        atomic_write_text(marker, str(time.time()))
    except OSError:
        pass


def _read_retention_days(data_dir: Path) -> int:
    """Read logs.retention_days from config.yaml. Default: 0 (keep forever)."""
    config_path = data_dir / "config.yaml"
    if not config_path.is_file():
        return 0
    try:
        import yaml

        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return int(raw.get("logs", {}).get("retention_days", 0))
    except Exception:
        return 0


def enforce_retention(data_dir: Path | None = None, *, force: bool = False) -> dict:
    """Delete sessions + decisions older than retention_days.

    Args:
        data_dir: Project data directory. Defaults to current project's data dir.
        force: Skip the 24h interval gate (useful for tests and CLI).

    Returns:
        {
            "enabled": bool,            # retention_days > 0
            "retention_days": int,
            "ran": bool,                # cleanup actually executed
            "sessions_deleted": int,
            "decisions_deleted": int,
        }
    """
    from mcp_server.paths import get_data_dir

    if data_dir is None:
        data_dir = get_data_dir()

    retention_days = _read_retention_days(data_dir)

    result = {
        "enabled": retention_days > 0,
        "retention_days": retention_days,
        "ran": False,
        "sessions_deleted": 0,
        "decisions_deleted": 0,
    }

    # Default (keep forever) — no-op
    if retention_days <= 0:
        return result

    # Skip if we just ran recently (unless forced)
    if not force and not _should_run_cleanup(data_dir):
        return result

    # Enforce retention via SQL
    graph_db = data_dir / "graph" / "graph.db"
    if not graph_db.exists():
        return result

    try:
        import sqlite3

        conn = sqlite3.connect(str(graph_db))
        conn.row_factory = sqlite3.Row

        cutoff_sql = "datetime('now', ?)"
        cutoff_arg = f"-{retention_days} days"

        # Count first so we can return accurate stats
        old_sessions = conn.execute(
            f"SELECT session_id FROM sessions WHERE created_at < {cutoff_sql}",
            (cutoff_arg,),
        ).fetchall()
        session_ids = [r["session_id"] for r in old_sessions]

        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            decision_count = conn.execute(
                f"SELECT COUNT(*) FROM decisions WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchone()[0]

            conn.execute(
                f"DELETE FROM decisions WHERE session_id IN ({placeholders})",
                session_ids,
            )
            conn.execute(
                f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            )
            conn.commit()

            result["sessions_deleted"] = len(session_ids)
            result["decisions_deleted"] = decision_count

        conn.close()
        result["ran"] = True
        _mark_cleanup_done(data_dir)

        if result["sessions_deleted"] > 0:
            logger.info(
                "Retention cleanup: deleted %d sessions, %d decisions older than %d days",
                result["sessions_deleted"],
                result["decisions_deleted"],
                retention_days,
            )
    except Exception as e:
        logger.warning("Retention cleanup failed: %s", e)
        try:
            from mcp_server.crash_logger import log_crash

            log_crash(e, context="log_retention.enforce_retention")
        except Exception:
            pass

    return result
