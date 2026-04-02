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
        self.conn = sqlite3.connect(str(self.db_path), timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT,
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

    def register_project(self, path: str, name: str, language: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO projects (path, name, language, last_synced_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (path, name, language),
        )
        self.conn.commit()

    def get_project_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def upsert_preference(self, category: str, signal: str, example: str | None,
                          source_project: str, frequency: int = 1) -> None:
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

    def get_preferences(self, min_frequency: int = 3, language: str | None = None) -> list[dict]:
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

    def upsert_rule(self, rule_text: str, confidence: float, source_project: str,
                    category: str | None = None, language: str | None = None) -> None:
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
                (rule_text, confidence, json.dumps([source_project]), category, language),
            )
        self.conn.commit()

    def get_rules(self, min_confidence: float = 0.6, language: str | None = None) -> list[dict]:
        """Get global rules above confidence threshold, optionally filtered by language."""
        if language:
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
            "total_preferences": self.conn.execute("SELECT COUNT(*) FROM global_preferences").fetchone()[0],
            "total_rules": self.conn.execute("SELECT COUNT(*) FROM global_rules").fetchone()[0],
        }
