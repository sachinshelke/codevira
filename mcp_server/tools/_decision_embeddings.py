"""
_decision_embeddings.py — Hybrid retrieval for ``search_decisions``.

2026-05-17 v2.1: closes the benchmark gap where ``search_decisions`` was
BM25 keyword-only and silently missed natural-language queries (e.g.
``"DDD architecture layer"`` returned 0 hits even when a decision was
literally about 4-layer DDD).

Design
------
- **On write** (``record_decision``): the SQLite row is the canonical store;
  we ADDITIONALLY embed the decision text into a ChromaDB collection
  ``codevira_decisions`` for semantic recall.
- **On read** (``search_decisions``): we run the existing BM25-LIKE SQL
  query AND a ChromaDB semantic query in parallel, merge with Reciprocal
  Rank Fusion (RRF), and return the unified ranked list.
- **Graceful degradation** (P9): every helper here is wrapped in try/except.
  If chromadb is missing, corrupted, or temporarily unavailable, we log a
  warning and fall back to BM25-only — never block the user's write or read.
- **Single source of truth** (P6): SQLite remains canonical. The ChromaDB
  copy is a lossy projection used only for ranking. If the two ever
  disagree, SQLite wins — we re-fetch the decision row by ID before
  returning it.

ChromaDB collection schema
--------------------------
- ``collection_name``: ``codevira_decisions``
- ``id``: stringified SQLite decision row ID (so we can fetch back)
- ``document``: the decision text + optional context (what gets embedded)
- ``metadata``: ``{"decision_id": int, "session_id": str, "file_path": str|None}``

Reciprocal Rank Fusion (RRF)
----------------------------
For each candidate, the score is::

    score = sum(1 / (k + rank_i))  for i in (bm25, semantic)

where ``k=60`` is the standard constant. Result: a candidate that ranks
top-1 in either retriever gets a high score; candidates appearing in BOTH
lists climb to the top. No ML required, no extra dependency.

Failure modes considered
------------------------
- chromadb not installed → all helpers no-op (return [] / skip embed)
- decisions collection doesn't exist yet → lazy create on first write
- HNSW corruption (the 2026-05-14 UDAP pattern) → caught by the same
  ``_looks_like_chroma_corruption`` predicate; helper returns []
- embed call raises ValueError (e.g. text too long) → caught; SQL write
  succeeds, semantic ranking degraded for that one decision
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Same collection name across all projects — the chromadb persistent client
# is already per-data-dir, so isolation is at the directory level.
_DECISIONS_COLLECTION_NAME = "codevira_decisions"


def _decisions_collection_or_none():
    """Return the ChromaDB decisions collection, or None on any error.

    Lazy-creates the collection on first call. Reuses the same persistent
    client + embedding function as the code-chunk index (one ML model load
    per process).

    Returns:
        Collection instance, OR None if chromadb is unavailable / corrupted
        / any other unrecoverable error. Callers MUST treat None as
        "semantic search disabled" and degrade gracefully (P9).
    """
    try:
        from indexer.index_codebase import (
            _get_chroma_client,
            _get_embedding_fn,
            _looks_like_chroma_corruption,
            ChromaCorrupted,
        )
    except ImportError:
        # indexer not importable (very rare — installation issue).
        logger.debug("indexer.index_codebase not importable; decisions search degraded to BM25 only")
        return None

    try:
        client = _get_chroma_client(probe=False)  # no probe — lazy, per-call
        embed_fn = _get_embedding_fn()
        # get_or_create avoids the "collection exists" race when two MCP
        # tools call into here concurrently.
        return client.get_or_create_collection(
            name=_DECISIONS_COLLECTION_NAME,
            embedding_function=embed_fn,
        )
    except ChromaCorrupted:
        logger.warning(
            "Chroma store corrupted; decisions semantic search disabled. "
            "Run `codevira heal --vectors` to recover."
        )
        return None
    except ImportError as exc:
        # chromadb or sentence-transformers missing
        logger.debug("Semantic search dep missing; decisions search degraded: %s", exc)
        return None
    except Exception as exc:
        if _looks_like_chroma_corruption(exc):
            logger.warning(
                "Chroma corruption detected; decisions semantic search disabled: %s",
                exc,
            )
        else:
            logger.warning("Decisions collection unavailable: %s", exc)
        return None


def embed_decision(
    decision_id: int,
    text: str,
    *,
    session_id: str | None = None,
    file_path: str | None = None,
    context: str | None = None,
) -> bool:
    """Embed a freshly-recorded decision into the semantic collection.

    Best-effort: returns False on any failure but NEVER raises. The SQLite
    row is already committed by the caller — losing the embedding
    degrades search ranking but doesn't lose data.

    Args:
        decision_id: the SQLite primary key (used as the chromadb id).
        text: the decision text — what gets embedded.
        session_id, file_path, context: optional metadata for filtering
            and re-display.

    Returns:
        True if successfully embedded, False otherwise.
    """
    collection = _decisions_collection_or_none()
    if collection is None:
        return False

    # Build the embedding text: decision + optional context so the
    # embedding captures both. Cap at ~4KB to keep embed calls cheap.
    embed_text = text or ""
    if context:
        embed_text = f"{embed_text}\n\nContext: {context}"
    embed_text = embed_text[:4096]
    if not embed_text.strip():
        return False

    metadata: dict[str, Any] = {"decision_id": int(decision_id)}
    if session_id:
        metadata["session_id"] = str(session_id)
    if file_path:
        metadata["file_path"] = str(file_path)

    try:
        # Upsert pattern: delete-then-add is the only safe way to update
        # an embedding in chromadb < 0.5. For new decisions delete is a
        # no-op; for re-embed (backfill / edit) it ensures one canonical
        # vector per decision.
        try:
            collection.delete(ids=[str(decision_id)])
        except Exception:
            pass
        collection.add(
            ids=[str(decision_id)],
            documents=[embed_text],
            metadatas=[metadata],
        )
        return True
    except Exception as exc:
        logger.warning(
            "embed_decision(%s) failed: %s — SQL row preserved, ranking degraded",
            decision_id, exc,
        )
        return False


def semantic_search_decisions(
    query: str,
    limit: int = 10,
    session_id: str | None = None,
) -> list[int]:
    """Run a semantic query against the decisions collection.

    Returns a list of decision IDs in ranked order (best first). Caller
    is responsible for fetching the full decision rows from SQLite by ID
    — keeps SQLite as the source of truth.

    Returns empty list on any failure (P9 graceful degradation): caller
    falls through to BM25-only ranking.
    """
    if not query or not query.strip():
        return []
    collection = _decisions_collection_or_none()
    if collection is None:
        return []

    try:
        # Build a where filter only if session_id is provided. ChromaDB
        # requires non-None values in `where`.
        where = {"session_id": str(session_id)} if session_id else None
        result = collection.query(
            query_texts=[query],
            n_results=max(1, min(limit, 50)),
            where=where,
        )
    except Exception as exc:
        logger.warning("semantic_search_decisions(%r) failed: %s", query, exc)
        return []

    # chromadb returns ids as a list-of-lists (per-query). Flatten to first.
    raw_ids = (result.get("ids") or [[]])[0]
    out: list[int] = []
    for raw in raw_ids:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def rrf_merge(
    bm25_ids: list[int],
    semantic_ids: list[int],
    *,
    k: int = 60,
    limit: int = 10,
) -> list[int]:
    """Reciprocal Rank Fusion of two ranked ID lists.

    For each candidate id, score = sum(1 / (k + rank_i)) across both
    retrievers. Returns IDs sorted by descending score.

    Standard k=60 is from the original RRF paper. Larger k flattens the
    score (more democratic between retrievers); smaller k weights early
    ranks more. 60 is widely used; we don't tune.

    Both inputs may be empty. If both are empty, returns [].
    """
    scores: dict[int, float] = {}
    for rank, did in enumerate(bm25_ids):
        scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank + 1)
    for rank, did in enumerate(semantic_ids):
        scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank + 1)
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [did for did, _ in ranked[:limit]]


def backfill_all_decisions(db) -> dict[str, Any]:
    """Embed every decision currently in SQLite that's not yet in chromadb.

    Used by ``codevira heal --decisions`` after upgrade from v2.0 → v2.1.
    Idempotent: re-running is safe (the embed_decision upsert pattern
    ensures one canonical vector per decision).

    Args:
        db: a SQLiteGraph instance (caller owns the open connection).

    Returns:
        {"total": N, "embedded": M, "failed": F, "skipped": S} summary.
    """
    collection = _decisions_collection_or_none()
    if collection is None:
        return {
            "total": 0,
            "embedded": 0,
            "failed": 0,
            "skipped": 0,
            "note": "chromadb unavailable — semantic search disabled",
        }

    try:
        cur = db.conn.execute(
            "SELECT id, decision, context, session_id, file_path "
            "FROM decisions ORDER BY id ASC"
        )
        rows = list(cur.fetchall())
    except Exception as exc:
        logger.error("backfill_all_decisions: failed to enumerate decisions: %s", exc)
        return {"total": 0, "embedded": 0, "failed": 1, "skipped": 0, "error": str(exc)}

    total = len(rows)
    embedded = 0
    failed = 0
    skipped = 0
    for row in rows:
        did = row["id"]
        text = row["decision"] or ""
        if not text.strip():
            skipped += 1
            continue
        ok = embed_decision(
            decision_id=did,
            text=text,
            session_id=row["session_id"],
            file_path=row["file_path"],
            context=row["context"],
        )
        if ok:
            embedded += 1
        else:
            failed += 1
    return {
        "total": total,
        "embedded": embedded,
        "failed": failed,
        "skipped": skipped,
    }
