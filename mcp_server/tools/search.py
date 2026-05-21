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


def write_session_logs(logs: list[dict]) -> dict[str, Any]:
    """v2.1.2 Item 24: batch variant of write_session_log (in v2.2.0 backend).

    Each item: ``{session_id, task, phase, files_changed?, decisions?,
    next_steps?}``. Independent per-item; partial failure surfaced.

    Returns ``{count, session_ids, errors}``.
    """
    if not isinstance(logs, list):
        return {
            "count": 0,
            "session_ids": [],
            "errors": [{"idx": 0, "error": "logs must be a list"}],
        }

    from mcp_server.storage import sessions_store

    # sessions_store.write_many returns the GENERATED log ids (S000NNN).
    # The integration test contract expects the SESSION ID the caller
    # passed (or batch-N as fallback). So we map item.session_id through
    # while still calling write_many for the persistence side-effect.
    ids: list[str] = []
    errors: list[dict] = []
    valid_logs: list[dict] = []
    requested_session_ids: list[str] = []
    for idx, item in enumerate(logs):
        if not isinstance(item, dict):
            errors.append({"idx": idx, "error": "item must be a dict"})
            continue
        sid = item.get("session_id") or f"batch-{idx}"
        valid_logs.append(
            {
                "session_id": sid,
                "task": item.get("task"),
                "phase": str(item.get("phase", ""))
                if item.get("phase") is not None
                else None,
                "decisions": item.get("decisions") or [],
                "summary": item.get("summary"),
            }
        )
        requested_session_ids.append(sid)

    if valid_logs:
        try:
            _, batch_errors = sessions_store.write_many(valid_logs)
            ids = requested_session_ids
            for be in batch_errors:
                errors.append({"idx": be.get("index", -1), "error": be.get("error")})
        except Exception as exc:  # noqa: BLE001
            errors.append({"idx": -1, "error": str(exc)})

    return {
        "count": len(ids),
        "session_ids": ids,
        "errors": errors,
    }


def write_session_log(
    session_id: str,
    task: str,
    phase: str,
    files_changed: list[str],
    decisions: list[dict],
    next_steps: list[str],
) -> dict[str, Any]:
    """Write a structured session log (v2.2.0: appends to sessions.jsonl).

    v2.1.x had session_id collision detection (auto-suffix); v2.2.0
    keeps the SAME session_id semantics (no auto-suffix; the
    sessions.jsonl format is append-only with timestamps, so multiple
    entries for the same session_id naturally coexist as audit trail).
    """
    from mcp_server.storage import sessions_store

    sessions_store.write(
        session_id=session_id,
        task=task,
        phase=phase,
        summary=None,
        decision_ids=[
            d.get("id")
            for d in (decisions or [])
            if isinstance(d, dict) and d.get("id")
        ],
    )

    return {
        "status": f"Session {session_id} logged to .codevira/sessions.jsonl.",
        "session_id": session_id,
        "requested_session_id": session_id,
        "collision_resolved": False,
    }


def search_decisions(
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    full: bool = False,
    summary_only: bool = False,
    since: str | None = None,
) -> dict[str, Any]:
    """Search past decisions via FTS5 (BM25-ranked).

    v2.2.0: pure FTS5 keyword/stemming search over decisions.jsonl.
    No semantic embedding (chromadb removed). Natural-language queries
    still work via porter stemming + BM25; concept-only queries with
    zero keyword overlap may miss (acceptable trade — see v2.2.0 plan).

    Args:
        query: search terms; empty string returns 0 results.
        limit: max results (clamped to [1, 20]).
        session_id: filter to a session (post-FTS5 filter).
        full: return full decision text (default truncates to 200 chars).
        summary_only: ~70% smaller payload — only {id, summary, score,
            do_not_revert} per result.
        since: ISO 8601 / YYYY-MM-DD; only decisions ts > since returned.

    Returns:
        {query, count, retrieval, threshold_used, results, [hint], [_warning]}
    """
    limit = max(1, min(int(limit), 20))

    from mcp_server.storage import decisions_store

    results = decisions_store.search(query or "", limit=limit, since=since)

    # Apply session_id filter post-search (FTS5 has no notion of session).
    if session_id:
        results = [r for r in results if r.get("session_id") == session_id]

    # v2.2.0 retrieval mode is always "keyword" (no semantic).
    retrieval = "keyword" if results else "keyword-no-results"
    # No threshold concept in v2.2.0 (FTS5 BM25 has no boolean cutoff).
    # Return None so callers can detect "no semantic threshold" semantics.
    threshold = None

    if summary_only:
        slim = [
            {
                "id": r.get("id"),
                "summary": (r.get("decision") or "")[:80],
                "score": r.get("score"),
                "do_not_revert": bool(r.get("do_not_revert", False)),
            }
            for r in results
        ]
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
            if r.get("decision") and len(r["decision"]) > 200:
                r["decision"] = r["decision"][:199] + "…"

    return {
        "query": query,
        "count": len(results),
        "retrieval": retrieval,
        "threshold_used": threshold,
        "results": results,
        "hint": (
            "Pass full=True for untruncated decision text. Increase limit up to 20."
            if not full
            else "Showing full untruncated decisions."
        ),
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
    """Enumerate decisions with optional filters (v2.2.0: from JSONL).

    All filters AND together — only decisions matching every constraint
    are returned. ``tags`` is intersection (all tags must match).

    Args:
        limit: max rows (clamped to [1, 200]).
        since_date: ISO 8601 / YYYY-MM-DD; only ts > since_date.
        file_pattern: fnmatch glob on ``file_path`` (e.g. ``"src/*"``).
        protected_only: only do_not_revert=True.
        session_id: single-session filter.
        tags: intersection — match ALL of the supplied tags.
        include_superseded: default False (hide soft-deleted).
        full: full record vs slim shape.

    Returns ``{count, has_more, decisions, filters_applied}``.
    """
    limit = max(1, min(int(limit), 200))

    from mcp_server.storage import decisions_store

    result = decisions_store.list_all(
        limit=limit,
        since=since_date,
        file_pattern=file_pattern,
        protected_only=protected_only,
        session_id=session_id,
        tags=tags,
        include_superseded=include_superseded,
        full=full,
    )

    return {
        "count": result["count"],
        "has_more": result["has_more"],
        "decisions": result["decisions"],
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
    """Enumerate tags in the project with decision counts (v2.2.0).

    Reads ``.codevira/manifest.yaml`` (O(1) — no scan of decisions.jsonl).
    Sorted by count desc, then alphabetical.

    Returns ``{tags: [{tag, count}, ...], count}``.
    """
    from mcp_server.storage import decisions_store

    res = decisions_store.list_tags_with_counts()
    return {"tags": res["tags"], "count": res.get("total_unique", len(res["tags"]))}


def get_history(
    file_path: str,
    limit: int = 5,
    full: bool = False,
    since: str | None = None,
) -> dict[str, Any]:
    """Return most recent decisions touching a file (v2.2.0).

    Uses ``decisions_store.list_all`` with ``file_pattern=file_path``.
    Defaults to slim shape (~500 tokens total typical).

    Args:
        file_path: literal file path to filter by (no glob; exact match).
        limit: max rows (clamped to [1, 50]).
        full: untruncated decision text.
        since: ISO 8601 / YYYY-MM-DD.

    Returns ``{file_path, returned, limit, has_more, history}``.
    """
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    from mcp_server.storage import decisions_store

    result = decisions_store.list_all(
        limit=limit,
        since=since,
        file_pattern=file_path,
        full=full,
    )
    history = result["decisions"]

    if not full:
        for r in history:
            if r.get("decision") and len(r["decision"]) > 200:
                r["decision"] = r["decision"][:199] + "…"

    return {
        "file_path": file_path,
        "returned": len(history),
        "limit": limit,
        "has_more": result["has_more"],
        "history": history,
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
