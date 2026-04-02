import sqlite3
import os
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class SQLiteGraph:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    @contextmanager
    def transaction(self):
        with self.conn:
            yield self.conn

    def _init_db(self):
        with self.transaction() as conn:
            conn.executescript('''
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
            ''')
            conn.execute("PRAGMA foreign_keys = ON;")

            # v1.5 migrations: add source column for global sync tracking
            try:
                conn.execute("ALTER TABLE preferences ADD COLUMN source TEXT DEFAULT 'local'")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute("ALTER TABLE learned_rules ADD COLUMN source TEXT DEFAULT 'local'")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def add_node(self, node_id: str, kind: str, name: str, file_path: str, **kwargs):
        existing = self.get_node(node_id)
        fields = ["id", "kind", "name", "file_path"]
        values = [node_id, kind, name, file_path]
        
        metadata_fields = ["line_start", "line_end", "docstring", "is_public", 
                           "role", "type", "rules", "key_functions", "dependencies", 
                           "stability", "do_not_revert", "layer"]
                           
        for k in metadata_fields:
            if k in kwargs:
                fields.append(k)
                values.append(kwargs[k])
            elif existing and existing.get(k) is not None:
                fields.append(k)
                values.append(existing[k])
                
        placeholders = ",".join(["?"] * len(fields))
        query = f"INSERT OR REPLACE INTO nodes ({','.join(fields)}) VALUES ({placeholders})"
        
        with self.transaction() as conn:
            conn.execute(query, values)

    def update_node_metadata(self, node_id: str, **kwargs):
        valid_fields = ["role", "type", "rules", "key_functions", "dependencies", 
                        "stability", "do_not_revert", "layer"]
        updates, values = [], []
        for k, v in kwargs.items():
            if k in valid_fields:
                updates.append(f"{k} = ?")
                values.append(v)
        if not updates: return
            
        values.append(node_id)
        query = f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?"
        with self.transaction() as conn:
            conn.execute(query, values)

    def get_node(self, node_id: str) -> dict | None:
        cur = self.conn.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
        row = cur.fetchone()
        return dict(row) if row else None
        
    def get_node_by_path(self, file_path: str) -> dict | None:
        cur = self.conn.execute('SELECT * FROM nodes WHERE file_path = ? AND kind = "file" LIMIT 1', (file_path,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_file_nodes(self, layer: str | None = None, stability: str | None = None, do_not_revert: bool | None = None) -> list[dict]:
        query = 'SELECT * FROM nodes WHERE kind = "file"'
        params = []
        if layer:
            query += ' AND layer = ?'
            params.append(layer)
        if stability:
            query += ' AND stability = ?'
            params.append(stability)
        if do_not_revert is not None:
            query += ' AND do_not_revert = ?'
            params.append(1 if do_not_revert else 0)
            
        cur = self.conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    def get_blast_radius(self, node_id: str, max_depth: int = 3) -> list[dict]:
        query = '''
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
        '''
        with self.transaction() as conn:
            cur = conn.execute(query, (node_id, max_depth, node_id))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Edge management
    # ------------------------------------------------------------------

    def add_edge(self, source_id: str, target_id: str, kind: str = "imports", line: int | None = None):
        with self.transaction() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO edges (source_id, target_id, kind, line) VALUES (?, ?, ?, ?)',
                (source_id, target_id, kind, line),
            )

    def remove_edges_for_node(self, node_id: str):
        with self.transaction() as conn:
            conn.execute('DELETE FROM edges WHERE source_id = ?', (node_id,))

    def get_edges_from(self, node_id: str) -> list[dict]:
        cur = self.conn.execute('SELECT * FROM edges WHERE source_id = ?', (node_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_edges_to(self, node_id: str) -> list[dict]:
        cur = self.conn.execute('SELECT * FROM edges WHERE target_id = ?', (node_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_all_edges(self) -> list[dict]:
        cur = self.conn.execute('SELECT * FROM edges')
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Outcome tracking (feedback loop)
    # ------------------------------------------------------------------

    def record_outcome(self, session_id: str, file_path: str, outcome_type: str,
                       decision_id: int | None = None, delta_summary: str | None = None):
        with self.transaction() as conn:
            conn.execute('''
                INSERT INTO outcomes (session_id, file_path, decision_id, outcome_type, delta_summary)
                VALUES (?, ?, ?, ?, ?)
            ''', (session_id, file_path, decision_id, outcome_type, delta_summary))

    def get_outcomes_for_file(self, file_path: str, limit: int = 20) -> list[dict]:
        cur = self.conn.execute('''
            SELECT o.*, d.decision FROM outcomes o
            LEFT JOIN decisions d ON o.decision_id = d.id
            WHERE o.file_path = ?
            ORDER BY o.detected_at DESC LIMIT ?
        ''', (file_path, limit))
        return [dict(r) for r in cur.fetchall()]

    def get_decision_confidence(self, file_path: str | None = None, pattern: str | None = None) -> dict:
        """Calculate confidence scores based on outcome history."""
        if file_path:
            cur = self.conn.execute('''
                SELECT outcome_type, COUNT(*) as cnt FROM outcomes
                WHERE file_path = ? GROUP BY outcome_type
            ''', (file_path,))
        elif pattern:
            cur = self.conn.execute('''
                SELECT outcome_type, COUNT(*) as cnt FROM outcomes
                WHERE file_path LIKE ? GROUP BY outcome_type
            ''', (f'%{pattern}%',))
        else:
            cur = self.conn.execute(
                'SELECT outcome_type, COUNT(*) as cnt FROM outcomes GROUP BY outcome_type'
            )

        counts = {row['outcome_type']: row['cnt'] for row in cur.fetchall()}
        total = sum(counts.values())
        kept = counts.get('kept', 0)
        modified = counts.get('modified', 0)
        reverted = counts.get('reverted', 0)

        confidence = (kept + modified * 0.5) / total if total > 0 else 0.0
        return {
            "total_decisions": total,
            "kept": kept,
            "modified": modified,
            "reverted": reverted,
            "confidence": round(confidence, 3),
        }

    # ------------------------------------------------------------------
    # Developer preferences
    # ------------------------------------------------------------------

    def record_preference(self, category: str, signal: str, example: str | None = None,
                          source: str = "local"):
        existing = self.conn.execute(
            'SELECT id, frequency FROM preferences WHERE category = ? AND signal = ?',
            (category, signal),
        ).fetchone()

        if existing:
            with self.transaction() as conn:
                conn.execute('''
                    UPDATE preferences SET frequency = frequency + 1, last_seen = CURRENT_TIMESTAMP, example = COALESCE(?, example)
                    WHERE id = ?
                ''', (example, existing['id']))
        else:
            with self.transaction() as conn:
                conn.execute('''
                    INSERT INTO preferences (category, signal, example, source) VALUES (?, ?, ?, ?)
                ''', (category, signal, example, source))

    def get_preferences(self, category: str | None = None, min_frequency: int = 1) -> list[dict]:
        if category:
            cur = self.conn.execute('''
                SELECT * FROM preferences WHERE category = ? AND frequency >= ?
                ORDER BY frequency DESC
            ''', (category, min_frequency))
        else:
            cur = self.conn.execute('''
                SELECT * FROM preferences WHERE frequency >= ? ORDER BY frequency DESC
            ''', (min_frequency,))
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Learned rules
    # ------------------------------------------------------------------

    def add_learned_rule(self, rule_text: str, confidence: float, source_sessions: list[str],
                         category: str | None = None, file_pattern: str | None = None):
        import json
        with self.transaction() as conn:
            conn.execute('''
                INSERT INTO learned_rules (rule_text, confidence, source_sessions, category, file_pattern)
                VALUES (?, ?, ?, ?, ?)
            ''', (rule_text, confidence, json.dumps(source_sessions), category, file_pattern))

    def update_learned_rule(self, rule_id: int, confidence: float | None = None,
                            source_sessions: list[str] | None = None):
        import json
        updates, values = [], []
        if confidence is not None:
            updates.append("confidence = ?")
            values.append(confidence)
        if source_sessions is not None:
            updates.append("source_sessions = ?")
            values.append(json.dumps(source_sessions))
        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            values.append(rule_id)
            with self.transaction() as conn:
                conn.execute(f'UPDATE learned_rules SET {", ".join(updates)} WHERE id = ?', values)

    def get_learned_rules(self, category: str | None = None, file_pattern: str | None = None,
                          min_confidence: float = 0.0) -> list[dict]:
        query = 'SELECT * FROM learned_rules WHERE confidence >= ?'
        params: list = [min_confidence]
        if category:
            query += ' AND category = ?'
            params.append(category)
        if file_pattern:
            query += ' AND (file_pattern IS NULL OR ? LIKE file_pattern)'
            params.append(file_pattern)
        query += ' ORDER BY confidence DESC'
        cur = self.conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Project maturity metrics
    # ------------------------------------------------------------------

    def get_project_maturity(self) -> dict:
        """Compute overall project maturity based on outcomes and coverage."""
        # Overall confidence
        confidence = self.get_decision_confidence()

        # File coverage: files with at least one session decision
        total_files = self.conn.execute('SELECT COUNT(*) as c FROM nodes WHERE kind = "file"').fetchone()['c']
        covered_files = self.conn.execute(
            'SELECT COUNT(DISTINCT file_path) as c FROM decisions WHERE file_path IS NOT NULL'
        ).fetchone()['c']

        # Learned rules count
        rule_count = self.conn.execute('SELECT COUNT(*) as c FROM learned_rules WHERE confidence >= 0.5').fetchone()['c']

        # Preference signals count
        pref_count = self.conn.execute('SELECT COUNT(*) as c FROM preferences WHERE frequency >= 2').fetchone()['c']

        # Session count
        session_count = self.conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c']

        coverage = round(covered_files / total_files, 3) if total_files > 0 else 0.0

        return {
            "session_count": session_count,
            "total_files": total_files,
            "covered_files": covered_files,
            "coverage": coverage,
            "overall_confidence": confidence["confidence"],
            "outcome_breakdown": confidence,
            "learned_rules": rule_count,
            "preference_signals": pref_count,
        }

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def get_recent_sessions(self, limit: int = 5) -> list[dict]:
        cur = self.conn.execute('''
            SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?
        ''', (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_recent_decisions(self, limit: int = 10) -> list[dict]:
        cur = self.conn.execute('''
            SELECT d.*, s.summary, s.phase FROM decisions d
            JOIN sessions s ON d.session_id = s.session_id
            ORDER BY d.created_at DESC LIMIT ?
        ''', (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_file_hash(self, file_path: str) -> str | None:
        cur = self.conn.execute('SELECT sha256 FROM file_hashes WHERE file_path = ?', (file_path,))
        row = cur.fetchone()
        return row['sha256'] if row else None

    def update_file_hash(self, file_path: str, sha256: str):
        with self.transaction() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO file_hashes (file_path, sha256, last_indexed_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (file_path, sha256))

    def log_session(self, session_id: str, summary: str, phase: str, decisions: list[dict]):
        with self.transaction() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO sessions (session_id, summary, phase)
                VALUES (?, ?, ?)
            ''', (session_id, summary, phase))
            
            for d in decisions:
                conn.execute('''
                    INSERT INTO decisions (session_id, file_path, decision, context)
                    VALUES (?, ?, ?, ?)
                ''', (session_id, d.get("file_path"), d.get("decision"), d.get("context")))

    def search_decisions(self, query: str, limit: int = 10, session_id: str | None = None) -> list[dict]:
        sql = '''
            SELECT d.decision, d.context, d.file_path, s.summary, s.phase, d.created_at
            FROM decisions d
            JOIN sessions s ON d.session_id = s.session_id
            WHERE (d.decision LIKE ? OR d.context LIKE ? OR s.summary LIKE ?)
        '''
        params = [f'%{query}%', f'%{query}%', f'%{query}%']
        
        if session_id:
            sql += ' AND d.session_id = ?'
            params.append(session_id)
            
        sql += ' ORDER BY d.created_at DESC LIMIT ?'
        params.append(limit)
        
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # v1.5: Function-level symbols and call graph
    # ------------------------------------------------------------------

    def add_symbol(self, symbol_id: str, file_node_id: str, name: str, kind: str,
                   signature: str | None = None, parameters: str | None = None,
                   return_type: str | None = None, start_line: int | None = None,
                   end_line: int | None = None, docstring: str | None = None,
                   is_public: bool = True, calls: str | None = None):
        """Insert or replace a function/class symbol."""
        with self.transaction() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO symbols
                    (id, file_node_id, name, kind, signature, parameters, return_type,
                     start_line, end_line, docstring, is_public, calls)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol_id, file_node_id, name, kind, signature, parameters,
                  return_type, start_line, end_line, docstring, is_public, calls))

    def remove_symbols_for_file(self, file_node_id: str):
        """Remove all symbols (and their call_edges) for a file node."""
        with self.transaction() as conn:
            # call_edges cascade from symbols via FK
            conn.execute("DELETE FROM symbols WHERE file_node_id = ?", (file_node_id,))

    def add_call_edge(self, caller_id: str, callee_id: str, line: int | None = None):
        """Record a function call relationship."""
        with self.transaction() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO call_edges (caller_id, callee_id, line)
                VALUES (?, ?, ?)
            ''', (caller_id, callee_id, line))

    def get_callers(self, symbol_id: str) -> list[dict]:
        """Get all functions that call this symbol."""
        cur = self.conn.execute('''
            SELECT s.id, s.name, s.kind, s.file_node_id, ce.line
            FROM call_edges ce
            JOIN symbols s ON ce.caller_id = s.id
            WHERE ce.callee_id = ?
            ORDER BY s.name
        ''', (symbol_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_callees(self, symbol_id: str) -> list[dict]:
        """Get all functions called by this symbol."""
        cur = self.conn.execute('''
            SELECT s.id, s.name, s.kind, s.file_node_id, ce.line
            FROM call_edges ce
            JOIN symbols s ON ce.callee_id = s.id
            WHERE ce.caller_id = ?
            ORDER BY s.name
        ''', (symbol_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_symbols_for_file(self, file_node_id: str) -> list[dict]:
        """Get all symbols in a file."""
        cur = self.conn.execute('''
            SELECT * FROM symbols WHERE file_node_id = ? ORDER BY start_line
        ''', (file_node_id,))
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
                "SELECT * FROM symbols WHERE name = ? LIMIT 1", (name,),
            )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_symbol_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def get_call_edge_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM call_edges").fetchone()[0]

    def find_hotspot_functions(self, min_lines: int = 50) -> list[dict]:
        """Find large functions exceeding line threshold."""
        cur = self.conn.execute('''
            SELECT s.*, (s.end_line - s.start_line) as line_count,
                   n.file_path as full_path
            FROM symbols s
            JOIN nodes n ON s.file_node_id = n.id
            WHERE (s.end_line - s.start_line) >= ?
            ORDER BY (s.end_line - s.start_line) DESC
        ''', (min_lines,))
        return [dict(r) for r in cur.fetchall()]

    def find_high_fan_in(self, min_callers: int = 5) -> list[dict]:
        """Find symbols with many callers (high fan-in = high risk)."""
        cur = self.conn.execute('''
            SELECT s.id, s.name, s.kind, s.file_node_id, COUNT(ce.caller_id) as caller_count
            FROM symbols s
            JOIN call_edges ce ON ce.callee_id = s.id
            GROUP BY s.id
            HAVING caller_count >= ?
            ORDER BY caller_count DESC
        ''', (min_callers,))
        return [dict(r) for r in cur.fetchall()]

    def close(self):
        self.conn.close()

