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
            ''')
            conn.execute("PRAGMA foreign_keys = ON;")

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

    def close(self):
        self.conn.close()

