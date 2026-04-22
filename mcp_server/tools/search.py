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

def search_codebase(description: str, top_k: int = 5, include_content: bool = False) -> dict[str, Any]:
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
        # v1.6: Check if auto-init is running and return a friendly status
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
        return {
            "error": "Semantic index not found.",
            "hint": "Reinstall codevira (chromadb is included by default): pip install --upgrade codevira, then run: codevira index --full",
        }

    # Cap top_k to avoid token bombs
    if top_k < 1:
        top_k = 1
    if top_k > 20:
        top_k = 20

    try:
        collection = client.get_collection("codebase_index", embedding_function=embed_fn)
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
                        1.0 - (results["distances"][0][i] if "distances" in results else 0),
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

def write_session_log(session_id: str, task: str, phase: str, files_changed: list[str], decisions: list[dict], next_steps: list[str]) -> dict[str, str]:
    """Write a structured session log to SQLite Memory."""
    db = _get_db()
    db.log_session(session_id, task, phase, decisions)
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

    return {"status": f"Session {session_id} logged to SQLite Memory."}

def search_decisions(
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Search past decisions across sessions, changesets, and roadmap phases.

    Default: returns up to 5 matches with truncated context (150 chars each)
    to keep the response token-efficient (~500 tokens total).

    Pass full=True to get untruncated decision + context + summary text.
    """
    if limit < 1:
        limit = 1
    if limit > 20:
        limit = 20

    db = _get_db()
    results = db.search_decisions(query, limit, session_id)
    db.close()

    if not full:
        # Truncate verbose text fields
        for r in results:
            if r.get("decision"):
                r["decision"] = (r["decision"][:199] + "…") if len(r["decision"]) > 200 else r["decision"]
            if r.get("context"):
                r["context"] = (r["context"][:149] + "…") if len(r["context"]) > 150 else r["context"]
            if r.get("summary"):
                r["summary"] = (r["summary"][:99] + "…") if len(r["summary"]) > 100 else r["summary"]

    return {
        "query": query,
        "count": len(results),
        "results": results,
        "hint": "Pass full=True for untruncated decision text. Increase limit up to 20."
                if not full else "Showing full untruncated decisions.",
    }

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
    sql = '''
        SELECT d.decision, d.context, s.summary, d.created_at, d.session_id
        FROM decisions d
        JOIN sessions s ON d.session_id = s.session_id
        WHERE d.file_path = ? OR s.summary LIKE ?
        ORDER BY d.created_at DESC
        LIMIT ?
    '''
    cur = db.conn.execute(sql, (file_path, f'%{file_path}%', limit + 1))
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

    t = threading.Thread(target=_background_refresh, daemon=True, name="codevira-refresh-index")
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
