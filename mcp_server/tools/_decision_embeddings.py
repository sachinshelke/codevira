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


# 2026-05-19 issue #10: cache the SHAPE of the failure when chromadb /
# torch can't load (sandboxed Antigravity case). Tools that depend on
# semantic search use this to surface a clear one-time warning in their
# response instead of silently degrading to BM25. None = "we haven't
# determined yet". str = "we tried and failed; here's the reason".
_semantic_unavailable_reason: str | None = None


def get_semantic_unavailable_reason() -> str | None:
    """Issue #10: return a human-readable reason if semantic infra
    failed to load in this process, else None.

    The reason is set by :func:`_decisions_collection_or_none` the first
    time it tries to load chromadb + sentence-transformers + torch and
    catches an OSError / ImportError. Subsequent calls don't re-try
    the load.
    """
    return _semantic_unavailable_reason


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
    global _semantic_unavailable_reason
    try:
        from indexer.index_codebase import (
            _get_chroma_client,
            _get_embedding_fn,
            _looks_like_chroma_corruption,
            ChromaCorrupted,
        )
    except ImportError as exc:
        # indexer not importable (very rare — installation issue).
        msg = (
            f"indexer.index_codebase not importable: {exc}. Semantic "
            f"search degraded to BM25 only."
        )
        logger.debug(msg)
        _semantic_unavailable_reason = msg
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
        msg = (
            "Chroma store corrupted. Run `codevira reset --vectors` to "
            "recover (auto-backs-up decisions first)."
        )
        logger.warning(msg + " Decisions semantic search disabled.")
        _semantic_unavailable_reason = msg
        return None
    except ImportError as exc:
        # chromadb or sentence-transformers missing
        msg = (
            f"chromadb / sentence-transformers / torch failed to load: "
            f"{exc}. Semantic search degraded to BM25 only. If running "
            f"under a sandboxed parent process (Antigravity / hardened-"
            f"runtime macOS app), see issue #10 — torch dylib dlopen "
            f"may be blocked by the parent's entitlements."
        )
        logger.debug(msg)
        _semantic_unavailable_reason = msg
        return None
    except OSError as exc:
        # macOS dlopen failures arrive as OSError, not ImportError.
        # Issue #10: torch's libtorch_global_deps.dylib fails to load
        # under Antigravity's sandbox.
        msg = (
            f"Native library load failed: {exc}. Common cause: parent "
            f"process (e.g. Antigravity) sandbox blocks dlopen of "
            f"unsigned PyPI dylibs. Semantic search degraded to BM25 "
            f"only. See issue #10 + docs/troubleshooting/antigravity.md."
        )
        logger.warning(msg)
        _semantic_unavailable_reason = msg
        return None
    except Exception as exc:
        if _looks_like_chroma_corruption(exc):
            msg = (
                f"Chroma corruption detected: {exc}. Run `codevira reset "
                f"--vectors` to recover."
            )
            logger.warning(msg)
            _semantic_unavailable_reason = msg
        else:
            msg = f"Decisions collection unavailable: {exc}"
            logger.warning(msg)
            _semantic_unavailable_reason = msg
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
            decision_id,
            exc,
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

    Note: kept for backward compat. New callers should prefer
    :func:`semantic_search_decisions_scored` so they can apply a
    similarity threshold (v2.1.2 Item 1).
    """
    return [
        did for did, _dist in semantic_search_decisions_scored(query, limit, session_id)
    ]


def semantic_search_decisions_scored(
    query: str,
    limit: int = 10,
    session_id: str | None = None,
) -> list[tuple[int, float]]:
    """v2.1.2 Item 1: scored variant of :func:`semantic_search_decisions`.

    Returns ``[(decision_id, cosine_distance), ...]`` in ranked order
    (lowest distance = most similar). The caller applies a threshold
    cut-off so gibberish queries don't surface "least bad" matches
    (the trust-recovery fix that v2.1.1 missed).

    Distance convention: ChromaDB's default is cosine distance in
    roughly ``[0, 2]``; in practice with the all-MiniLM-L6-v2
    embedding it sits in ``[0, ~1.2]``. Lower = more similar. The
    ``search`` threshold default is 0.45 (filter anything looser);
    ``hook`` injection uses 0.30 (stricter; surfaces fewer false
    positives on prompts only loosely related to existing decisions).
    """
    if not query or not query.strip():
        return []
    collection = _decisions_collection_or_none()
    if collection is None:
        return []

    try:
        where = {"session_id": str(session_id)} if session_id else None
        result = collection.query(
            query_texts=[query],
            n_results=max(1, min(limit, 50)),
            where=where,
            include=["distances"],
        )
    except Exception as exc:
        logger.warning("semantic_search_decisions_scored(%r) failed: %s", query, exc)
        return []

    raw_ids = (result.get("ids") or [[]])[0]
    raw_distances = (result.get("distances") or [[]])[0]
    out: list[tuple[int, float]] = []
    for idx, raw in enumerate(raw_ids):
        try:
            did = int(raw)
        except (TypeError, ValueError):
            continue
        try:
            dist = float(raw_distances[idx]) if idx < len(raw_distances) else 1.0
        except (TypeError, ValueError):
            dist = 1.0
        out.append((did, dist))
    return out


# ---------------------------------------------------------------------
# 2026-05-18 v2.1.2 Item 1: smart self-calibrating similarity threshold
# ---------------------------------------------------------------------

# Static fallback when calibration hasn't run or has too few positive
# samples to be reliable. Distance convention: lower = more similar
# (chromadb cosine distance; range roughly [0, 1.2] for all-MiniLM-L6-v2).
#
# Tuning notes (2026-05-18, empirically measured):
#   - all-MiniLM-L6-v2 puts "same topic, different phrasing" at ~0.55
#     distance ("use bcrypt over argon2" vs "What did we decide about
#     bcrypt for password hashing?")
#   - Truly unrelated content sits at ~0.90 ("how to make a cake" vs
#     "use bcrypt over argon2")
#   - Gap is wide and stable; we set:
#       search  ≤ 0.65 — accept legitimate "same topic" matches,
#                        reject anything semantically distant
#       hook    ≤ 0.55 — auto-injection has higher false-positive cost,
#                        so we require a strong-match signal
#   - The original plan targeted 0.45 / 0.30; empirical testing showed
#     those reject legitimate question-form prompts on this model. The
#     calibrator can still tighten per-project below these defaults.
_STATIC_THRESHOLD_SEARCH = 0.65
_STATIC_THRESHOLD_HOOK = 0.55
# Hook injection uses a STRICTER threshold than user-initiated search.
_HOOK_DELTA = 0.10
# Calibration safety rails — never let the auto-calibrator return a
# threshold so loose that gibberish surfaces, or so tight that almost
# nothing surfaces.
_THRESHOLD_MIN = 0.35
_THRESHOLD_MAX = 0.80
# Minimum positive sample count before we trust a calibration result.
_MIN_POSITIVE_SAMPLES = 5
# Auto-recalibration cadence: every N decisions added, re-fit in a
# background thread.
_CALIBRATION_AUTO_EVERY_N = 10


def _calibration_path():
    """Return the per-project calibration.json path."""
    from mcp_server.paths import get_data_dir

    return get_data_dir() / "calibration.json"


def load_threshold(*, target: str = "search") -> float:
    """Return the active similarity threshold for a retrieval target.

    ``target`` is ``"search"`` (user-initiated search_decisions) or
    ``"hook"`` (cross-session prompt-injection). Order of resolution:

    1. Per-project ``calibration.json`` written by :func:`recalibrate_threshold`
    2. Static defaults (0.45 / 0.30) when calibration is absent or
       has fewer than ``_MIN_POSITIVE_SAMPLES`` positives.

    Never raises — calibration errors fall through to the static default.
    """
    static = _STATIC_THRESHOLD_SEARCH if target == "search" else _STATIC_THRESHOLD_HOOK
    try:
        import json

        p = _calibration_path()
        if not p.is_file():
            return static
        data = json.loads(p.read_text())
        if int(data.get("positive_samples", 0)) < _MIN_POSITIVE_SAMPLES:
            return static
        # The canonical stored value is the search threshold; derive hook
        # by subtracting the delta so both stay in lockstep.
        search_thr = float(data.get("threshold_search", static))
        if target == "hook":
            return max(_THRESHOLD_MIN, search_thr - _HOOK_DELTA)
        return search_thr
    except Exception as exc:  # noqa: BLE001
        logger.debug("load_threshold(%s) fell back to static: %s", target, exc)
        return static


def recalibrate_threshold(db=None) -> dict[str, Any]:
    """Re-fit the similarity threshold from the project's positive samples.

    Algorithm (kept deliberately ML-free — pure descriptive statistics):

    1. Pull "positive samples" — decisions the user clearly valued
       (``do_not_revert=True`` OR has a confirmed-kept outcome).
    2. For each, find its 10 nearest neighbours in the project's
       ChromaDB decisions collection. Record those cosine distances.
    3. The 75th percentile of that distribution is the new threshold:
       "be at least as similar as 75% of historical surface-worthy
       pairs."
    4. Clamp to ``[_THRESHOLD_MIN, _THRESHOLD_MAX]``.
    5. Persist to ``<data_dir>/calibration.json`` with sample count +
       timestamp metadata.

    Returns a dict summary:
        {
            "positive_samples": int,
            "neighbor_distances_collected": int,
            "threshold_search": float,
            "threshold_hook": float,
            "p75_raw": float,
            "clamped": bool,
            "static_default": bool,  # True if fewer than MIN_POSITIVE_SAMPLES
            "calibrated_at": ISO timestamp,
        }
    """
    import json
    from datetime import datetime, timezone

    own_db = False
    if db is None:
        try:
            from indexer.sqlite_graph import SQLiteGraph
            from mcp_server.paths import get_data_dir

            graph_db_path = get_data_dir() / "graph" / "graph.db"
            if not graph_db_path.is_file():
                return {
                    "positive_samples": 0,
                    "threshold_search": _STATIC_THRESHOLD_SEARCH,
                    "threshold_hook": _STATIC_THRESHOLD_HOOK,
                    "static_default": True,
                    "note": "no graph.db — calibration skipped",
                }
            db = SQLiteGraph(graph_db_path)
            own_db = True
        except Exception as exc:
            logger.warning("recalibrate_threshold: cannot open db: %s", exc)
            return {
                "positive_samples": 0,
                "threshold_search": _STATIC_THRESHOLD_SEARCH,
                "threshold_hook": _STATIC_THRESHOLD_HOOK,
                "static_default": True,
                "error": str(exc),
            }

    try:
        # Step 1: pull positive samples.
        positives: list[tuple[int, str]] = []
        try:
            cur = db.conn.execute(
                "SELECT id, decision FROM decisions "
                "WHERE do_not_revert = 1 AND decision IS NOT NULL AND decision != '' "
                "ORDER BY id ASC"
            )
            for row in cur.fetchall():
                positives.append((int(row["id"]), str(row["decision"])))
        except Exception as exc:
            logger.debug("recalibrate_threshold: do_not_revert query failed: %s", exc)
        # Also pull kept-by-outcome decisions (the second axis of "user
        # valued this"). outcomes.classification = 'kept' is the signal.
        try:
            cur = db.conn.execute(
                "SELECT d.id, d.decision FROM decisions d "
                "JOIN outcomes o ON o.decision_id = d.id "
                "WHERE LOWER(o.classification) = 'kept' "
                "AND d.decision IS NOT NULL AND d.decision != ''"
            )
            seen_ids = {p[0] for p in positives}
            for row in cur.fetchall():
                if int(row["id"]) not in seen_ids:
                    positives.append((int(row["id"]), str(row["decision"])))
        except Exception as exc:
            logger.debug("recalibrate_threshold: outcomes query failed: %s", exc)

        result: dict[str, Any] = {
            "positive_samples": len(positives),
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
        }

        if len(positives) < _MIN_POSITIVE_SAMPLES:
            result.update(
                {
                    "threshold_search": _STATIC_THRESHOLD_SEARCH,
                    "threshold_hook": _STATIC_THRESHOLD_HOOK,
                    "static_default": True,
                    "note": f"fewer than {_MIN_POSITIVE_SAMPLES} positive samples — using static defaults",
                }
            )
            return result

        # Step 2: for each positive, find its 10 nearest neighbours.
        collection = _decisions_collection_or_none()
        if collection is None:
            result.update(
                {
                    "threshold_search": _STATIC_THRESHOLD_SEARCH,
                    "threshold_hook": _STATIC_THRESHOLD_HOOK,
                    "static_default": True,
                    "note": "chromadb unavailable — using static defaults",
                }
            )
            return result

        all_distances: list[float] = []
        for pid, ptext in positives:
            try:
                qres = collection.query(
                    query_texts=[ptext[:4096]],
                    n_results=10,
                    include=["distances"],
                )
                dists = (qres.get("distances") or [[]])[0]
                ids = (qres.get("ids") or [[]])[0]
                # Exclude self-match (the positive's own embedding will
                # always rank distance 0 — would skew low).
                for idx, raw_id in enumerate(ids):
                    if str(raw_id) == str(pid):
                        continue
                    if idx < len(dists):
                        try:
                            all_distances.append(float(dists[idx]))
                        except (TypeError, ValueError):
                            continue
            except Exception as exc:
                logger.debug("calibration query for positive %s failed: %s", pid, exc)
                continue

        result["neighbor_distances_collected"] = len(all_distances)

        if not all_distances:
            result.update(
                {
                    "threshold_search": _STATIC_THRESHOLD_SEARCH,
                    "threshold_hook": _STATIC_THRESHOLD_HOOK,
                    "static_default": True,
                    "note": "no neighbor distances collected — using static defaults",
                }
            )
            return result

        # Step 3: 75th percentile.
        all_distances.sort()
        p75_index = int(len(all_distances) * 0.75)
        p75_index = min(p75_index, len(all_distances) - 1)
        p75_raw = all_distances[p75_index]
        result["p75_raw"] = p75_raw

        # Step 4: clamp.
        clamped_thr = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, p75_raw))
        result["clamped"] = clamped_thr != p75_raw
        result["threshold_search"] = clamped_thr
        result["threshold_hook"] = max(_THRESHOLD_MIN, clamped_thr - _HOOK_DELTA)
        result["static_default"] = False

        # Step 5: persist.
        try:
            p = _calibration_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(result, indent=2, default=str))
            tmp.replace(p)
        except Exception as exc:
            logger.warning(
                "recalibrate_threshold: failed to persist calibration: %s", exc
            )
            result["persist_error"] = str(exc)

        return result
    finally:
        if own_db:
            try:
                db.close()
            except Exception:
                pass


def maybe_auto_recalibrate(decisions_count_total: int) -> bool:
    """Trigger a background recalibration if ``decisions_count_total`` is
    a multiple of ``_CALIBRATION_AUTO_EVERY_N``.

    Non-blocking (runs in a daemon thread). Returns True if a
    recalibration thread was spawned, False otherwise.

    Called from :func:`record_decision` after a successful write.
    """
    if decisions_count_total <= 0:
        return False
    if decisions_count_total % _CALIBRATION_AUTO_EVERY_N != 0:
        return False
    import threading

    def _bg():
        try:
            recalibrate_threshold()
        except Exception as exc:
            logger.debug("auto-recalibration failed: %s", exc)

    t = threading.Thread(target=_bg, daemon=True, name="codevira-recalibrate")
    t.start()
    return True


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
