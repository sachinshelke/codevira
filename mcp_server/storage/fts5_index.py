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

# v3.1.0 M3 Phase 2: skills FTS5 table coexists in the same .sqlite
# file. Separate meta key (``skill_source_mtime``) so the staleness
# check tracks decisions and skills independently. Weights below
# (name 3.0 / summary 1.5 / procedure 1.0) match the plan's stated
# ranking; tags is UNINDEXED — agents can supply tags as a separate
# Jaccard filter at the skills_store layer rather than letting FTS5
# stem them.
_SKILL_TABLE = "skill_fts"

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
    file_path,
    tags UNINDEXED,
    tokenize = "porter unicode61 remove_diacritics 2"
);
"""

_CREATE_SKILL_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {_SKILL_TABLE}
USING fts5(
    skill_id UNINDEXED,
    name,
    summary,
    procedure,
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
    """Create FTS5 and meta tables if they don't exist.

    Schema evolution: if the FTS5 table exists but is missing the
    ``file_path`` column (pre-v2.2.0 schema), drop and recreate it so
    the caller gets a clean rebuild on the next ``rebuild_from_jsonl``
    call. We detect this by checking the FTS5 schema string stored in
    ``{_TABLE}_config``.
    """
    # Check if the FTS5 table already exists with the old schema (no file_path).
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if f"{_TABLE}" in tables:
            # Detect old schema: try to SELECT the file_path column.
            try:
                conn.execute(f"SELECT file_path FROM {_TABLE} LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                # Old schema — drop and let it be recreated below.
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_data")
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_idx")
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_content")
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_docsize")
                conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_config")
                conn.commit()
    except Exception:  # noqa: BLE001
        pass
    conn.execute(_CREATE_SQL)
    conn.execute(_CREATE_SKILL_SQL)  # v3.1.0 M3 Phase 2
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
                    "(decision_id, decision, context, summary, file_path, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(r.get("id", "")),
                        r.get("decision") or "",
                        r.get("context") or "",
                        _summary_or_first_chars(r),
                        r.get("file_path") or "",
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
                "(decision_id, decision, context, summary, file_path, tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    did,
                    decision.get("decision") or "",
                    decision.get("context") or "",
                    _summary_or_first_chars(decision),
                    decision.get("file_path") or "",
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
                       bm25({_TABLE}, 3.0, 1.5, 1.0, 0.8, 0.0) AS score,
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


# ─── Skills FTS5 (v3.1.0 M3 Phase 2) ─────────────────────────────────


def rebuild_skills_from_jsonl(skills_path: Path, index_path: Path) -> int:
    """Drop + recreate the skill_fts table from skills.jsonl.

    Returns the number of indexed skills. Skips superseded entries to
    match ``list_skills`` default behavior. Same atomicity contract as
    ``rebuild_from_jsonl`` — single transaction inside the connection.
    """
    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        records = jsonl_store.read_merged(skills_path)

        with conn:
            conn.execute(f"DELETE FROM {_SKILL_TABLE}")
            for r in records:
                if r.get("status") == "superseded":
                    continue
                conn.execute(
                    f"INSERT INTO {_SKILL_TABLE} "
                    "(skill_id, name, summary, procedure, tags) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(r.get("id", "")),
                        r.get("name") or "",
                        r.get("summary") or "",
                        r.get("procedure") or "",
                        " ".join((r.get("triggers") or {}).get("tags") or []),
                    ),
                )
            try:
                src_mtime = skills_path.stat().st_mtime
            except OSError:
                src_mtime = 0
            conn.execute(
                f"INSERT OR REPLACE INTO {_META_TABLE}(key, value) VALUES(?, ?)",
                ("skill_source_mtime", str(src_mtime)),
            )
            count = conn.execute(f"SELECT COUNT(*) FROM {_SKILL_TABLE}").fetchone()[0]
        return int(count)
    finally:
        conn.close()


def add_skill(index_path: Path, skill: dict[str, Any]) -> None:
    """Incrementally index one skill. Called after skills_store.record.

    Idempotent: an existing skill_id is DELETEd before INSERT, so a
    second add (e.g., after an amendment) cleanly replaces the row.
    Skips superseded skills so they don't pollute search results.
    """
    if skill.get("status") == "superseded":
        return

    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        kid = str(skill.get("id", ""))
        if not kid:
            return
        with conn:
            conn.execute(f"DELETE FROM {_SKILL_TABLE} WHERE skill_id = ?", (kid,))
            conn.execute(
                f"INSERT INTO {_SKILL_TABLE} "
                "(skill_id, name, summary, procedure, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    kid,
                    skill.get("name") or "",
                    skill.get("summary") or "",
                    skill.get("procedure") or "",
                    " ".join((skill.get("triggers") or {}).get("tags") or []),
                ),
            )
    finally:
        conn.close()


def search_skills(
    index_path: Path,
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """BM25-ranked FTS5 search over the skill library.

    Returns ``[{"skill_id": str, "score": float, "snippet": str}, ...]``
    sorted ascending by BM25 distance (best matches first). Weights
    per the plan: name 3.0, summary 1.5, procedure 1.0; tags is
    UNINDEXED so it doesn't contribute to BM25 ranking.

    Bad-query handling mirrors ``search()``: malformed inputs return
    [] rather than raising.
    """
    if not index_path.is_file():
        return []
    if not query or not query.strip():
        return []
    sanitized = _sanitize_fts_query(query)
    if not sanitized:
        return []

    conn = _connect(index_path)
    try:
        try:
            cursor = conn.execute(
                f"""
                SELECT skill_id,
                       bm25({_SKILL_TABLE}, 3.0, 1.5, 1.0, 0.0) AS score,
                       snippet({_SKILL_TABLE}, 2, '[', ']', '…', 12) AS snippet
                FROM {_SKILL_TABLE}
                WHERE {_SKILL_TABLE} MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (sanitized, limit),
            )
            return [
                {
                    "skill_id": row["skill_id"],
                    "score": float(row["score"]),
                    "snippet": row["snippet"],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError as exc:
            logger.warning(
                "fts5_index.search_skills: query failed (%r); returning empty: %s",
                query,
                exc,
            )
            return []
    finally:
        conn.close()


def skill_staleness_check(skills_path: Path, index_path: Path) -> bool:
    """Return True if the skills FTS5 index is older than skills.jsonl.

    Tracked under a separate meta key (``skill_source_mtime``) so it
    doesn't collide with the decisions staleness signal.
    """
    if not index_path.is_file():
        return True
    if not skills_path.is_file():
        return False  # nothing to index
    src_mtime = skills_path.stat().st_mtime

    conn = _connect(index_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            f"SELECT value FROM {_META_TABLE} WHERE key = ?",
            ("skill_source_mtime",),
        ).fetchone()
        if row is None:
            return True
        try:
            idx_mtime = float(row["value"])
        except (TypeError, ValueError):
            return True
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
    """Build a relevance-search FTS5 query from user input.

    Produces an ``OR``-joined query over significant content words.
    This means a document matching ANY of the query terms will be
    returned and ranked by BM25 (most-matching docs score highest).
    An AND query (the default when tokens are space-separated in FTS5)
    is too strict for relevance injection — e.g. a prompt asking about
    "bcrypt for password hashing" should still find the decision "use
    bcrypt over argon2" even though "password" and "hashing" aren't in
    the stored text.

    Stopwords are stripped so common English connectives don't dominate
    the match. Tokens under 3 chars are also skipped (too noisy).

    Recovery: if the sanitized query blows up at the FTS5 layer,
    ``search()`` catches OperationalError and returns [].
    """
    _STOPWORDS = frozenset(
        {
            "a",
            "an",
            "the",
            "is",
            "it",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "or",
            "and",
            "but",
            "if",
            "do",
            "did",
            "we",
            "i",
            "you",
            "he",
            "she",
            "they",
            "what",
            "how",
            "why",
            "when",
            "where",
            "this",
            "that",
            "with",
            "from",
            "by",
            "as",
            "be",
            "was",
            "are",
            "about",
            "have",
            "has",
            "had",
            "not",
            "can",
            "will",
            "would",
            "should",
            "could",
            "our",
            "their",
            "which",
            "who",
            "get",
            "use",
            "used",
            "any",
            "all",
            "my",
            "your",
            "its",
            "been",
            "into",
            "let",
            "also",
            "just",
            "so",
            "up",
            "out",
            "there",
            "then",
        }
    )
    stripped = query.strip()
    if not stripped:
        return ""
    tokens = []
    for raw in stripped.split():
        # Strip trailing punctuation and outer quotes.
        t = raw.strip("\"'.,;:!?()[]{}").strip()
        # Drop FTS5 operator chars from the inside of tokens.
        for op_char in ('"', "(", ")", "*", ":", "^"):
            t = t.replace(op_char, " ")
        t = t.strip()
        if not t:
            continue
        # Skip stopwords and very short tokens.
        if t.lower() in _STOPWORDS or len(t) < 3:
            continue
        # Quote to protect embedded dots / slashes / hyphens.
        tokens.append(f'"{t}"')
    if not tokens:
        return ""
    # OR-join so ANY matching token scores the document.
    return " OR ".join(tokens)
