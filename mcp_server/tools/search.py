import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp_server.paths import get_data_dir, get_project_root
from mcp_server.tools.graph import get_impact
from indexer.sqlite_graph import SQLiteGraph

def _get_db() -> SQLiteGraph:
    db_path = get_data_dir() / "graph" / "graph.db"
    return SQLiteGraph(db_path)

def _get_chroma_client():
    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except ImportError:
        return None, None

    db_dir = get_data_dir() / "codeindex"
    if not db_dir.exists():
        return None, None

    client = chromadb.PersistentClient(path=str(db_dir))
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client, embed_fn

def search_codebase(description: str, top_k: int = 5) -> dict[str, Any]:
    client, embed_fn = _get_chroma_client()
    if not client:
        return {"error": "Index not found. Run 'codevira index --full'."}

    try:
        collection = client.get_collection("codebase_index", embedding_function=embed_fn)
        results = collection.query(query_texts=[description], n_results=top_k)
        
        matches = []
        if results["documents"] and results["documents"][0]:
            for i in range(len(results["documents"][0])):
                doc = results["documents"][0][i]
                meta = results["metadatas"][0][i]
                matches.append({
                    "file_path": meta["file_path"],
                    "chunk_type": meta["chunk_type"],
                    "name": meta["name"],
                    "content": doc,
                    "relevance_score": 1.0 - (results["distances"][0][i] if "distances" in results else 0),
                })
        return {
            "query": description,
            "matches": matches,
            "hint": "Use get_code(file_path, symbol) to read full source.",
        }
    except Exception as e:
        return {"error": f"Search failed: {e}"}

def write_session_log(
    session_id: str,
    task: str,
    task_type: str,
    files_changed: list[str],
    decisions: list,
    phase: str | int | None = None,
    next_action: str | None = None,
    agents_invoked: list[str] | None = None,
    tests_run: list[str] | None = None,
    tests_passed: bool | None = None,
    build_clean: bool | None = None,
    changeset_id: str | None = None,
) -> dict[str, str]:
    """Write a structured session log to SQLite Memory."""
    db = _get_db()

    # Build a summary from the task info
    summary = f"[{task_type}] {task}"

    # Normalize decisions: the MCP schema sends list[str], but SQLite
    # log_session() expects list[dict] with {decision, file_path, context}.
    normalized = []
    if decisions:
        for d in decisions:
            if isinstance(d, str):
                normalized.append({"decision": d, "file_path": None, "context": task_type})
            elif isinstance(d, dict):
                normalized.append(d)

    # Add file change records as decisions for traceability
    for f in (files_changed or []):
        normalized.append({
            "decision": f"Modified file: {f}",
            "file_path": f,
            "context": changeset_id or task_type,
        })

    db.log_session(session_id, summary, str(phase) if phase else "", normalized)
    db.close()
    return {"status": f"Session {session_id} logged to SQLite Memory."}

def search_decisions(query: str, limit: int = 10, session_id: str | None = None) -> dict[str, Any]:
    db = _get_db()
    results = db.search_decisions(query, limit, session_id)
    db.close()
    
    return {
        "query": query,
        "results": results,
        "hint": "Use these past decisions to avoid repeating mistakes."
    }

def get_history(file_path: str) -> dict[str, Any]:
    db = _get_db()
    sql = '''
        SELECT d.decision, d.context, s.summary, d.created_at, d.session_id
        FROM decisions d
        JOIN sessions s ON d.session_id = s.session_id
        WHERE d.file_path = ? OR s.summary LIKE ?
        ORDER BY d.created_at DESC
    '''
    cur = db.conn.execute(sql, (file_path, f'%{file_path}%'))
    results = [dict(r) for r in cur.fetchall()]
    db.close()

    return {
        "file_path": file_path,
        "history": results,
    }

def refresh_index(file_paths: list[str]) -> dict:
    from indexer.index_codebase import cmd_incremental
    cmd_incremental(quiet=True)
    return {"status": f"Index refreshed for {len(file_paths)} files."}
