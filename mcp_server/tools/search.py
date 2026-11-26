from __future__ import annotations

import re
from typing import Any

# 4.0.1: removed this module's own `_get_db()` + the SQLiteGraph/get_data_dir
# imports that fed it. Every tool in this file goes through
# storage.decisions_store (FTS5); nothing here has touched graph.db since
# search_codebase was cut in v2.2.0. It survived review because `_get_db` is
# also defined in tools/graph.py and tools/learning.py, where it IS live — a
# grep for the name always found callers, just never this copy's.

# v2.2.0: removed _chroma_cache, _get_chroma_client, prewarm_embedding_model,
# search_codebase, _structural_fallback. Decision search lives in
# mcp_server/storage/decisions_store.py (FTS5 backend); see the four
# delegate functions below.


# v2.2.0+ (2026-05-22 surface-cut audit batch 6): write_session_logs
# (batch endpoint) deleted. Agents called single-record write_session_log
# in practice; the batch saved round-trips that never happened in real
# data. Use write_session_log directly.


#: The fields a decision inside a session log may carry, besides ``id``.
_DECISION_FIELDS = (
    "file_path",
    "symbol",
    "context",
    "do_not_revert",
    "tags",
    "alternatives_considered",
    "would_re_examine_if",
)

# A real id always has a digit; "Deprecated" is a decision, not an id.
_DECISION_ID_RE = re.compile(r"D(?=[0-9A-Za-z]*[0-9])[0-9A-Za-z]{6,}")

_TRUE_WORDS = frozenset({"true", "yes", "1", "on"})


def _as_list(value: Any) -> list[Any]:
    """A caller's lone value is a list of one, never a list of its characters."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _conflict_warning(decision_id: str, text: str, file_path: str | None) -> str | None:
    """Name the locked decisions a newly recorded one may contradict."""
    from mcp_server.tools.check_conflict import check_conflict

    hits = [
        str(c.get("decision_id"))
        for c in check_conflict(text, file_path=file_path)["conflicts"]
        if c.get("decision_id") != decision_id
    ]
    if not hits:
        return None
    return (
        f"{decision_id} may conflict with {', '.join(hits)}. Both are kept; use "
        f"supersede_decision(old_id, new_decision, reason) to retire one."
    )


def _carry_into_repeat(did: str, item: dict[str, Any], lock: bool) -> list[str]:
    """Apply a lock asked for on a repeated decision; name what else was not.

    A repeat links to the decision it repeats instead of recording a twin, so
    anything new it carries would otherwise go nowhere searchable.
    """
    from mcp_server.storage import decisions_store

    notes: list[str] = []
    existing = decisions_store.get(did) or {}
    if lock and not existing.get("do_not_revert"):
        decisions_store.mark_protected(did)
        notes.append(f"{did} repeats an existing decision; locked it as asked.")
    extra = [
        k
        for k in _DECISION_FIELDS
        if k not in ("file_path", "do_not_revert")
        and item.get(k)
        and item.get(k) != existing.get(k)
    ]
    if extra:
        notes.append(
            f"{did} repeats an existing decision; its new {', '.join(extra)} "
            f"is kept in this session log only. Use supersede_decision to change it."
        )
    return notes


def _keep_decision(
    item: Any, session_id: str
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Link or record one decision from a session log.

    Never goes through ``record_decision``: its supersede-on-write would let a
    terse repeat in the log replace the detailed decision it repeats.

    Returns ``(entry to store in the log, decision id, warnings)``.
    """
    from mcp_server.storage import decisions_store

    if isinstance(item, str):
        bare = item.strip()
        item = {"id": bare} if _DECISION_ID_RE.fullmatch(bare) else {"decision": bare}
    if not isinstance(item, dict):
        return None, None, [f"Ignored a decision that is not an object: {item!r}"]

    given = str(item.get("id") or "").strip()
    text = item.get("decision")
    text = text.strip() if isinstance(text, str) else ""
    entry = {k: item[k] for k in ("id", "decision", *_DECISION_FIELDS) if item.get(k)}
    warnings: list[str] = []

    if given:
        if decisions_store.get(given):
            return entry, given, warnings
        warnings.append(f"No decision {given} in this project.")
        if not text:
            return entry, None, warnings
    if not text:
        return None, None, ["Ignored a decision with no `decision` text and no `id`."]

    fields = {k: item.get(k) for k in _DECISION_FIELDS}
    lock = item.get("do_not_revert", False)
    fields["do_not_revert"] = lock is True
    if not isinstance(lock, bool):
        fields["do_not_revert"] = str(lock).strip().lower() in _TRUE_WORDS
        warnings.append(
            f"do_not_revert={lock!r} is not true/false; read it as "
            f"{fields['do_not_revert']}."
        )
    fields["tags"] = _as_list(fields["tags"])
    fields["alternatives_considered"] = (
        _as_list(fields["alternatives_considered"]) or None
    )
    path = fields["file_path"]
    try:
        did = decisions_store.find_restatement(text, path)
        if did is not None:
            warnings.extend(_carry_into_repeat(did, item, fields["do_not_revert"]))
        else:
            did = decisions_store.record(text, session_id=session_id, **fields)
            clash = _conflict_warning(did, text, path)
            if clash:
                warnings.append(clash)
    except Exception as exc:  # noqa: BLE001 — the log must still be written
        warnings.append(
            f"Could not record the decision {text[:60]!r} ({exc}). Its text is kept "
            f"in this session log; call record_decision to make it searchable."
        )
        return entry, None, warnings
    return {**entry, "id": did}, did, warnings


def write_session_log(
    session_id: str,
    task: str,
    phase: str,
    files_changed: list[str],
    decisions: list[dict],
    next_steps: list[str],
    task_type: str | None = None,
    skill_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Write a structured session log (appends to sessions.jsonl).

    Everything passed in is stored. Each decision is also made searchable: an
    ``id`` links an existing one, a repeat of an active decision links to it
    (see ``decisions_store.find_restatement``), anything else is recorded.

    Multiple logs for one ``session_id`` coexist as an audit trail.

    ``task_type`` / ``skill_ids`` (4.0 Step 2.4): ``sessions_store.write``
    has accepted both since v3.1.x, but neither this signature nor the MCP
    inputSchema exposed them — so every session on record carries
    ``task_type: None``, and ``cli_induce._build_proposals`` filters on
    exactly that field. Skill induction could therefore never fire, no
    matter how much the project was used. Both default to None so existing
    callers are unaffected.

    Returns:
        ``{status, session_id, requested_session_id, collision_resolved,
        decision_ids, stored: {files_changed, decisions, next_steps},
        [warnings]}``.
    """
    from mcp_server.storage import sessions_store

    kept: list[dict[str, Any]] = []
    decision_ids: list[str] = []
    warnings: list[str] = []
    for item in _as_list(decisions):
        entry, did, notes = _keep_decision(item, session_id)
        warnings.extend(notes)
        if entry is not None:
            kept.append(entry)
        if did is not None and did not in decision_ids:
            decision_ids.append(did)

    files = [str(f) for f in _as_list(files_changed)]
    steps = [str(n) for n in _as_list(next_steps)]
    sessions_store.write(
        session_id=session_id,
        task=task,
        phase=phase,
        summary=None,
        decision_ids=decision_ids,
        decisions=kept,
        files_changed=files,
        next_steps=steps,
        task_type=task_type,
        skill_ids=skill_ids,
    )

    response: dict[str, Any] = {
        "status": f"Session {session_id} logged to .codevira/sessions.jsonl.",
        "session_id": session_id,
        "requested_session_id": session_id,
        "collision_resolved": False,
        "decision_ids": decision_ids,
        "stored": {
            "files_changed": len(files),
            "decisions": len(kept),
            "next_steps": len(steps),
        },
    }
    if warnings:
        response["warnings"] = warnings
    return response


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
            # 4.0 Step 6: `score_breakdown` is deliberately NOT in the
            # compact row. Measured at +122 tokens on a 5-row payload
            # (449 -> 571, +27%) — a real cost on EVERY search for an aid
            # needed rarely, and E1 (D0000ZQ) chose token discipline for
            # the default. It ships under full=True, the documented
            # escape hatch, alongside snippet and origin.
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
