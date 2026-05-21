"""
fts5_index.py — SQLite FTS5 keyword search over decisions.jsonl.

v2.2.0 replaces ChromaDB semantic search with SQLite FTS5 (full-text
search, BM25-ranked). For our use case — searching hundreds to a few
thousand short decision texts — FTS5 delivers sub-50ms queries with:

- Zero native dependencies beyond stdlib sqlite3
- ~10 KB index per 1000 decisions (vs ~10 MB for chromadb embeddings)
- Deterministic results (no nondeterminism from model loading order)
- Built-in stemming via the porter tokenizer
- BM25 ranking (tunable via the rank function)

The index lives in ``<repo>/.codevira-cache/fts5.sqlite`` (gitignored).
It's rebuildable from decisions.jsonl; if missing/corrupted, callers
should regenerate via ``rebuild_from_jsonl()``.

Lifecycle:
- ``rebuild_from_jsonl()``: full rebuild (drop + recreate the FTS5
  table; ingest every decision)
- ``add_decision()``: incremental insert called after each
  ``record_decision`` (kept in sync with decisions.jsonl)
- ``search(query, limit)``: returns ranked decision IDs + BM25 scores
- ``staleness_check()``: True if decisions.jsonl mtime > index mtime
  (in which case caller should rebuild)

We index three fields with different weights:
- ``decision`` (highest weight: 3.0) — the actual text
- ``context`` (medium: 1.5) — surrounding rationale
- ``summary`` (low: 1.0) — derived first-line; redundant with decision
  but useful for queries that hit only the summary

We deliberately do NOT index ``tags`` here — tag lookup uses the
manifest.yaml O(1) path which is faster + exact.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from mcp_server.storage import jsonl_store

logger = logging.getLogger(__name__)

_TABLE = "decision_fts"
_META_TABLE = "fts_meta"

# Schema is intentionally minimal — FTS5 is fast even without elaborate
# tokenization tweaks. Porter stemmer covers "auth" → "authentication"
# style matches; remove_diacritics handles café/cafe; ascii fallback
# for safe defaults.
_CREATE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE}
USING fts5(
    decision_id UNINDEXED,
    decision,
    context,
    summary,
    tags UNINDEXED,
    tokenize = "porter unicode61 remove_diacritics 2"
);
"""

_CREATE_META_SQL = f"""
CREATE TABLE IF NOT EXISTS {_META_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    """Open or create the FTS5 SQLite database with sensible defaults."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # WAL mode reduces contention when the watcher writes concurrently
    # with an MCP-tool read.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SQL)
    conn.execute(_CREATE_META_SQL)
    conn.commit()


def rebuild_from_jsonl(decisions_path: Path, index_path: Path) -> int:
    """Drop + recreate the FTS5 index from decisions.jsonl.

    Returns the number of indexed records. Safe to call any time;
    atomic-enough (we DELETE FROM the FTS5 table inside one transaction,
    then INSERT all rows, then COMMIT).
    """
    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        records = jsonl_store.read_all(decisions_path)

        with conn:  # single transaction (auto-commit on exit)
            conn.execute(f"DELETE FROM {_TABLE}")
            for r in records:
                if r.get("is_superseded") or r.get("superseded_by"):
                    # Skip superseded so search results match list_decisions
                    # default (hide superseded).
                    continue
                conn.execute(
                    f"INSERT INTO {_TABLE} "
                    "(decision_id, decision, context, summary, tags) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(r.get("id", "")),
                        r.get("decision") or "",
                        r.get("context") or "",
                        _summary_or_first_chars(r),
                        " ".join(r.get("tags") or []),
                    ),
                )
            # Record mtime of source file so staleness check works.
            try:
                src_mtime = decisions_path.stat().st_mtime
            except OSError:
                src_mtime = 0
            conn.execute(
                f"INSERT OR REPLACE INTO {_META_TABLE}(key, value) VALUES(?, ?)",
                ("source_mtime", str(src_mtime)),
            )
            count = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        return int(count)
    finally:
        conn.close()


def add_decision(index_path: Path, decision: dict[str, Any]) -> None:
    """Incrementally index one decision. Called after record_decision.

    Idempotent: if the decision_id already exists in the FTS5 table
    (re-indexing after a recover), it's replaced.
    """
    if decision.get("is_superseded") or decision.get("superseded_by"):
        return  # don't index superseded decisions

    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        did = str(decision.get("id", ""))
        if not did:
            return
        with conn:
            conn.execute(f"DELETE FROM {_TABLE} WHERE decision_id = ?", (did,))
            conn.execute(
                f"INSERT INTO {_TABLE} "
                "(decision_id, decision, context, summary, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    did,
                    decision.get("decision") or "",
                    decision.get("context") or "",
                    _summary_or_first_chars(decision),
                    " ".join(decision.get("tags") or []),
                ),
            )
    finally:
        conn.close()


def search(
    index_path: Path,
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Run BM25-ranked FTS5 search; return ranked records.

    Returns: ``[{"decision_id": str, "score": float, "snippet": str}, ...]``

    ``score`` is the BM25 distance (lower = better match). We sort
    ascending so callers see best matches first.

    Returns empty list if the index doesn't exist (caller's responsibility
    to ``rebuild_from_jsonl()`` first).

    Bad-query handling: FTS5 raises on certain malformed queries (e.g.
    unbalanced quotes). We catch and return [] rather than propagating.
    """
    if not index_path.is_file():
        return []
    if not query or not query.strip():
        return []

    # Sanitize query for FTS5 — strip characters that FTS5 treats as
    # operators if the user didn't write them deliberately.
    sanitized = _sanitize_fts_query(query)
    if not sanitized:
        return []

    conn = _connect(index_path)
    try:
        try:
            cursor = conn.execute(
                f"""
                SELECT decision_id,
                       bm25({_TABLE}, 3.0, 1.5, 1.0, 0.0) AS score,
                       snippet({_TABLE}, 1, '[', ']', '…', 12) AS snippet
                FROM {_TABLE}
                WHERE {_TABLE} MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (sanitized, limit),
            )
            return [
                {
                    "decision_id": row["decision_id"],
                    "score": float(row["score"]),
                    "snippet": row["snippet"],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError as exc:
            logger.warning(
                "fts5_index.search: query failed (%r); returning empty: %s",
                query,
                exc,
            )
            return []
    finally:
        conn.close()


def staleness_check(decisions_path: Path, index_path: Path) -> bool:
    """Return True if the index is older than decisions.jsonl.

    Caller should ``rebuild_from_jsonl`` in that case.
    """
    if not index_path.is_file():
        return True
    if not decisions_path.is_file():
        return False  # nothing to index against; not stale
    src_mtime = decisions_path.stat().st_mtime

    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            f"SELECT value FROM {_META_TABLE} WHERE key = ?",
            ("source_mtime",),
        ).fetchone()
        if row is None:
            return True
        try:
            idx_mtime = float(row["value"])
        except (TypeError, ValueError):
            return True
        # Use a 1-second epsilon to tolerate filesystems with second-precision mtime.
        return src_mtime > idx_mtime + 1.0
    finally:
        conn.close()


# ─── Internal helpers ─────────────────────────────────────────────────


def _summary_or_first_chars(record: dict[str, Any], cap: int = 80) -> str:
    """Fall back to first-N chars of decision text if no summary present."""
    summary = record.get("summary")
    if summary:
        return str(summary)
    text = str(record.get("decision") or "")
    if len(text) <= cap:
        return text
    cut = text[:cap]
    last_space = cut.rfind(" ")
    if last_space > cap // 2:
        return cut[:last_space] + "…"
    return cut + "…"


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 operator chars unless the user wrote them deliberately.

    FTS5 treats characters like ``"``, ``*``, ``(``, ``)``, ``-``, ``:``
    as operators. A user query like ``"x.y.z"`` (with no quoting intent)
    can blow up the parser. We quote the whole thing as a phrase if it
    contains only "safe" characters; otherwise we pass through.

    Recovery: if our sanitized query still blows up, ``search()``
    catches OperationalError and returns [].
    """
    stripped = query.strip()
    if not stripped:
        return ""
    # Tokenize on whitespace; reject empty tokens; quote each as a phrase
    # so dots/slashes/colons inside tokens don't confuse FTS5.
    tokens = []
    for raw in stripped.split():
        # Strip outer quotes the user may have typed; we'll re-quote.
        t = raw.strip('"').strip("'").strip()
        # Drop any FTS5 operator chars from the middle of tokens.
        for op_char in ('"', "(", ")", "*", ":", "^"):
            t = t.replace(op_char, " ")
        t = t.strip()
        if t:
            tokens.append(f'"{t}"')
    return " ".join(tokens)
