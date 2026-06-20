from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


def _is_duplicate(
    new_decision: str, existing_decisions: list[str], threshold: float = 0.8
) -> bool:
    """True when ``new_decision`` overlaps any entry in ``existing_decisions`` at ``threshold``.

    v1.8 dedup signal for :meth:`SQLiteGraph.log_session` — iterative agent runs
    routinely log the same intent 5+ times per day, which bloats the decision
    log and blunts ``search_decisions`` / ``get_session_context``.

    Comparison is token-set overlap on lowercased whitespace splits (no
    embeddings, no stemming, no stop-word removal):

        overlap = |A ∩ B| / max(|A|, |B|)

    - ``max`` (not ``min``) so adding three words to an old decision doesn't
      score 1.0 just because the old decision is a subset.
    - Very short decisions (< 3 tokens) always return False — too noisy to
      reliably dedup.
    - An empty ``existing_decisions`` list trivially returns False.
    """
    new_tokens = set(new_decision.lower().split())
    if len(new_tokens) < 3:
        return False
    for existing in existing_decisions:
        existing_tokens = set(existing.lower().split())
        if not existing_tokens:
            continue
        overlap = len(new_tokens & existing_tokens) / max(
            len(new_tokens), len(existing_tokens)
        )
        if overlap >= threshold:
            return True
    return False


class SQLiteGraph:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 30s timeout (up from sqlite3 default of 5s): the background watcher's
        # reindex thread and any concurrent MCP server connection both write to
        # this graph.db. v1.8.0 fixed the same race for GlobalDB; v1.8.1 ports
        # the fix here after `OperationalError("database is locked")` in
        # add_symbol/remove_symbols_for_file showed up in production crash logs.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # Enable WAL with retries — see global_db._enable_wal_with_retry for
        # the SQLite race rationale (PRAGMA journal_mode=WAL doesn't honor
        # busy_timeout; concurrent opens collide on EXCLUSIVE lock).
        self._enable_wal_with_retry()
        # Belt-and-braces: SQLite-level busy_timeout for subsequent writes.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_db()

    def _enable_wal_with_retry(
        self, attempts: int = 10, initial_delay: float = 0.02
    ) -> None:
        """Best-effort enable of WAL journal mode.

        Pillar 3.3 (v2.0-rc.1): the implementation moved to the shared
        helper ``indexer._sqlite_util.enable_wal_with_retry`` to dedup
        the same logic in :class:`indexer.global_db.GlobalDB`. This
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

    @contextmanager
    def transaction(self):
        with self.conn:
            yield self.conn

    def _init_db(self):
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    docstring TEXT,
                    is_public BOOLEAN,

                    -- User/Agent Metadata
                    role TEXT,
                    type TEXT,
                    rules TEXT,
                    key_functions TEXT,
                    dependencies TEXT,
                    stability TEXT DEFAULT 'medium',
                    do_not_revert BOOLEAN DEFAULT 0,
                    layer TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_nodes_file_path ON nodes(file_path);
                CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

                CREATE TABLE IF NOT EXISTS edges (
                    source_id TEXT,
                    target_id TEXT,
                    kind TEXT NOT NULL,
                    line INTEGER,
                    PRIMARY KEY (source_id, target_id, kind),
                    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);

                -- ----------------------------------------------------
                -- Hashing for Incremental Indexing
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS file_hashes (
                    file_path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    last_indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                -- ----------------------------------------------------
                -- Memory & Session Logs
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    summary TEXT,
                    phase TEXT
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    file_path TEXT,
                    decision TEXT NOT NULL,
                    context TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_session ON decisions(session_id);
                CREATE INDEX IF NOT EXISTS idx_decisions_file ON decisions(file_path);

                -- ----------------------------------------------------
                -- v1.4: Outcome Tracking (feedback loop)
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    decision_id INTEGER,
                    outcome_type TEXT NOT NULL,  -- 'kept' | 'modified' | 'reverted'
                    delta_summary TEXT,
                    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_outcomes_session ON outcomes(session_id);
                CREATE INDEX IF NOT EXISTS idx_outcomes_file ON outcomes(file_path);

                -- ----------------------------------------------------
                -- v1.4: Developer Preferences (learned from corrections)
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,    -- 'naming' | 'structure' | 'patterns' | 'formatting'
                    signal TEXT NOT NULL,
                    example TEXT,
                    frequency INTEGER DEFAULT 1,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_preferences_category ON preferences(category);

                -- ----------------------------------------------------
                -- v1.4: Learned Rules (auto-generated from patterns)
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS learned_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_text TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    source_sessions TEXT,      -- JSON array of session IDs
                    category TEXT,             -- 'testing' | 'imports' | 'structure' | 'naming'
                    file_pattern TEXT,         -- glob pattern this rule applies to
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_learned_rules_category ON learned_rules(category);

                -- ----------------------------------------------------
                -- v1.5: Function-level symbols and call graph
                -- ----------------------------------------------------
                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,           -- 'file:path.py::func_name'
                    file_node_id TEXT,             -- FK to nodes.id
                    name TEXT NOT NULL,
                    kind TEXT,                     -- 'function' | 'class' | 'method' | 'interface' | 'struct'
                    signature TEXT,
                    parameters TEXT,               -- JSON: [{name, type}]
                    return_type TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    docstring TEXT,
                    is_public BOOLEAN DEFAULT 1,
                    calls TEXT,                    -- JSON: ['func_a', 'func_b']
                    FOREIGN KEY (file_node_id) REFERENCES nodes(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_node_id);
                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

                CREATE TABLE IF NOT EXISTS call_edges (
                    caller_id TEXT,                -- symbols.id
                    callee_id TEXT,                -- symbols.id
                    line INTEGER,
                    PRIMARY KEY (caller_id, callee_id),
                    FOREIGN KEY (caller_id) REFERENCES symbols(id) ON DELETE CASCADE,
                    FOREIGN KEY (callee_id) REFERENCES symbols(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_call_edges_callee ON call_edges(callee_id);
                CREATE INDEX IF NOT EXISTS idx_call_edges_caller ON call_edges(caller_id);
            """)
            conn.execute("PRAGMA foreign_keys = ON;")

            # v1.5 migrations: add source column for global sync tracking
            try:
                conn.execute(
                    "ALTER TABLE preferences ADD COLUMN source TEXT DEFAULT 'local'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "ALTER TABLE learned_rules ADD COLUMN source TEXT DEFAULT 'local'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            # v2.0-rc.3 (Bug 3): retire stale learned rules from MCP.
            # When a rule is retired it stays in the table (audit trail)
            # but is filtered from default ``get_learned_rules()`` queries
            # so it doesn't keep firing false positives on deleted code.
            try:
                conn.execute(
                    "ALTER TABLE learned_rules ADD COLUMN retired_at DATETIME DEFAULT NULL"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "ALTER TABLE learned_rules ADD COLUMN retired_reason TEXT DEFAULT NULL"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            # v2.0-rc.3 (Bug 2): decision-level do_not_revert. Hero 1
            # positioning ("AI cannot undo your protected decisions")
            # was previously only honored at file granularity via
            # nodes.do_not_revert. Adding the same flag at decision
            # granularity lets the AI mark "use Postgres for the
            # cortex metadata store" as do_not_revert without locking
            # the entire file. ``search_decisions`` surfaces the flag
            # so policies / agents can respect it.
            try:
                conn.execute(
                    "ALTER TABLE decisions ADD COLUMN do_not_revert INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

    def add_node(self, node_id: str, kind: str, name: str, file_path: str, **kwargs):
        existing = self.get_node(node_id)
        fields = ["id", "kind", "name", "file_path"]
        values = [node_id, kind, name, file_path]

        metadata_fields = [
            "line_start",
            "line_end",
            "docstring",
            "is_public",
            "role",
            "type",
            "rules",
            "key_functions",
            "dependencies",
            "stability",
            "do_not_revert",
            "layer",
        ]

        for k in metadata_fields:
            if k in kwargs:
                fields.append(k)
                values.append(kwargs[k])
            elif existing and existing.get(k) is not None:
                fields.append(k)
                values.append(existing[k])

        placeholders = ",".join(["?"] * len(fields))
        query = (
            f"INSERT OR REPLACE INTO nodes ({','.join(fields)}) VALUES ({placeholders})"
        )

        with self.transaction() as conn:
            conn.execute(query, values)

    def update_node_metadata(self, node_id: str, **kwargs):
        valid_fields = [
            "role",
            "type",
            "rules",
            "key_functions",
            "dependencies",
            "stability",
            "do_not_revert",
            "layer",
        ]
        updates, values = [], []
        for k, v in kwargs.items():
            if k in valid_fields:
                updates.append(f"{k} = ?")
                values.append(v)
        if not updates:
            return

        values.append(node_id)
        query = f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?"
        with self.transaction() as conn:
            conn.execute(query, values)

    def get_node(self, node_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_node_by_path(self, file_path: str) -> dict | None:
        cur = self.conn.execute(
            'SELECT * FROM nodes WHERE file_path = ? AND kind = "file" LIMIT 1',
            (file_path,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_file_nodes(
        self,
        layer: str | None = None,
        stability: str | None = None,
        do_not_revert: bool | None = None,
    ) -> list[dict]:
        query = 'SELECT * FROM nodes WHERE kind = "file"'
        params: list[Any] = []
        if layer:
            query += " AND layer = ?"
            params.append(layer)
        if stability:
            query += " AND stability = ?"
            params.append(stability)
        if do_not_revert is not None:
            query += " AND do_not_revert = ?"
            params.append(1 if do_not_revert else 0)

        cur = self.conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    def count_nodes(self, kind: str | None = None) -> int:
        """Return the total node count, optionally filtered by kind.

        2026-05-17 Bug B fix: cmd_incremental uses this to distinguish
        "graph empty, never indexed" from "graph populated, no changes."
        The former returned "Index is up to date" as a lie; now it
        returns "graph has 0 nodes — run `codevira index --full`".

        Args:
            kind: optional node kind to filter (e.g. "file"). None = all kinds.

        Returns:
            Integer count. Returns 0 if the table is empty or doesn't
            exist (P4 defensive parsing — never crashes the caller).
        """
        try:
            if kind is None:
                cur = self.conn.execute("SELECT COUNT(*) FROM nodes")
            else:
                cur = self.conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE kind = ?", (kind,)
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            # Defensive: schema-version drift or table missing — treat as 0
            # so callers degrade gracefully (P9). Worst case the caller
            # offers the user "run --full" which is correct in this state too.
            return 0

    def clear(self) -> int:
        """Delete every node — and, via ``ON DELETE CASCADE``, its edges,
        symbols, and call_edges — for a true from-scratch wipe of this
        project's graph.

        ``codevira index --full`` (``cmd_full_rebuild``) calls this before
        rebuilding so the add-if-absent loop in ``generate_graph_sqlite``
        actually re-adds (and refreshes) every file-node instead of skipping
        those already present — the reason a re-run used to report
        "0 nodes". Cascade is relied upon: ``PRAGMA foreign_keys = ON`` is
        set at connect, so emptying ``nodes`` empties the dependent tables.

        Returns:
            The node count removed (0 if already empty / table missing —
            defensive, never raises).
        """
        try:
            removed = self.count_nodes()
            with self.transaction() as conn:
                conn.execute("DELETE FROM nodes")
            return removed
        except Exception:
            return 0

    def remove_node(self, node_id: str) -> bool:
        """Delete a single node and (via ``ON DELETE CASCADE``) its edges,
        symbols, and call_edges.

        Used to prune nodes for source files that no longer exist on disk so
        the graph — and ``codevira status``'s node count — tracks the
        filesystem and shrinks on deletion instead of accumulating orphans
        forever. (3.5.1)

        Returns:
            True if a node row was deleted, False otherwise (already gone /
            error — defensive, never raises).
        """
        try:
            with self.transaction() as conn:
                cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                return bool(cur.rowcount)
        except Exception:
            return False

    def get_blast_radius(self, node_id: str, max_depth: int = 3) -> list[dict]:
        query = """
            WITH RECURSIVE
            dependents(id, path, depth) AS (
                SELECT id, id, 0 FROM nodes WHERE id = ?
                UNION ALL
                SELECT e.source_id, d.path || '->' || e.source_id, d.depth + 1
                FROM edges e
                JOIN dependents d ON e.target_id = d.id
                WHERE d.depth < ? AND instr(d.path, e.source_id) = 0
            )
            SELECT DISTINCT n.*, d.depth
            FROM dependents d
            JOIN nodes n ON d.id = n.id
            WHERE d.id != ?
            ORDER BY d.depth ASC;
        """
        with self.transaction() as conn:
            cur = conn.execute(query, (node_id, max_depth, node_id))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Edge management
    # ------------------------------------------------------------------

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        kind: str = "imports",
        line: int | None = None,
    ):
        """Insert / replace an edge between two nodes.

        v2.0-rc.5 (Bug 13): same FK-race shape as ``add_call_edge``
        (Bug 9 in rc.4). When the watcher reindexes a file it deletes
        all that file's edges then re-adds them; a concurrent reindex
        on the target node can delete the row mid-flight, raising
        ``IntegrityError: FOREIGN KEY constraint failed`` and crashing
        the watcher. Same fix: silently drop edges referencing missing
        nodes via WHERE EXISTS subqueries. The edge is rebuilt on the
        next full reindex.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO edges (source_id, target_id, kind, line) "
                "SELECT ?, ?, ?, ? "
                "WHERE EXISTS (SELECT 1 FROM nodes WHERE id = ?) "
                "  AND EXISTS (SELECT 1 FROM nodes WHERE id = ?)",
                (source_id, target_id, kind, line, source_id, target_id),
            )

    def remove_edges_for_node(self, node_id: str):
        with self.transaction() as conn:
            conn.execute("DELETE FROM edges WHERE source_id = ?", (node_id,))

    def get_edges_from(self, node_id: str) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM edges WHERE source_id = ?", (node_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_edges_to(self, node_id: str) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM edges WHERE target_id = ?", (node_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_all_edges(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM edges")
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Outcome tracking (feedback loop)
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        session_id: str,
        file_path: str,
        outcome_type: str,
        decision_id: int | None = None,
        delta_summary: str | None = None,
    ):
        """Record an outcome (kept / modified / reverted) for a session.

        v2.0-rc.5 (Bug 15): outcomes.session_id has FK → sessions, and
        outcomes.decision_id has FK → decisions(id) ON DELETE SET NULL.
        If the parent session was deleted (e.g. by a cleanup task) we
        can't insert. Drop the outcome silently — the next session's
        outcomes are still recorded; we don't want one cleanup race
        to break the policy engine's read path.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO outcomes (session_id, file_path, decision_id, outcome_type, delta_summary)
                SELECT ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM sessions WHERE session_id = ?)
            """,
                (
                    session_id,
                    file_path,
                    decision_id,
                    outcome_type,
                    delta_summary,
                    session_id,
                ),
            )

    def get_outcomes_for_file(self, file_path: str, limit: int = 20) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT o.*, d.decision FROM outcomes o
            LEFT JOIN decisions d ON o.decision_id = d.id
            WHERE o.file_path = ?
            ORDER BY o.detected_at DESC LIMIT ?
        """,
            (file_path, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_decision_confidence(
        self, file_path: str | None = None, pattern: str | None = None
    ) -> dict:
        """Calculate confidence scores based on outcome history."""
        if file_path:
            cur = self.conn.execute(
                """
                SELECT outcome_type, COUNT(*) as cnt FROM outcomes
                WHERE file_path = ? GROUP BY outcome_type
            """,
                (file_path,),
            )
        elif pattern:
            cur = self.conn.execute(
                """
                SELECT outcome_type, COUNT(*) as cnt FROM outcomes
                WHERE file_path LIKE ? GROUP BY outcome_type
            """,
                (f"%{pattern}%",),
            )
        else:
            cur = self.conn.execute(
                "SELECT outcome_type, COUNT(*) as cnt FROM outcomes GROUP BY outcome_type"
            )

        counts = {row["outcome_type"]: row["cnt"] for row in cur.fetchall()}
        total = sum(counts.values())
        kept = counts.get("kept", 0)
        modified = counts.get("modified", 0)
        reverted = counts.get("reverted", 0)

        confidence = (kept + modified * 0.5) / total if total > 0 else 0.0
        return {
            "total_decisions": total,
            "kept": kept,
            "modified": modified,
            "reverted": reverted,
            "confidence": round(confidence, 3),
        }

    # v3.0.0 (2026-05-22 surface-cut audit): preferences + learned_rules
    # methods deleted. The MCP tools that exposed them (get_preferences,
    # get_learned_rules, retire_rule) and the engine policies that
    # consumed them (LiveStyleEnforcement, AIPromotionScore) were all
    # deleted in the audit — see CHANGELOG ``[Unreleased]`` Removed
    # section for the full list. The `preferences` and `learned_rules`
    # SQLite tables remain in the schema for back-compat (an old graph.db
    # opened by v3.0.0 should still load cleanly), but they're never
    # written or read by any v3.0.0 code path.

    # ------------------------------------------------------------------
    # v3.0.0 audit cleanup: get_project_maturity deleted along with the
    # MCP tool of the same name. It read learned_rules + preferences
    # counts, both of which are always zero in v3.0.0 (the surface for
    # those was deleted in the 2026-05-22 audit).

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def get_recent_sessions(self, limit: int = 5) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?
        """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_recent_decisions(self, limit: int = 10) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT d.*, s.summary, s.phase FROM decisions d
            JOIN sessions s ON d.session_id = s.session_id
            ORDER BY d.created_at DESC LIMIT ?
        """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def record_decision(
        self,
        *,
        decision: str,
        file_path: str | None = None,
        context: str | None = None,
        do_not_revert: bool = False,
        session_id: str | None = None,
        summary: str | None = None,
        phase: str | None = None,
    ) -> dict:
        """Insert a single decision with optional do_not_revert flag.

        Bug 2 (v2.0-rc.3): unblocks the canonical "log a decision and
        protect it from being reverted" flow. Without this method the
        AI had to use ``write_session_log`` (heavyweight, no flag) or
        ``update_node`` (per-FILE, not per-decision).

        If ``session_id`` is omitted, an auto-generated id is used and
        a session row is created on the fly so the FK constraint
        passes. Returns ``{decision_id, session_id}``.
        """
        import uuid

        sid = session_id or f"rec_{uuid.uuid4().hex[:8]}"
        # 2026-05-18 v2.1.2 Item 7: derive a useful summary from the
        # decision text instead of the unhelpful "ad-hoc record_decision"
        # placeholder. Field-test Report 3 §"Decisions recorded ad-hoc
        # show useless summary" flagged this — `summary: "ad-hoc
        # record_decision"` shows up in subsequent search results, telling
        # users the tool name not the actual content.
        if summary:
            effective_summary = summary
        elif decision:
            # First 80 chars of decision text, word-boundary trimmed.
            trimmed = decision.strip().split("\n", 1)[0]
            if len(trimmed) > 80:
                # Cut at last space ≤ 78 to avoid mid-word
                cut = trimmed[:78]
                last_space = cut.rfind(" ")
                if last_space >= 50:
                    effective_summary = trimmed[:last_space] + "…"
                else:
                    effective_summary = cut + "…"
            else:
                effective_summary = trimmed
        else:
            effective_summary = "ad-hoc record_decision"  # safety fallback
        with self.transaction() as conn:
            # Ensure parent session exists (FK constraint).
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, summary, phase) "
                "VALUES (?, ?, ?)",
                (sid, effective_summary, phase),
            )
            cur = conn.execute(
                "INSERT INTO decisions "
                "(session_id, file_path, decision, context, do_not_revert) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, file_path, decision, context, 1 if do_not_revert else 0),
            )
            decision_id = cur.lastrowid
        return {"decision_id": decision_id, "session_id": sid}

    def set_decision_protection(self, decision_id: int, do_not_revert: bool) -> bool:
        """Flip the do_not_revert flag on an existing decision.

        Returns True if a row was updated, False if decision_id not found.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE decisions SET do_not_revert = ? WHERE id = ?",
                (1 if do_not_revert else 0, decision_id),
            )
            return cur.rowcount > 0

    def get_file_hash(self, file_path: str) -> str | None:
        cur = self.conn.execute(
            "SELECT sha256 FROM file_hashes WHERE file_path = ?", (file_path,)
        )
        row = cur.fetchone()
        return row["sha256"] if row else None

    def update_file_hash(self, file_path: str, sha256: str):
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO file_hashes (file_path, sha256, last_indexed_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
                (file_path, sha256),
            )

    def log_session(
        self, session_id: str, summary: str, phase: str, decisions: list[dict]
    ) -> str:
        """Insert a session row plus its decisions, skipping duplicates.

        v1.8: A decision is skipped when it has a ``file_path`` and overlaps
        ≥ 80% (token-set) with any of the 5 most recent decisions for that
        same file. The session row is always written — only redundant
        *decisions* are dropped. Decisions without a ``file_path`` are always
        inserted (no scope to compare against).

        2026-05-18 v2.1.2 Item 22: returns the (possibly auto-suffixed)
        session_id actually written. If the requested session_id already
        exists with a DIFFERENT summary, we auto-suffix with a short hash
        of the new summary so the two sessions don't silently merge. If
        the existing row has the same summary, we keep the requested id
        (idempotent — for replay / retry cases).
        """
        import hashlib as _hashlib

        with self.transaction() as conn:
            # Check for collision FIRST (before any insert that might mutate).
            existing = conn.execute(
                "SELECT summary FROM sessions WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            actual_session_id = session_id
            if existing is not None:
                existing_summary = (existing["summary"] or "").strip()
                new_summary = (summary or "").strip()
                if existing_summary and new_summary and existing_summary != new_summary:
                    # Different content → auto-suffix with short hash.
                    digest = _hashlib.sha1(
                        f"{new_summary}|{phase or ''}".encode("utf-8")
                    ).hexdigest()[:8]
                    actual_session_id = f"{session_id}-{digest}"
                    # Defensive: still possible (very rare) for the suffixed
                    # id to also collide. Keep extending if so.
                    n = 1
                    while (
                        conn.execute(
                            "SELECT 1 FROM sessions WHERE session_id = ? LIMIT 1",
                            (actual_session_id,),
                        ).fetchone()
                        is not None
                    ):
                        actual_session_id = f"{session_id}-{digest}-{n}"
                        n += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (session_id, summary, phase)
                VALUES (?, ?, ?)
            """,
                (actual_session_id, summary, phase),
            )

            for d in decisions:
                fp = d.get("file_path")
                dtext = d.get("decision")
                if fp and dtext:
                    existing_decisions = [
                        row["decision"]
                        for row in conn.execute(
                            "SELECT decision FROM decisions WHERE file_path = ? "
                            "ORDER BY created_at DESC LIMIT 5",
                            (fp,),
                        ).fetchall()
                    ]
                    if _is_duplicate(dtext, existing_decisions):
                        continue
                conn.execute(
                    """
                    INSERT INTO decisions (session_id, file_path, decision, context)
                    VALUES (?, ?, ?, ?)
                """,
                    (actual_session_id, fp, dtext, d.get("context")),
                )
        return actual_session_id

    def search_decisions(
        self,
        query: str,
        limit: int = 10,
        session_id: str | None = None,
        *,
        since: str | None = None,
    ) -> list[dict]:
        """Search decisions with relevance-tiered ranking.

        v1.8: Results are ordered by match location (file_path > decision text >
        context > summary-only) then by recency within each tier.

        2026-05-18 v2.1.2 Item 25: optional ``since`` filter (ISO 8601
        timestamp or YYYY-MM-DD). Only decisions ``created_at > since``
        are returned.
        """
        pat = f"%{query}%"
        sql = """
            SELECT d.id, d.decision, d.context, d.file_path,
                   d.do_not_revert, s.summary, s.phase, d.created_at
            FROM decisions d
            JOIN sessions s ON d.session_id = s.session_id
            WHERE (d.file_path LIKE ? OR d.decision LIKE ? OR d.context LIKE ? OR s.summary LIKE ?)
        """
        params: list = [pat, pat, pat, pat]

        if session_id:
            sql += " AND d.session_id = ?"
            params.append(session_id)

        if since:
            sql += " AND d.created_at > ?"
            params.append(since)

        sql += """
            ORDER BY
              CASE
                WHEN d.file_path LIKE ? THEN 0
                WHEN d.decision  LIKE ? THEN 1
                WHEN d.context   LIKE ? THEN 2
                ELSE 3
              END,
              d.created_at DESC
            LIMIT ?
        """
        params.extend([pat, pat, pat, limit])

        cur = self.conn.execute(sql, params)
        # 2026-05-18 v2.1.2 Item 5: coerce `do_not_revert` INTEGER (0/1
        # in SQLite) → bool (True/False) before exposing to MCP callers.
        # Field-test Report 3 flagged the inconsistency: schema says
        # boolean but API returns `1`. Coerce at the read boundary so all
        # downstream paths (search.py response, engine signals, replay)
        # see the right type.
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if "do_not_revert" in d:
                d["do_not_revert"] = bool(d["do_not_revert"])
            rows.append(d)
        return rows

    # ------------------------------------------------------------------
    # v1.5: Function-level symbols and call graph
    # ------------------------------------------------------------------

    def add_symbol(
        self,
        symbol_id: str,
        file_node_id: str,
        name: str,
        kind: str,
        signature: str | None = None,
        parameters: str | None = None,
        return_type: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        docstring: str | None = None,
        is_public: bool = True,
        calls: str | None = None,
    ):
        """Insert or replace a function/class symbol.

        v2.0-rc.5 (Bug 14): symbols.file_node_id has FK → nodes(id). If
        the file node was deleted by a concurrent watcher reindex
        between the parse pass and this insert, FK fires. Same fix as
        Bug 9 / Bug 13: drop the row silently if the parent node is
        gone — the symbol re-adds on the next reindex.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO symbols
                    (id, file_node_id, name, kind, signature, parameters, return_type,
                     start_line, end_line, docstring, is_public, calls)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM nodes WHERE id = ?)
            """,
                (
                    symbol_id,
                    file_node_id,
                    name,
                    kind,
                    signature,
                    parameters,
                    return_type,
                    start_line,
                    end_line,
                    docstring,
                    is_public,
                    calls,
                    file_node_id,
                ),
            )

    def remove_symbols_for_file(self, file_node_id: str):
        """Remove all symbols (and their call_edges) for a file node."""
        with self.transaction() as conn:
            # call_edges cascade from symbols via FK
            conn.execute("DELETE FROM symbols WHERE file_node_id = ?", (file_node_id,))

    def add_call_edge(self, caller_id: str, callee_id: str, line: int | None = None):
        """Record a function call relationship.

        v2.0-rc.4 (Bug 9): the previous implementation used INSERT OR REPLACE
        unconditionally and raised ``IntegrityError: FOREIGN KEY constraint
        failed`` when a referenced symbol was deleted by a concurrent watcher
        reindex (the ``all_symbols`` lookup in graph_generator can go stale
        between read and write). 67 such crashes were recorded in the
        wild before this fix.

        New behaviour: the INSERT now uses an EXISTS subquery so rows that
        reference a missing symbol are silently dropped. The call edge is
        not critical (it can be rebuilt on the next full reindex) — losing
        the row beats crashing the watcher.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO call_edges (caller_id, callee_id, line)
                SELECT ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM symbols WHERE id = ?)
                  AND EXISTS (SELECT 1 FROM symbols WHERE id = ?)
            """,
                (caller_id, callee_id, line, caller_id, callee_id),
            )

    def get_callers(self, symbol_id: str) -> list[dict]:
        """Get all functions that call this symbol."""
        cur = self.conn.execute(
            """
            SELECT s.id, s.name, s.kind, s.file_node_id, ce.line
            FROM call_edges ce
            JOIN symbols s ON ce.caller_id = s.id
            WHERE ce.callee_id = ?
            ORDER BY s.name
        """,
            (symbol_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_callees(self, symbol_id: str) -> list[dict]:
        """Get all functions called by this symbol."""
        cur = self.conn.execute(
            """
            SELECT s.id, s.name, s.kind, s.file_node_id, ce.line
            FROM call_edges ce
            JOIN symbols s ON ce.callee_id = s.id
            WHERE ce.caller_id = ?
            ORDER BY s.name
        """,
            (symbol_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_symbols_for_file(self, file_node_id: str) -> list[dict]:
        """Get all symbols in a file."""
        cur = self.conn.execute(
            """
            SELECT * FROM symbols WHERE file_node_id = ? ORDER BY start_line
        """,
            (file_node_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def find_symbol(self, name: str, file_path: str | None = None) -> dict | None:
        """Find a symbol by name, optionally scoped to a file."""
        if file_path:
            node_id = f"file:{file_path}"
            cur = self.conn.execute(
                "SELECT * FROM symbols WHERE name = ? AND file_node_id = ?",
                (name, node_id),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM symbols WHERE name = ? LIMIT 1",
                (name,),
            )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_symbol_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def get_call_edge_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM call_edges").fetchone()[0]

    def find_hotspot_functions(self, min_lines: int = 50) -> list[dict]:
        """Find large functions exceeding line threshold."""
        cur = self.conn.execute(
            """
            SELECT s.*, (s.end_line - s.start_line) as line_count,
                   n.file_path as full_path
            FROM symbols s
            JOIN nodes n ON s.file_node_id = n.id
            WHERE (s.end_line - s.start_line) >= ?
            ORDER BY (s.end_line - s.start_line) DESC
        """,
            (min_lines,),
        )
        return [dict(r) for r in cur.fetchall()]

    def find_high_fan_in(self, min_callers: int = 5) -> list[dict]:
        """Find symbols with many callers (high fan-in = high risk)."""
        cur = self.conn.execute(
            """
            SELECT s.id, s.name, s.kind, s.file_node_id, COUNT(ce.caller_id) as caller_count
            FROM symbols s
            JOIN call_edges ce ON ce.callee_id = s.id
            GROUP BY s.id
            HAVING caller_count >= ?
            ORDER BY caller_count DESC
        """,
            (min_callers,),
        )
        return [dict(r) for r in cur.fetchall()]

    def close(self):
        self.conn.close()
