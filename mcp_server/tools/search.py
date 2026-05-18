from __future__ import annotations

from typing import Any

from mcp_server.paths import get_data_dir
from indexer.sqlite_graph import SQLiteGraph


def _get_db() -> SQLiteGraph:
    db_path = get_data_dir() / "graph" / "graph.db"
    return SQLiteGraph(db_path)


# Module-level cache for the semantic search stack.
# Creating SentenceTransformerEmbeddingFunction triggers:
#   - ~90MB model download on first use (cached in ~/.cache/huggingface/)
#   - PyTorch/onnxruntime init (~1-3s even after download)
# Caching avoids paying this cost on every search_codebase call — first
# call is still slow but subsequent calls are instant.
_chroma_cache: dict = {"client": None, "embed_fn": None, "db_dir": None}


def _get_chroma_client():
    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except ImportError:
        return None, None

    db_dir = get_data_dir() / "codeindex"
    if not db_dir.exists():
        return None, None

    # Reuse cached client + embed_fn if we're hitting the same project's index
    # (PersistentClient is tied to a specific path; invalidate if path changes).
    if (
        _chroma_cache["client"] is not None
        and _chroma_cache["embed_fn"] is not None
        and _chroma_cache["db_dir"] == str(db_dir)
    ):
        return _chroma_cache["client"], _chroma_cache["embed_fn"]

    client = chromadb.PersistentClient(path=str(db_dir))
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    _chroma_cache["client"] = client
    _chroma_cache["embed_fn"] = embed_fn
    _chroma_cache["db_dir"] = str(db_dir)
    return client, embed_fn


def prewarm_embedding_model() -> None:
    """Pre-load the embedding model in a background thread at server startup.

    SentenceTransformerEmbeddingFunction triggers a ~90MB model download on
    first use and 1-3s of PyTorch init. If we wait until the first
    search_codebase() call, Antigravity's ~30s MCP timeout can kill the query.

    Called from server.main() and http_server.run_http_server() via a daemon
    thread — the server becomes ready immediately; search gets warmed up in
    parallel. Safe to call multiple times (cached after first successful load).
    """
    import threading

    def _warmup():
        try:
            _get_chroma_client()  # populates _chroma_cache
        except Exception:
            # Failure is non-fatal — search_codebase will retry on next call
            pass

    t = threading.Thread(target=_warmup, daemon=True, name="codevira-embed-prewarm")
    t.start()


def search_codebase(
    description: str, top_k: int = 5, include_content: bool = False
) -> dict[str, Any]:
    """Semantic search over the codebase.

    Returns file/symbol pointers by default — NOT full source code. This keeps
    the response token-efficient (~300 tokens for 5 matches). Call get_code(
    file_path, symbol) to read source for a specific match.

    Pass include_content=True only when you explicitly need the chunk source
    inline (can be 500-3000 tokens per match).
    """
    # Fast path: if another thread is still pre-warming the embedding model,
    # return a "warming" status immediately instead of blocking for 30s+ and
    # triggering the MCP client's timeout. Agent retries get served once
    # warmup is done.
    if _chroma_cache["embed_fn"] is None:
        # Cache miss. Try to populate it (fast if model is already downloaded
        # and cached on disk; slow only the first time ever across any project).
        # Wrap in a short timeout so we don't block the MCP thread.
        import threading as _th

        load_done = _th.Event()
        load_err = [None]

        def _load():
            try:
                _get_chroma_client()
            except Exception as e:
                load_err[0] = e
            finally:
                load_done.set()

        _th.Thread(target=_load, daemon=True, name="codevira-embed-sync-load").start()
        # Wait up to 10 seconds — enough for warm-cache loads, short of MCP timeout
        if not load_done.wait(timeout=10.0):
            return {
                "status": "warming",
                "message": (
                    "Semantic search model is loading (first-time setup downloads ~90MB). "
                    "Try this query again in 30-60 seconds. Other tools work normally."
                ),
            }
        if load_err[0] is not None:
            return {"error": f"Embedding model load failed: {load_err[0]}"}

    client, embed_fn = _get_chroma_client()
    if not client:
        # P0-A (rc.5): graceful fallback — try the structural search instead
        # of bailing with an unhelpful "Reinstall codevira" hint. The real
        # fix when the semantic index is missing is `codevira index`, not
        # a reinstall (chromadb has been bundled since rc.4).
        # First check if a build is currently in progress.
        try:
            from mcp_server.auto_init import get_init_progress

            prog = get_init_progress()
            if prog["status"] in ("initializing", "indexing"):
                return {
                    "status": "indexing",
                    "message": "Semantic index is being built in the background. Try again in a few seconds.",
                }
        except Exception:
            pass
        return _structural_fallback(
            description,
            top_k,
            reason="semantic index not built yet",
        )

    # Cap top_k to avoid token bombs
    if top_k < 1:
        top_k = 1
    if top_k > 20:
        top_k = 20

    try:
        collection = client.get_collection(
            "codebase_index", embedding_function=embed_fn
        )
        results = collection.query(query_texts=[description], n_results=top_k)

        matches = []
        if results["documents"] and results["documents"][0]:
            for i in range(len(results["documents"][0])):
                doc = results["documents"][0][i]
                meta = results["metadatas"][0][i]
                match = {
                    "file_path": meta["file_path"],
                    "chunk_type": meta["chunk_type"],
                    "name": meta["name"],
                    "relevance_score": round(
                        1.0
                        - (results["distances"][0][i] if "distances" in results else 0),
                        3,
                    ),
                }
                if include_content:
                    match["content"] = doc
                matches.append(match)

        return {
            "query": description,
            "matches": matches,
            "hint": (
                "Call get_code(file_path, symbol) to read source for a match. "
                "Pass include_content=True to get inline source (larger response)."
            ),
        }
    except Exception as e:
        try:
            from mcp_server.crash_logger import log_crash

            log_crash(e, context="search_codebase")
        except Exception:
            pass
        return {"error": f"Search failed: {e}"}


def _structural_fallback(query: str, top_k: int, *, reason: str) -> dict[str, Any]:
    """P0-A (rc.5): structural fallback when semantic search is unavailable.

    Returns the same shape as a normal search_codebase response but uses the
    graph DB's symbols + nodes tables (filename + symbol substring match) for
    ranking. The user gets RESULTS — not an error — and a clear note that
    semantic ranking is unavailable.

    The `reason` parameter explains WHY semantic search couldn't be used; it's
    surfaced in the response so callers can react (e.g., "the index isn't
    built — run `codevira index` to enable semantic ranking").
    """
    matches: list[dict[str, Any]] = []
    graph_open = False
    try:
        from mcp_server.paths import get_data_dir
        from indexer.sqlite_graph import SQLiteGraph

        graph_db_path = get_data_dir() / "graph" / "graph.db"
        if not graph_db_path.is_file():
            return {
                "matches": [],
                "warning": (
                    f"Semantic search unavailable ({reason}) AND no graph DB at "
                    f"{graph_db_path}. Run `codevira index` to build both."
                ),
                "fix_command": "codevira index",
            }
        g = SQLiteGraph(graph_db_path)
        graph_open = True
        # Tokenise the query for cheap substring matching.
        terms = [t.lower() for t in query.split() if len(t) > 2]
        if not terms:
            terms = [query.lower()]
        seen: set[tuple[str, str]] = set()
        # Symbol-name match (function / class names that mention any term).
        try:
            for term in terms:
                rows = g.conn.execute(
                    "SELECT name, file_path, kind FROM symbols "
                    "WHERE LOWER(name) LIKE ? "
                    "ORDER BY name LIMIT ?",
                    (f"%{term}%", top_k * 2),
                ).fetchall()
                for r in rows:
                    key = (r["file_path"], r["name"])
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append(
                        {
                            "file_path": r["file_path"],
                            "chunk_type": r["kind"],
                            "name": r["name"],
                            "match_type": "symbol_substring",
                        }
                    )
                    if len(matches) >= top_k:
                        break
                if len(matches) >= top_k:
                    break
        except Exception:
            # symbols table missing or empty; fall through to filename match.
            pass
        # Filename match (file paths that mention any term) — only if we
        # haven't filled top_k from symbols.
        if len(matches) < top_k:
            try:
                for term in terms:
                    rows = g.conn.execute(
                        "SELECT DISTINCT file_path FROM nodes "
                        "WHERE LOWER(file_path) LIKE ? "
                        "ORDER BY file_path LIMIT ?",
                        (f"%{term}%", top_k * 2),
                    ).fetchall()
                    for r in rows:
                        key = (r["file_path"], "")
                        if key in seen:
                            continue
                        seen.add(key)
                        matches.append(
                            {
                                "file_path": r["file_path"],
                                "chunk_type": "file",
                                "name": "",
                                "match_type": "filename_substring",
                            }
                        )
                        if len(matches) >= top_k:
                            break
                    if len(matches) >= top_k:
                        break
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if graph_open:
            try:
                g.close()
            except Exception:
                pass

    return {
        "query": query,
        "matches": matches,
        "warning": (
            f"Semantic ranking unavailable ({reason}) — falling back to "
            f"structural (substring) matches against the graph DB. "
            f"Run `codevira index` to enable semantic search."
        ),
        "fix_command": "codevira index",
    }


def write_session_log(
    session_id: str,
    task: str,
    phase: str,
    files_changed: list[str],
    decisions: list[dict],
    next_steps: list[str],
) -> dict[str, Any]:
    """Write a structured session log to SQLite Memory.

    2026-05-18 v2.1.2 Item 22: if ``session_id`` collides with an
    existing row that has different content, the storage layer
    auto-suffixes with a short hash. The response always surfaces the
    ACTUAL session_id written via the ``session_id`` field so the
    caller knows what id to use for subsequent ``record_decision`` etc.
    calls (and ``collision_resolved=True`` when the requested id was
    rewritten).
    """
    db = _get_db()
    actual_session_id = db.log_session(session_id, task, phase, decisions)
    db.close()

    # v1.5: Export qualifying learnings to global memory
    try:
        from mcp_server.global_sync import export_project_to_global

        export_project_to_global()
    except Exception as e:
        try:
            from mcp_server.crash_logger import log_crash

            log_crash(e, context="write_session_log: global export")
        except Exception:
            pass

    collision = actual_session_id != session_id
    return {
        "status": f"Session {actual_session_id} logged to SQLite Memory.",
        "session_id": actual_session_id,
        "requested_session_id": session_id,
        "collision_resolved": collision,
        **(
            {
                "note": (
                    f"Requested session_id {session_id!r} already existed with "
                    f"different content; auto-suffixed to {actual_session_id!r} to "
                    f"avoid silent merge. Pass the new id to subsequent "
                    f"record_decision / get_session_context calls."
                )
            }
            if collision
            else {}
        ),
    }


def search_decisions(
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    full: bool = False,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Search past decisions across sessions, changesets, and roadmap phases.

    2026-05-17 v2.1.1: hybrid retrieval — BM25 keyword (SQL LIKE) AND
    semantic embedding (ChromaDB), merged with Reciprocal Rank Fusion.
    Natural-language queries like ``"DDD architecture layer"`` — which
    produced 0 hits in v2.0's BM25-only search — now surface the right
    decisions.

    2026-05-18 v2.1.2 Item 1: applies a smart self-calibrating similarity
    THRESHOLD before RRF so gibberish queries (``"how to make a cake"``,
    ``"zzzzzz xqzv9"``) return zero results instead of "least bad"
    matches. The threshold defaults to 0.45 (cosine distance — lower =
    more similar) and re-fits per project every ~10 decisions.

    2026-05-18 v2.1.2 Item 28: ``summary_only=True`` returns a slim
    payload (id + summary + score + do_not_revert only) — ~70% smaller
    response for AI triage queries.

    Default: returns up to 5 matches with truncated context (150 chars).
    Pass ``full=True`` for untruncated text. Response includes:
        - ``retrieval``: ``"hybrid" | "keyword" | "semantic" |
          "semantic-no-results-above-threshold"``
        - ``threshold_used``: the active cosine-distance cut-off
        - per-result ``score``: cosine distance for the semantic match
          (omitted for keyword-only hits)
    """
    if limit < 1:
        limit = 1
    if limit > 20:
        limit = 20

    # 2026-05-18 v2.1.2 Item 1: load active threshold (calibrated or static).
    try:
        from mcp_server.tools._decision_embeddings import load_threshold

        threshold = load_threshold(target="search")
    except Exception:
        threshold = 0.45

    db = _get_db()
    try:
        # ─── BM25 / SQL LIKE pass (existing behavior, kept as ranked source) ──
        bm25_results = db.search_decisions(query, limit * 3, session_id)
        bm25_ids = [r["id"] for r in bm25_results if r.get("id") is not None]

        # ─── Semantic / ChromaDB pass with threshold cut-off ─────────────────
        # 2026-05-18 v2.1.2 Item 1: use scored variant + filter.
        semantic_scored: list[tuple[int, float]] = []
        try:
            from mcp_server.tools._decision_embeddings import (
                semantic_search_decisions_scored,
                rrf_merge,
            )

            raw_scored = semantic_search_decisions_scored(query, limit * 3, session_id)
            semantic_scored = [(did, d) for did, d in raw_scored if d <= threshold]
        except Exception:
            semantic_scored = []

        semantic_ids = [did for did, _d in semantic_scored]
        semantic_score_by_id: dict[int, float] = dict(semantic_scored)

        # ─── Merge ────────────────────────────────────────────────────────────
        retrieval: str
        if semantic_ids and bm25_ids:
            try:
                from mcp_server.tools._decision_embeddings import rrf_merge

                merged_ids = rrf_merge(bm25_ids, semantic_ids, limit=limit)
            except Exception:
                merged_ids = bm25_ids[:limit]
                semantic_ids = []
            retrieval = "hybrid"
        elif semantic_ids:
            merged_ids = semantic_ids[:limit]
            retrieval = "semantic"
        elif bm25_ids:
            merged_ids = bm25_ids[:limit]
            retrieval = "keyword"
        else:
            # Both retrievers returned nothing. If semantic ran but everything
            # was above threshold, distinguish that case so the caller knows
            # "search worked, just nothing similar enough" vs "search broken."
            try:
                raw_scored_unfiltered = semantic_search_decisions_scored(
                    query, 1, session_id
                )
                if raw_scored_unfiltered:
                    retrieval = "semantic-no-results-above-threshold"
                else:
                    retrieval = "no-matches"
            except Exception:
                retrieval = "no-matches"
            merged_ids = []

        # Resolve IDs → full decision rows.
        id_to_row = {r["id"]: r for r in bm25_results}
        missing_ids = [i for i in merged_ids if i not in id_to_row]
        if missing_ids:
            placeholders = ",".join("?" * len(missing_ids))
            cur = db.conn.execute(
                f"SELECT d.id, d.decision, d.context, d.file_path, "
                f"d.do_not_revert, s.summary, s.phase, d.created_at "
                f"FROM decisions d JOIN sessions s "
                f"ON d.session_id = s.session_id WHERE d.id IN ({placeholders})",
                missing_ids,
            )
            for row in cur.fetchall():
                d = dict(row)
                if "do_not_revert" in d:
                    d["do_not_revert"] = bool(d["do_not_revert"])
                id_to_row[row["id"]] = d
        results = [id_to_row[i] for i in merged_ids if i in id_to_row]

        # Annotate each result with its semantic score (cosine distance).
        # Keyword-only matches get score=None so the caller can tell them apart.
        for r in results:
            rid = r.get("id")
            r["score"] = semantic_score_by_id.get(int(rid)) if rid is not None else None
    finally:
        db.close()

    if summary_only:
        # 2026-05-18 v2.1.2 Item 28: ~70% smaller payload for triage queries.
        slim = []
        for r in results:
            slim.append(
                {
                    "id": r.get("id"),
                    "summary": r.get("summary"),
                    "score": r.get("score"),
                    "do_not_revert": r.get("do_not_revert"),
                }
            )
        return {
            "query": query,
            "count": len(slim),
            "retrieval": retrieval,
            "threshold_used": threshold,
            "results": slim,
            "mode": "summary_only",
        }

    if not full:
        for r in results:
            if r.get("decision"):
                r["decision"] = (
                    (r["decision"][:199] + "…")
                    if len(r["decision"]) > 200
                    else r["decision"]
                )
            if r.get("context"):
                r["context"] = (
                    (r["context"][:149] + "…")
                    if len(r["context"]) > 150
                    else r["context"]
                )
            if r.get("summary"):
                r["summary"] = (
                    (r["summary"][:99] + "…")
                    if len(r["summary"]) > 100
                    else r["summary"]
                )

    return {
        "query": query,
        "count": len(results),
        "retrieval": retrieval,
        "threshold_used": threshold,
        "results": results,
        "hint": "Pass full=True for untruncated decision text. Increase limit up to 20."
        if not full
        else "Showing full untruncated decisions.",
    }


def list_decisions(
    limit: int = 20,
    since_date: str | None = None,
    file_pattern: str | None = None,
    protected_only: bool = False,
    session_id: str | None = None,
    tags: list[str] | None = None,
    include_superseded: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """2026-05-18 v2.1.2 Item 11: enumerate decisions with optional filters.

    Without filters, returns the ``limit`` most recently created decisions.
    All filters AND together — only decisions matching every supplied
    constraint are returned.

    Args:
        limit: max rows to return (clamped to [1, 200]).
        since_date: ISO 8601 timestamp (or YYYY-MM-DD). Only decisions
            ``created_at > since_date`` are returned. v2.1.2 Item 25.
        file_pattern: SQL LIKE pattern on ``file_path`` (e.g. ``"src/%"``).
        protected_only: filter to ``do_not_revert = 1`` rows only.
        session_id: filter to a single session.
        tags: filter to rows that have ALL of the supplied tags (Item 27).
        include_superseded: by default soft-deleted (Item 26 supersede)
            rows are hidden; pass True to include them.
        full: untruncated decision text.

    Returns:
        {"count": N, "decisions": [...], "has_more": bool, "filters_applied": {...}}
    """
    limit = max(1, min(int(limit), 200))
    where_clauses: list[str] = []
    params: list = []

    if since_date:
        where_clauses.append("d.created_at > ?")
        params.append(since_date)
    if file_pattern:
        where_clauses.append("d.file_path LIKE ?")
        params.append(file_pattern)
    if protected_only:
        where_clauses.append("d.do_not_revert = 1")
    if session_id:
        where_clauses.append("d.session_id = ?")
        params.append(session_id)

    db = _get_db()
    try:
        # Item 26: default-hide superseded rows. Best-effort: schema may
        # not yet have the column, in which case the filter is a no-op.
        if not include_superseded:
            try:
                db.conn.execute("SELECT is_superseded FROM decisions LIMIT 1")
                where_clauses.append("(d.is_superseded = 0 OR d.is_superseded IS NULL)")
            except Exception:
                pass

        # Item 27: tag filter — INTERSECT decision_tags.
        if tags:
            try:
                norm_tags = sorted(
                    {str(t).strip().lower() for t in tags if str(t).strip()}
                )
                placeholders = ",".join("?" * len(norm_tags))
                where_clauses.append(
                    f"d.id IN (SELECT decision_id FROM decision_tags "
                    f"WHERE tag IN ({placeholders}) "
                    f"GROUP BY decision_id HAVING COUNT(DISTINCT tag) = {len(norm_tags)})"
                )
                params.extend(norm_tags)
            except Exception:
                pass

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = (
            "SELECT d.id, d.decision, d.context, d.file_path, "
            "d.do_not_revert, d.session_id, d.created_at, s.summary, s.phase "
            f"FROM decisions d LEFT JOIN sessions s ON d.session_id = s.session_id "
            f"{where_sql} ORDER BY d.created_at DESC LIMIT ?"
        )
        cur = db.conn.execute(sql, [*params, limit + 1])
        rows = [dict(r) for r in cur.fetchall()]
        has_more = len(rows) > limit
        results = rows[:limit]

        for r in results:
            if "do_not_revert" in r:
                r["do_not_revert"] = bool(r["do_not_revert"])
            # Attach tags (best-effort).
            try:
                tag_cur = db.conn.execute(
                    "SELECT tag FROM decision_tags WHERE decision_id = ? ORDER BY tag",
                    (r["id"],),
                )
                r["tags"] = [row["tag"] for row in tag_cur.fetchall()]
            except Exception:
                r["tags"] = []
    finally:
        db.close()

    if not full:
        for r in results:
            if r.get("decision") and len(r["decision"]) > 200:
                r["decision"] = r["decision"][:199] + "…"
            if r.get("context") and len(r["context"]) > 150:
                r["context"] = r["context"][:149] + "…"

    return {
        "count": len(results),
        "has_more": has_more,
        "decisions": results,
        "filters_applied": {
            "since_date": since_date,
            "file_pattern": file_pattern,
            "protected_only": protected_only,
            "session_id": session_id,
            "tags": list(tags) if tags else None,
            "include_superseded": include_superseded,
            "limit": limit,
        },
    }


def list_tags() -> dict[str, Any]:
    """2026-05-18 v2.1.2 Item 27: enumerate all tags in the project with
    decision counts. Useful for discovery / catalog.

    Returns ``{"tags": [{"tag": str, "count": int}, ...]}`` sorted by
    count desc. Returns empty list if the ``decision_tags`` table
    doesn't exist yet (pre-v2.1.2 schema).
    """
    db = _get_db()
    try:
        try:
            cur = db.conn.execute(
                "SELECT tag, COUNT(*) AS c FROM decision_tags "
                "GROUP BY tag ORDER BY c DESC, tag ASC"
            )
            tags = [{"tag": r["tag"], "count": r["c"]} for r in cur.fetchall()]
        except Exception:
            tags = []
    finally:
        db.close()
    return {"tags": tags, "count": len(tags)}


def get_history(file_path: str, limit: int = 5, full: bool = False) -> dict[str, Any]:
    """Return most recent decisions touching a file.

    Default: 5 most recent, with truncated context (150 chars each).
    Response stays under ~500 tokens for typical use.

    Pass full=True for untruncated decisions, or increase limit up to 50.
    """
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    db = _get_db()
    sql = """
        SELECT d.decision, d.context, s.summary, d.created_at, d.session_id
        FROM decisions d
        JOIN sessions s ON d.session_id = s.session_id
        WHERE d.file_path = ? OR s.summary LIKE ?
        ORDER BY d.created_at DESC
        LIMIT ?
    """
    cur = db.conn.execute(sql, (file_path, f"%{file_path}%", limit + 1))
    rows = cur.fetchall()
    has_more = len(rows) > limit
    results = [dict(r) for r in rows[:limit]]
    db.close()

    if not full:
        for r in results:
            if r.get("decision") and len(r["decision"]) > 200:
                r["decision"] = r["decision"][:199] + "…"
            if r.get("context") and len(r["context"]) > 150:
                r["context"] = r["context"][:149] + "…"
            if r.get("summary") and len(r["summary"]) > 100:
                r["summary"] = r["summary"][:99] + "…"

    return {
        "file_path": file_path,
        "returned": len(results),
        "limit": limit,
        "has_more": has_more,
        "history": results,
    }


def refresh_index(file_paths: list[str]) -> dict:
    """Trigger an incremental reindex of changed files (non-blocking).

    Returns immediately with a "started" status. The actual work (graph
    update + optional semantic indexing) runs in a background thread.

    For large projects, semantic reindexing can take minutes. If this ran
    synchronously, the MCP tool call would hang the calling AI agent.
    Use get_session_context() to check progress via indexing_progress field.
    """
    import threading
    from indexer.index_codebase import _check_search_deps

    requested_files = file_paths or None

    def _background_refresh():
        try:
            from mcp_server.tools.graph import refresh_graph

            refresh_graph(file_paths=file_paths if file_paths else None)
        except Exception as e:
            try:
                from mcp_server.crash_logger import log_crash

                log_crash(e, context="refresh_index: graph refresh (background)")
            except Exception:
                pass

        if _check_search_deps():
            try:
                from indexer.index_codebase import cmd_incremental

                cmd_incremental(quiet=True, file_paths=requested_files)
            except Exception as e:
                try:
                    from mcp_server.crash_logger import log_crash

                    log_crash(e, context="refresh_index: semantic index (background)")
                except Exception:
                    pass

    t = threading.Thread(
        target=_background_refresh, daemon=True, name="codevira-refresh-index"
    )
    t.start()

    mode = "targeted" if requested_files else "incremental"
    note = "Search index will update in background."
    if not _check_search_deps():
        note = "Graph only (semantic search not installed)."

    return {
        "status": "Refresh started in background.",
        "mode": mode,
        "note": note,
        **({"file_paths": requested_files} if requested_files else {}),
    }
