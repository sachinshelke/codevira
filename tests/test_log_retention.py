"""
Tests for mcp_server/log_retention.py — the logs.retention_days enforcement.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import yaml

from mcp_server.log_retention import (
    enforce_retention,
    _read_retention_days,
    _should_run_cleanup,
    _mark_cleanup_done,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, retention_days: int = 0) -> Path:
    """Create a minimal project data dir with config.yaml + graph.db."""
    data_dir = tmp_path / "project-data"
    data_dir.mkdir()
    (data_dir / "logs").mkdir()
    (data_dir / "graph").mkdir()

    config = {"project": {"name": "t"}, "logs": {"retention_days": retention_days}}
    (data_dir / "config.yaml").write_text(yaml.safe_dump(config))

    # Create graph.db with sessions + decisions tables
    db_path = data_dir / "graph" / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            summary TEXT,
            phase TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            file_path TEXT,
            decision TEXT,
            context TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
    """)
    conn.commit()
    conn.close()
    return data_dir


def _insert_session(data_dir: Path, session_id: str, days_ago: int, n_decisions: int = 0):
    """Insert a session with created_at set to N days ago, plus N decisions."""
    db_path = data_dir / "graph" / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO sessions (session_id, summary, phase, created_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (session_id, f"Session {session_id}", "phase1", f"-{days_ago} days"),
    )
    for i in range(n_decisions):
        conn.execute(
            "INSERT INTO decisions (session_id, file_path, decision, context, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now', ?))",
            (session_id, f"src/f{i}.py", f"decision-{i}", "ctx", f"-{days_ago} days"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# _read_retention_days
# ---------------------------------------------------------------------------

class TestReadRetentionDays:
    def test_reads_value_from_config(self, tmp_path):
        data_dir = _make_project(tmp_path, retention_days=30)
        assert _read_retention_days(data_dir) == 30

    def test_defaults_to_zero_when_missing(self, tmp_path):
        data_dir = tmp_path / "p"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("project:\n  name: t\n")
        assert _read_retention_days(data_dir) == 0

    def test_returns_zero_when_no_config(self, tmp_path):
        assert _read_retention_days(tmp_path / "nonexistent") == 0

    def test_returns_zero_on_malformed_yaml(self, tmp_path):
        data_dir = tmp_path / "p"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("not: valid: yaml: [[[")
        assert _read_retention_days(data_dir) == 0


# ---------------------------------------------------------------------------
# _should_run_cleanup / _mark_cleanup_done
# ---------------------------------------------------------------------------

class TestCleanupInterval:
    def test_runs_when_no_marker_exists(self, tmp_path):
        data_dir = _make_project(tmp_path)
        assert _should_run_cleanup(data_dir) is True

    def test_skips_within_24h(self, tmp_path):
        data_dir = _make_project(tmp_path)
        _mark_cleanup_done(data_dir)
        assert _should_run_cleanup(data_dir) is False

    def test_runs_after_24h(self, tmp_path):
        data_dir = _make_project(tmp_path)
        # Marker written 25 hours ago
        marker = data_dir / "logs" / ".last_retention_cleanup"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time() - 25 * 3600))
        assert _should_run_cleanup(data_dir) is True

    def test_runs_on_corrupt_marker(self, tmp_path):
        data_dir = _make_project(tmp_path)
        marker = data_dir / "logs" / ".last_retention_cleanup"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not a float")
        assert _should_run_cleanup(data_dir) is True


# ---------------------------------------------------------------------------
# enforce_retention
# ---------------------------------------------------------------------------

class TestEnforceRetention:
    def test_retention_zero_is_noop(self, tmp_path):
        """retention_days=0 returns enabled=False and runs nothing."""
        data_dir = _make_project(tmp_path, retention_days=0)
        _insert_session(data_dir, "old", days_ago=100, n_decisions=2)

        result = enforce_retention(data_dir=data_dir, force=True)
        assert result["enabled"] is False
        assert result["ran"] is False
        assert result["sessions_deleted"] == 0
        assert result["decisions_deleted"] == 0

        # Verify nothing was deleted
        conn = sqlite3.connect(str(data_dir / "graph" / "graph.db"))
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        conn.close()

    def test_deletes_old_sessions(self, tmp_path):
        """With retention_days=30, sessions older than 30 days are deleted."""
        data_dir = _make_project(tmp_path, retention_days=30)
        _insert_session(data_dir, "old-1", days_ago=45, n_decisions=2)
        _insert_session(data_dir, "old-2", days_ago=60, n_decisions=1)
        _insert_session(data_dir, "recent", days_ago=5, n_decisions=3)

        result = enforce_retention(data_dir=data_dir, force=True)

        assert result["enabled"] is True
        assert result["ran"] is True
        assert result["sessions_deleted"] == 2
        assert result["decisions_deleted"] == 3  # 2 + 1

        conn = sqlite3.connect(str(data_dir / "graph" / "graph.db"))
        remaining = conn.execute("SELECT session_id FROM sessions").fetchall()
        conn.close()
        assert {r[0] for r in remaining} == {"recent"}

    def test_nothing_to_delete(self, tmp_path):
        """All sessions are recent — nothing deleted."""
        data_dir = _make_project(tmp_path, retention_days=30)
        _insert_session(data_dir, "recent", days_ago=5)

        result = enforce_retention(data_dir=data_dir, force=True)
        assert result["ran"] is True
        assert result["sessions_deleted"] == 0
        assert result["decisions_deleted"] == 0

    def test_skipped_when_cleanup_ran_recently(self, tmp_path):
        """Without force=True, cleanup skips if marker is fresh."""
        data_dir = _make_project(tmp_path, retention_days=30)
        _insert_session(data_dir, "old", days_ago=100)
        _mark_cleanup_done(data_dir)

        result = enforce_retention(data_dir=data_dir, force=False)
        assert result["enabled"] is True
        assert result["ran"] is False
        assert result["sessions_deleted"] == 0

        # Session should still exist
        conn = sqlite3.connect(str(data_dir / "graph" / "graph.db"))
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_missing_graph_db_is_noop(self, tmp_path):
        """No graph.db (uninitialized project) — don't crash."""
        data_dir = tmp_path / "p"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(
            yaml.safe_dump({"logs": {"retention_days": 30}})
        )

        result = enforce_retention(data_dir=data_dir, force=True)
        assert result["enabled"] is True
        assert result["ran"] is False
        assert result["sessions_deleted"] == 0

    def test_marker_written_after_successful_run(self, tmp_path):
        """After cleanup runs, the 24h marker is written."""
        data_dir = _make_project(tmp_path, retention_days=30)
        _insert_session(data_dir, "old", days_ago=100)

        marker = data_dir / "logs" / ".last_retention_cleanup"
        assert not marker.exists()

        enforce_retention(data_dir=data_dir, force=True)

        assert marker.exists()
        # Marker should contain a valid timestamp
        ts = float(marker.read_text().strip())
        assert abs(ts - time.time()) < 5  # written within last 5 seconds

    def test_decision_count_includes_orphans(self, tmp_path):
        """Decisions without matching sessions aren't touched (FK integrity)."""
        data_dir = _make_project(tmp_path, retention_days=30)
        _insert_session(data_dir, "old", days_ago=100, n_decisions=2)

        # Insert an orphan decision (no matching session row)
        conn = sqlite3.connect(str(data_dir / "graph" / "graph.db"))
        conn.execute(
            "INSERT INTO decisions (session_id, file_path, decision, context) "
            "VALUES (?, ?, ?, ?)",
            ("nonexistent-session", "src/x.py", "d", "c"),
        )
        conn.commit()
        conn.close()

        result = enforce_retention(data_dir=data_dir, force=True)
        assert result["sessions_deleted"] == 1
        assert result["decisions_deleted"] == 2  # only the 2 linked to "old"
