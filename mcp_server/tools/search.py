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
    all_projects: bool = False,
) -> dict[str, Any]:
    """Search past decisions via FTS5 (BM25-ranked).

    v2.2.0: pure FTS5 keyword/stemming search over decisions.jsonl.
    No semantic embedding (chromadb removed). Natural-language queries
    still work via porter stemming + BM25; concept-only queries with
    zero keyword overlap may miss (acceptable trade — see v2.2.0 plan).

    E1 (Phase 19): the DEFAULT is now summary-first — each row is
    {id, decision (one-line ≤140 chars), file_path, do_not_revert, tags,
    score}, dropping the heavy per-row snippet/origin and full text. Use
    ``full=True`` for untruncated rows, ``expand(ids=[...])`` to fetch
    specific decisions in full, or set ``CODEVIRA_DECISION_DETAIL=full`` to
    restore the pre-E1 verbose default machine-wide.

    Args:
        query: search terms; empty string returns 0 results.
        limit: max results (clamped to [1, 20]).
        session_id: filter to a session (post-FTS5 filter).
        full: return full untruncated rows (incl. snippet/origin).
        summary_only: ~70% smaller payload — only {id, summary, score,
            do_not_revert} per result.
        since: ISO 8601 / YYYY-MM-DD; only decisions ts > since returned.
        all_projects: when True (v3.6.0), search EVERY registered project's
            decision store, not just the current one. Each result gains
            ``project`` + ``project_path`` so you can see where it came from.
            Default False (current project only).

    Returns:
        {query, count, retrieval, threshold_used, results, [hint], [_warning]}
    """
    limit = max(1, min(int(limit), 20))

    from mcp_server.storage import decisions_store

    if all_projects:
        results = decisions_store.search_all_projects(
            query or "", limit=limit, since=since
        )
    else:
        results = decisions_store.search(query or "", limit=limit, since=since)

    # Apply session_id filter post-search (FTS5 has no notion of session).
    if session_id:
        results = [r for r in results if r.get("session_id") == session_id]

    # E1 (Phase 19): summary-first default. The verbose pre-E1 default is
    # restorable machine-wide via CODEVIRA_DECISION_DETAIL=full (mirrors the
    # CODEVIRA_TOOL_PROFILE / CODEVIRA_TOKEN_PRECISION env convention).
    # Explicit full=True / summary_only always win over the env.
    import os

    effective_full = full or (
        os.environ.get("CODEVIRA_DECISION_DETAIL", "").strip().lower() == "full"
    )

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
                # v3.6.0: present only on all_projects results.
                **({"project": r["project"]} if r.get("project") else {}),
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

    if effective_full:
        return {
            "query": query,
            "count": len(results),
            "retrieval": retrieval,
            "threshold_used": threshold,
            "results": results,
            "hint": "Showing full untruncated decisions.",
        }

    # Compact default (E1): id + one-line summary + key fields (do_not_revert,
    # file_path, tags) + score. Drops the heavy per-row snippet/origin and the
    # full decision text; expand(ids=[...]) or full=True fetches the rest.
    compact = [
        {
            "id": r.get("id"),
            "decision": decisions_store.one_line_summary(r.get("decision"), 140),
            "file_path": r.get("file_path"),
            "do_not_revert": bool(r.get("do_not_revert", False)),
            "tags": r.get("tags") or [],
            "created_at": r.get("created_at"),
            "score": r.get("score"),
            # v3.6.0: present only on all_projects results.
            **(
                {"project": r["project"], "project_path": r.get("project_path")}
                if r.get("project")
                else {}
            ),
        }
        for r in results
    ]
    return {
        "query": query,
        "count": len(compact),
        "retrieval": retrieval,
        "threshold_used": threshold,
        "results": compact,
        "hint": (
            "Compact rows (E1 summary-first). Pass full=True for untruncated "
            "text + snippet/origin, or expand(ids=[...]) to fetch specific "
            "decisions in full."
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
    ``summary_only`` (tiny) < default compact (one-line decision + key
    fields) < ``full``. E1 (Phase 19): the default decision text is a
    one-line summary (≤140 chars); ``full=True`` or
    ``CODEVIRA_DECISION_DETAIL=full`` returns untruncated records, and
    ``expand(ids=[...])`` fetches specific decisions in full.

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
            do_not_revert}`` per row. v3.0.0 parity with
            ``search_decisions``: the param existed there but was missing
            here, so agents who'd used ``search_decisions(summary_only=True)``
            reasonably assumed it worked on ``list_decisions`` too. Takes
            precedence over ``full`` when both are set.

    Returns ``{count, has_more, decisions, filters_applied, [mode]}``.
    """
    limit = max(1, min(int(limit), 200))

    import os

    from mcp_server.storage import decisions_store

    # E1 (Phase 19): summary-first default; CODEVIRA_DECISION_DETAIL=full
    # restores the pre-E1 verbose default. Explicit full/summary_only win.
    effective_full = full or (
        os.environ.get("CODEVIRA_DECISION_DETAIL", "").strip().lower() == "full"
    )

    result = decisions_store.list_all(
        limit=limit,
        since=since_date,
        file_pattern=file_pattern,
        protected_only=protected_only,
        session_id=session_id,
        tags=tags,
        include_superseded=include_superseded,
        full=effective_full and not summary_only,
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

    if effective_full:
        decisions_out = result["decisions"]
    else:
        # Compact default (E1): one-line the decision text; key fields
        # (id, file_path, do_not_revert, tags, created_at) already present.
        decisions_out = [
            {**d, "decision": decisions_store.one_line_summary(d.get("decision"), 140)}
            for d in result["decisions"]
        ]

    return {
        "count": result["count"],
        "has_more": result["has_more"],
        "decisions": decisions_out,
        "filters_applied": filters_applied,
    }


def expand(ids: list[str]) -> dict[str, Any]:
    """Batch-fetch FULL decision records by ID — the expand path for the
    summary-first defaults (E1, Phase 19).

    The compact ``search_decisions`` / ``list_decisions`` defaults return
    one-line summaries + stable IDs; pass the IDs you care about here to get
    their complete records (full text, context, origin, timestamps). This is
    the token-efficient pattern: scan cheap summaries, expand only the few
    that matter.

    Args:
        ids: decision IDs to fetch (e.g. ["D0000Z4", "D0000WR"]).

    Returns:
        {requested, count, decisions: [full records], not_found: [ids]}.
        Order follows ``ids``; unknown IDs are listed under ``not_found``
        and simply omitted from ``decisions`` (never raises).
    """
    from mcp_server.storage import decisions_store

    id_list = [str(i).strip() for i in (ids or []) if str(i).strip()]
    found: list[dict[str, Any]] = []
    not_found: list[str] = []
    for did in id_list:
        rec = decisions_store.get(did)
        if rec is None:
            not_found.append(did)
        else:
            found.append(rec)

    return {
        "requested": len(id_list),
        "count": len(found),
        "decisions": found,
        "not_found": not_found,
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
