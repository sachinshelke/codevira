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

            -- 4.0: `tenant` and the UNIQUE that includes it are here so a
            -- FRESH install lands on the right schema directly. Existing
            -- databases are carried over by _add_tenant_column() +
            -- _widen_unique_to_tenant() below.
            CREATE TABLE IF NOT EXISTS global_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                signal TEXT NOT NULL,
                example TEXT,
                frequency INTEGER DEFAULT 1,
                source_projects TEXT DEFAULT '[]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                tenant TEXT NOT NULL DEFAULT 'local',
                UNIQUE(tenant, category, signal)
            );

            CREATE TABLE IF NOT EXISTS global_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                source_projects TEXT DEFAULT '[]',
                category TEXT,
                language TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                tenant TEXT NOT NULL DEFAULT 'local',
                UNIQUE(tenant, rule_text)
            );
        """)
        self.conn.commit()
        self._add_tenant_column()
        self._widen_unique_to_tenant()

    # ------------------------------------------------------------------
    # 4.0 Step 10 — tenant scoping
    # ------------------------------------------------------------------

    #: Tables whose rows belong to a person rather than to the machine.
    _TENANT_TABLES = ("global_preferences", "global_rules")

    def _add_tenant_column(self) -> None:
        """Add ``tenant`` to the cross-project tables, backfilled to
        ``local``.

        This is 4.0's one genuinely breaking schema change, and the break
        is in the READ direction: a 3.x client has no WHERE tenant clause,
        so on a shared home it would read every tenant's preferences as
        its own. Adding the column cannot be downgraded away.

        Writing it is safe in every direction that matters. The column is
        additive (``ALTER TABLE ADD COLUMN``, no rewrite), every existing
        row is backfilled to ``local``, and a 3.x client ignores a column
        it does not select. A single-user install therefore sees no
        change at all — which is the overwhelmingly common case and the
        one least entitled to be disrupted by a multi-tenant feature.

        ``projects`` is deliberately NOT scoped: it is an inventory of
        what exists on this filesystem, which is a property of the
        machine, not of a person. Scoping it would hide a colleague's
        project from `codevira projects` on a shared box and make the
        registry lie about what is there.
        """
        from mcp_server.tenant import DEFAULT_TENANT

        for table in self._TENANT_TABLES:
            try:
                cols = [
                    row[1]
                    for row in self.conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                ]
                if "tenant" in cols:
                    continue
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant TEXT "
                    f"NOT NULL DEFAULT '{DEFAULT_TENANT}'"
                )
                # Backfill is what the DEFAULT already does for existing
                # rows in SQLite, but being explicit costs nothing and
                # makes the migration legible in a diff.
                self.conn.execute(
                    f"UPDATE {table} SET tenant = ? WHERE tenant IS NULL",
                    (DEFAULT_TENANT,),
                )
                self.conn.commit()
                logger.info("global_db: added tenant column to %s", table)
            except sqlite3.Error as exc:
                # Never make an unmigrated global.db unusable. Reads fall
                # back to unscoped behaviour, which is exactly 3.x.
                logger.warning(
                    "global_db: could not add tenant column to %s: %s", table, exc
                )

    def _has_tenant(self, table: str) -> bool:
        try:
            return "tenant" in [
                r[1]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
        except sqlite3.Error:
            return False

    def _tenant(self) -> str:
        """Tenant owning this connection's writes. ``local`` if anything
        goes wrong — never an empty string, which would create an
        invisible partition nothing else could match."""
        try:
            from mcp_server.tenant import current_tenant

            return current_tenant()
        except Exception:  # noqa: BLE001
            from mcp_server.tenant import DEFAULT_TENANT

            return DEFAULT_TENANT

    #: Rebuilds needed because the original UNIQUE constraints predate
    #: tenancy. ``UNIQUE(category, signal)`` means tenant A holding
    #: "communication/be-concise" BLOCKS tenant B from ever storing it —
    #: the upsert finds no row for B, inserts, and hits the constraint.
    #: Isolation is not optional-extra here; without this the tenant
    #: column would be decorative.
    _TENANT_REBUILDS = {
        "global_preferences": (
            """CREATE TABLE global_preferences__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                signal TEXT NOT NULL,
                example TEXT,
                frequency INTEGER DEFAULT 1,
                source_projects TEXT DEFAULT '[]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                tenant TEXT NOT NULL DEFAULT 'local',
                UNIQUE(tenant, category, signal)
            )""",
            "category, signal, example, frequency, source_projects, updated_at, tenant",
        ),
        "global_rules": (
            """CREATE TABLE global_rules__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                source_projects TEXT DEFAULT '[]',
                category TEXT,
                language TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                tenant TEXT NOT NULL DEFAULT 'local',
                UNIQUE(tenant, rule_text)
            )""",
            "rule_text, confidence, source_projects, category, language, updated_at, tenant",
        ),
    }

    def _widen_unique_to_tenant(self) -> None:
        """Rebuild the cross-project tables so UNIQUE includes ``tenant``.

        SQLite cannot ALTER a constraint, so this is create-copy-swap. It
        runs inside one transaction: either the new table is in place with
        every row, or nothing changed. It is a no-op once the constraint
        already mentions ``tenant``, so it costs one sqlite_master read on
        every subsequent open.

        Every row is carried over — this migration must not be the thing
        that loses someone's learned preferences.
        """
        for table, (ddl, cols) in self._TENANT_REBUILDS.items():
            try:
                row = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not row or not row[0]:
                    continue
                if "UNIQUE(tenant" in row[0].replace(" ", "").replace("\n", ""):
                    continue  # already migrated

                before = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                    0
                ]

                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(ddl)
                self.conn.execute(
                    f"INSERT INTO {table}__new ({cols}) SELECT {cols} FROM {table}"
                )
                self.conn.execute(f"DROP TABLE {table}")
                self.conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
                after = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if after != before:
                    self.conn.execute("ROLLBACK")
                    logger.error(
                        "global_db: tenant rebuild of %s would have lost rows "
                        "(%d -> %d); rolled back",
                        table,
                        before,
                        after,
                    )
                    continue
                self.conn.commit()
                logger.info(
                    "global_db: widened %s UNIQUE to include tenant (%d rows)",
                    table,
                    after,
                )
            except sqlite3.Error as exc:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                logger.warning(
                    "global_db: tenant UNIQUE rebuild skipped for %s: %s", table, exc
                )

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
        tenant = self._tenant()
        scoped = self._has_tenant("global_preferences")
        existing = self.conn.execute(
            "SELECT id, frequency, source_projects FROM global_preferences "
            "WHERE category = ? AND signal = ?" + (" AND tenant = ?" if scoped else ""),
            (category, signal, tenant) if scoped else (category, signal),
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
            if scoped:
                self.conn.execute(
                    "INSERT INTO global_preferences "
                    "(category, signal, example, frequency, source_projects, tenant) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        category,
                        signal,
                        example,
                        frequency,
                        json.dumps([source_project]),
                        tenant,
                    ),
                )
            else:
                self.conn.execute(
                    "INSERT INTO global_preferences "
                    "(category, signal, example, frequency, source_projects) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        category,
                        signal,
                        example,
                        frequency,
                        json.dumps([source_project]),
                    ),
                )
        self.conn.commit()

    def get_preferences(
        self, min_frequency: int = 3, language: str | None = None
    ) -> list[dict]:
        """Get global preferences above the frequency threshold."""
        scoped = self._has_tenant("global_preferences")
        rows = self.conn.execute(
            "SELECT category, signal, example, frequency, source_projects "
            "FROM global_preferences WHERE frequency >= ?"
            + (" AND tenant = ?" if scoped else "")
            + " ORDER BY frequency DESC",
            (min_frequency, self._tenant()) if scoped else (min_frequency,),
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
        tenant = self._tenant()
        scoped = self._has_tenant("global_rules")
        existing = self.conn.execute(
            "SELECT id, confidence, source_projects FROM global_rules "
            "WHERE rule_text = ?" + (" AND tenant = ?" if scoped else ""),
            (rule_text, tenant) if scoped else (rule_text,),
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
        elif scoped:
            self.conn.execute(
                "INSERT INTO global_rules "
                "(rule_text, confidence, source_projects, category, language, tenant) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rule_text,
                    confidence,
                    json.dumps([source_project]),
                    category,
                    language,
                    tenant,
                ),
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
        # 4.0: tenant scoping. This read is the leak the tenant key
        # exists to close — an unscoped SELECT here would hand one
        # person's learned rules to another on a shared home, which is
        # the same class of bug as the 2026-05-18 language leak above.
        scoped = self._has_tenant("global_rules")
        cols = (
            "SELECT rule_text, confidence, source_projects, category, language "
            "FROM global_rules WHERE confidence >= ?"
        )
        tenant_clause = " AND tenant = ?" if scoped else ""
        params: tuple

        if language:
            lang_clause = (
                " AND language = ?"
                if strict_language
                else " AND (language = ? OR language IS NULL)"
            )
            sql = cols + lang_clause + tenant_clause + " ORDER BY confidence DESC"
            params = (
                (min_confidence, language, self._tenant())
                if scoped
                else (min_confidence, language)
            )
        else:
            sql = cols + tenant_clause + " ORDER BY confidence DESC"
            params = (min_confidence, self._tenant()) if scoped else (min_confidence,)

        rows = self.conn.execute(sql, params).fetchall()
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
