"""
global_db.py — Global SQLite database for cross-project intelligence.

Stores aggregated preferences, learned rules, and project registry in
~/.codevira/global.db. Enables new projects to inherit intelligence from
all past projects on day 1.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class GlobalDB:
    """Lightweight SQLite wrapper for the global cross-project database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 30s timeout (up from 5s): handles later-write contention under load.
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        # Enable WAL with retries. `PRAGMA journal_mode=WAL` requires an
        # exclusive lock and — unlike normal SQL — does NOT honour the
        # `busy_timeout`. When multiple threads/processes open the same fresh
        # database file concurrently, they all race to flip the journal mode
        # and some raise `OperationalError('database is locked')`. Skip the
        # PRAGMA if WAL is already the effective mode, otherwise retry with
        # short backoff. After ~1s we give up and fall through — the DB still
        # works in the default rollback-journal mode.
        self._enable_wal_with_retry()
        self.conn.execute("PRAGMA foreign_keys=ON")
        # SQLite-level busy timeout for subsequent writes (complements the
        # `timeout=30` on the connect — matters for later transactions).
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()
        # Bug 20 (rc.4): collapse legacy duplicate rows where the same logical
        # project was registered under two paths (the canonical project_root
        # AND the ~/.codevira/projects/<slug> storage path). One-shot, fast,
        # silent on no-op. Logic lives in indexer/_dedupe_migration.py — kept
        # out of this hot file to minimise blast radius.
        try:
            from indexer._dedupe_migration import dedupe_projects_by_git_remote

            dedupe_projects_by_git_remote(self.conn)
        except Exception as e:
            logger.warning("Bug 20 dedupe migration failed (continuing): %s", e)

    def _enable_wal_with_retry(
        self, attempts: int = 10, initial_delay: float = 0.02
    ) -> None:
        """Best-effort enable of WAL journal mode.

        Pillar 3.3 (v2.0-rc.1): the implementation moved to the shared
        helper ``indexer._sqlite_util.enable_wal_with_retry``. This
        method is kept as a thin shim for backward compatibility; new
        code should call the shared helper directly.
        """
        from indexer._sqlite_util import enable_wal_with_retry

        enable_wal_with_retry(
            self.conn,
            self.db_path,
            attempts=attempts,
            initial_delay=initial_delay,
        )

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT,
                git_remote TEXT,
                last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS global_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                signal TEXT NOT NULL,
                example TEXT,
                frequency INTEGER DEFAULT 1,
                source_projects TEXT DEFAULT '[]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, signal)
            );

            CREATE TABLE IF NOT EXISTS global_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL UNIQUE,
                confidence REAL DEFAULT 0.5,
                source_projects TEXT DEFAULT '[]',
                category TEXT,
                language TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Project registry
    # ------------------------------------------------------------------

    def register_project(
        self, path: str, name: str, language: str, git_remote: str | None = None
    ) -> None:
        # Ensure git_remote column exists (handles DBs created before v1.6)
        try:
            cols = [
                row[1]
                for row in self.conn.execute("PRAGMA table_info(projects)").fetchall()
            ]
            if "git_remote" not in cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN git_remote TEXT")
                self.conn.commit()
        except Exception:
            pass
        # P0-7 (rc.5): protect git_remote from being silently cleared. The
        # old INSERT OR REPLACE wrote NULL to git_remote whenever the caller
        # passed None — which subsequent code paths (e.g. doctor → MCP server
        # → auto_init re-register) often do. That broke the Bug-20 dedup
        # invariant: rows lost their identity column right after they were
        # set. Now we COALESCE — existing non-null git_remote is preserved
        # when the caller passes None.
        self.conn.execute(
            """
            INSERT INTO projects (path, name, language, git_remote, last_synced_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                language = excluded.language,
                git_remote = COALESCE(excluded.git_remote, projects.git_remote),
                last_synced_at = CURRENT_TIMESTAMP
            """,
            (path, name, language, git_remote),
        )
        self.conn.commit()

    def get_project_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def upsert_preference(
        self,
        category: str,
        signal: str,
        example: str | None,
        source_project: str,
        frequency: int = 1,
    ) -> None:
        """Insert or update a global preference. Aggregates frequency across projects."""
        existing = self.conn.execute(
            "SELECT id, frequency, source_projects FROM global_preferences WHERE category = ? AND signal = ?",
            (category, signal),
        ).fetchone()

        if existing:
            projects = json.loads(existing["source_projects"] or "[]")
            if source_project not in projects:
                projects.append(source_project)
            new_freq = existing["frequency"] + frequency
            self.conn.execute(
                "UPDATE global_preferences SET frequency = ?, source_projects = ?, example = COALESCE(?, example), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_freq, json.dumps(projects), example, existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO global_preferences (category, signal, example, frequency, source_projects) "
                "VALUES (?, ?, ?, ?, ?)",
                (category, signal, example, frequency, json.dumps([source_project])),
            )
        self.conn.commit()

    def get_preferences(
        self, min_frequency: int = 3, language: str | None = None
    ) -> list[dict]:
        """Get global preferences above the frequency threshold."""
        rows = self.conn.execute(
            "SELECT category, signal, example, frequency, source_projects FROM global_preferences "
            "WHERE frequency >= ? ORDER BY frequency DESC",
            (min_frequency,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def upsert_rule(
        self,
        rule_text: str,
        confidence: float,
        source_project: str,
        category: str | None = None,
        language: str | None = None,
    ) -> None:
        """Insert or update a global rule. Merges confidence via weighted average."""
        existing = self.conn.execute(
            "SELECT id, confidence, source_projects FROM global_rules WHERE rule_text = ?",
            (rule_text,),
        ).fetchone()

        if existing:
            projects = json.loads(existing["source_projects"] or "[]")
            if source_project not in projects:
                projects.append(source_project)
            new_conf = existing["confidence"] * 0.6 + confidence * 0.4
            self.conn.execute(
                "UPDATE global_rules SET confidence = ?, source_projects = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_conf, json.dumps(projects), existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO global_rules (rule_text, confidence, source_projects, category, language) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    rule_text,
                    confidence,
                    json.dumps([source_project]),
                    category,
                    language,
                ),
            )
        self.conn.commit()

    def get_rules(
        self,
        min_confidence: float = 0.6,
        language: str | None = None,
        *,
        strict_language: bool = True,
    ) -> list[dict]:
        """Get global rules above confidence threshold, optionally filtered by language.

        2026-05-18 v2.1.2 Item 9 (cross-project rules leak fix): when
        ``language`` is supplied, we now STRICTLY require a match. The
        prior behavior (``language = ? OR language IS NULL``) leaked
        rules from projects that had no language set into every other
        project — Report 1 §3.3 caught a Go-project rule appearing in
        a Python project. Set ``strict_language=False`` to opt back into
        the loose behavior for legacy callers (none exist in v2.1.2).
        """
        if language:
            if strict_language:
                rows = self.conn.execute(
                    "SELECT rule_text, confidence, source_projects, category, language FROM global_rules "
                    "WHERE confidence >= ? AND language = ? ORDER BY confidence DESC",
                    (min_confidence, language),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT rule_text, confidence, source_projects, category, language FROM global_rules "
                    "WHERE confidence >= ? AND (language = ? OR language IS NULL) ORDER BY confidence DESC",
                    (min_confidence, language),
                ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT rule_text, confidence, source_projects, category, language FROM global_rules "
                "WHERE confidence >= ? ORDER BY confidence DESC",
                (min_confidence,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return summary stats for the global database."""
        return {
            "project_count": self.get_project_count(),
            "total_preferences": self.conn.execute(
                "SELECT COUNT(*) FROM global_preferences"
            ).fetchone()[0],
            "total_rules": self.conn.execute(
                "SELECT COUNT(*) FROM global_rules"
            ).fetchone()[0],
        }
