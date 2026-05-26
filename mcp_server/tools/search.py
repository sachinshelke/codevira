from __future__ import annotations

from typing import Any

from mcp_server.paths import get_data_dir
from indexer.sqlite_graph import SQLiteGraph


def _get_db() -> SQLiteGraph:
    db_path = get_data_dir() / "graph" / "graph.db"
    return SQLiteGraph(db_path)


# v2.2.0: removed _chroma_cache, _get_chroma_client, prewarm_embedding_model,
# search_codebase, _structural_fallback. Decision search lives in
# mcp_server/storage/decisions_store.py (FTS5 backend); see the four
# delegate functions below.


# v2.2.0+ (2026-05-22 surface-cut audit batch 6): write_session_logs
# (batch endpoint) deleted. Agents called single-record write_session_log
# in practice; the batch saved round-trips that never happened in real
# data. Use write_session_log directly.


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
    summary_only: bool = False,
) -> dict[str, Any]:
    """Enumerate decisions with optional filters (v2.2.0: from JSONL).

    All filters AND together — only decisions matching every constraint
    are returned. ``tags`` is intersection (all tags must match).

    Three verbosity tiers (mirrors ``search_decisions``):
    ``summary_only`` (tiny) < default slim (~50 tok/row) < ``full``.

    Args:
        limit: max rows (clamped to [1, 200]).
        since_date: ISO 8601 / YYYY-MM-DD; only ts > since_date.
        file_pattern: fnmatch glob on ``file_path`` (e.g. ``"src/*"``).
        protected_only: only do_not_revert=True.
        session_id: single-session filter.
        tags: intersection — match ALL of the supplied tags.
        include_superseded: default False (hide soft-deleted).
        full: full untruncated record vs the default slim shape.
        summary_only: smallest payload — only ``{id, summary (80 chars),
            do_not_revert}`` per row. v3.0.1 parity with
            ``search_decisions``: the param existed there but was missing
            here, so agents who'd used ``search_decisions(summary_only=True)``
            reasonably assumed it worked on ``list_decisions`` too. Takes
            precedence over ``full`` when both are set.

    Returns ``{count, has_more, decisions, filters_applied, [mode]}``.
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
        full=full and not summary_only,
    )

    filters_applied = {
        "since_date": since_date,
        "file_pattern": file_pattern,
        "protected_only": protected_only,
        "session_id": session_id,
        "tags": list(tags) if tags else None,
        "include_superseded": include_superseded,
        "limit": limit,
    }

    if summary_only:
        decisions: list[dict[str, Any]] = [
            {
                "id": d.get("id"),
                "summary": (d.get("decision") or "")[:80],
                "do_not_revert": bool(d.get("do_not_revert", False)),
            }
            for d in result["decisions"]
        ]
        return {
            "count": result["count"],
            "has_more": result["has_more"],
            "decisions": decisions,
            "mode": "summary_only",
            "filters_applied": filters_applied,
        }

    return {
        "count": result["count"],
        "has_more": result["has_more"],
        "decisions": result["decisions"],
        "filters_applied": filters_applied,
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


# v2.2.0+ (2026-05-22 surface-cut audit batch 6): refresh_index
# deleted. It existed to refresh the chromadb semantic index that no
# longer exists. Callers wanting the code graph refreshed should call
# `refresh_graph(file_paths=...)` directly (separate MCP tool).
