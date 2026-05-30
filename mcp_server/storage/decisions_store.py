"""
decisions_store.py — high-level facade over decisions.jsonl + manifest + FTS5.

This is the module the MCP tools call. It hides the wire format and
the multiple write targets (decisions.jsonl + manifest.yaml + FTS5
index) behind a clean "decisions API."

API:

  record(decision, ...)         → new id  (single)
  record_many(records)          → list of new ids
  get(id)                       → full decision dict (with amendments
                                  applied) or None
  list_all(filters)             → {count, total, has_more, decisions}
  search(query, limit)          → ranked digest records with snippets
  list_tags_with_counts()       → {tags: [{tag, count}, ...]}
  mark_protected(id)            → bool
  supersede(old_id, new_text)   → {success, old_id, new_id, reason}
  rebuild_indexes()             → manifest + digest + FTS5 full rebuild
                                  (called by `codevira sync`)

Append-only contract: every write APPENDS to decisions.jsonl. "Mutations"
(mark_protected, supersede) append AMENDMENT lines that reference the
original by id. The read path merges amendments in order so callers
always see the current state.

Failure-mode policy (P9 — never block user write on cache failure):

- Append to decisions.jsonl FIRST (the canonical store).
- Then update manifest + FTS5 incrementally.
- If manifest/FTS5 update fails (disk full, lock contention), log a
  warning but return success. The user's decision is persisted.
- A subsequent ``rebuild_indexes()`` (or ``codevira sync``) reconciles.
"""

from __future__ import annotations

import fnmatch
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import (
    digest,
    fts5_index,
    jsonl_store,
    manifest,
    origin,
    paths,
    sanitize,
)

logger = logging.getLogger(__name__)


def default_session_id() -> str:
    """Generate a unique ad-hoc session id when the caller didn't supply one.

    v3.0.1 fix: prior to this, an unattributed ``record_decision`` /
    ``write_session_log`` defaulted to the LITERAL string ``"ad-hoc"``.
    Every concurrent IDE (Claude Code, Cursor, Windsurf, Antigravity)
    that didn't pass a slug collided into the same bucket — masking
    session boundaries and breaking the v3.1.0 working-memory design
    (which keys observations by session_id). Generating a unique
    suffix per call disambiguates without forcing every caller to
    invent a name.
    """
    return f"ad-hoc-{secrets.token_hex(3)}"


# ─── Internal: merge amendments into base records ─────────────────────


def _read_merged() -> list[dict[str, Any]]:
    """Read decisions.jsonl + fold amendment lines into their base records.

    Thin wrapper around the v3.0.1 shared primitive
    ``jsonl_store.read_merged`` so working/skills/activity/reflections
    stores can reuse the SAME amendment-overlay semantics without
    copying the merge dance. Behavior preserved exactly: amendment
    records carry the same ``id`` as the base plus a truthy
    ``_amendment_to_id`` marker; later amendments win; orphan
    amendments emit as their own record for diagnosis.
    """
    return jsonl_store.read_merged(paths.decisions_path())


def get(decision_id: str) -> dict[str, Any] | None:
    """Return the merged record for ``decision_id``, or None if not found."""
    for rec in _read_merged():
        if str(rec.get("id")) == decision_id:
            return rec
    return None


# ─── Writes ──────────────────────────────────────────────────────────


def record(
    decision: str,
    *,
    file_path: str | None = None,
    context: str | None = None,
    do_not_revert: bool = False,
    session_id: str | None = None,
    tags: list[str] | None = None,
    alternatives_considered: list[str] | None = None,
    would_re_examine_if: str | None = None,
) -> str:
    """Append one decision; return the generated ID.

    v3.1.x adds counter-decision discipline:

    - ``alternatives_considered`` (list[str]): the strongest alternatives
      we rejected. Surfaces the *losers* of the decision so future
      sessions can see what was considered and weigh whether to revisit.
      Empty list / None is allowed (legacy decisions, trivial choices).
    - ``would_re_examine_if`` (str): the condition that should trigger
      a re-examination of this decision. Especially valuable when paired
      with ``do_not_revert`` — turns the one-way ratchet into an active
      precondition. Example: "if PyPI package size exceeds 5 MB" or
      "if a user reports a leaked secret in a committed memory file".

    Side effects: incrementally updates manifest.yaml + FTS5 cache.
    """
    paths.ensure_dirs()

    # Normalize tags: lowercase, strip whitespace, dedup, sort
    # (lowercase normalization is the same rule manifest.incremental_add
    # applies; doing it here keeps the on-disk record clean too).
    norm_tags = sorted({str(t).strip().lower() for t in (tags or []) if str(t).strip()})

    # v3.1.x: scrub api-key / Bearer / password / AWS AKIA / long hex /
    # long base64 BEFORE persisting. A user prompt or stack trace pasted
    # into a decision text/context could otherwise commit a secret into
    # decisions.jsonl, the FTS5 index, the digest, and AGENTS.md.
    sanitized_decision = sanitize.scrub_sensitive(decision.strip())
    sanitized_context = sanitize.scrub_sensitive(context) if context else context

    # v3.1.x: alternatives are sanitized too (a "rejected option" string
    # can carry a secret just like the chosen decision can).
    sanitized_alternatives = [
        sanitize.scrub_sensitive(str(a).strip())
        for a in (alternatives_considered or [])
        if str(a).strip()
    ]
    sanitized_re_examine = (
        sanitize.scrub_sensitive(would_re_examine_if.strip())
        if would_re_examine_if and would_re_examine_if.strip()
        else None
    )

    base_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or default_session_id(),
        "file_path": file_path,
        "decision": sanitized_decision,
        "context": sanitized_context,
        "do_not_revert": bool(do_not_revert),
        "tags": norm_tags,
        "supersedes": None,
        "superseded_by": None,
        "outcome": None,
        # v3.1.x: counter-decision discipline. Surfaces the rejected
        # alternatives + the trigger that should force a re-examination.
        # Empty/None tolerated on read (legacy v3.0.x records).
        "alternatives_considered": sanitized_alternatives,
        "would_re_examine_if": sanitized_re_examine,
        # v3.1.0 M1: provenance tagging. Optional in reads (v3.0.x
        # records have no origin; readers treat as ide="unknown").
        "origin": origin.current_origin(),
    }

    decision_id = jsonl_store.append_with_generated_id(
        paths.decisions_path(), base_record
    )
    base_record["id"] = decision_id

    # Best-effort manifest + FTS5 + digest update (P9: never fail the write).
    try:
        manifest.incremental_add(paths.manifest_path(), base_record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.record: manifest update failed: %s", exc)

    try:
        fts5_index.add_decision(paths.fts5_path(), base_record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.record: FTS5 update failed: %s", exc)

    try:
        # Incrementally append one digest entry so RelevanceInject can
        # surface the summary without waiting for `codevira sync`.
        digest_rec = digest.digest_record(base_record)
        jsonl_store.append(paths.digest_path(), digest_rec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.record: digest update failed: %s", exc)

    # v3.1.0 M4: a decision tied to a file is a high-signal "attention"
    # event. Mirror it into the activity log so spatial_heat surfaces
    # the file. Best-effort (P9 — the decision is already persisted).
    _activity_origin = base_record["origin"]
    _activity_session = base_record["session_id"]
    if file_path:
        try:
            from mcp_server.storage import activity_store

            activity_store.add(
                str(file_path),
                kind=activity_store.KIND_DECISION_REF,
                session_id=str(_activity_session) if _activity_session else None,
                origin_override=_activity_origin
                if isinstance(_activity_origin, dict)
                else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("decisions_store.record: activity add failed: %s", exc)

    # Phase D — regenerate AGENTS.md so other AI tools (Copilot, Codex,
    # Cursor, Gemini, Factory, Amp, Windsurf, Zed, RooCode, Jules) see
    # the new decision on their next prompt. Best-effort (P9).
    _sync_agents_md_best_effort()

    return decision_id


def record_many(
    records: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Append many decisions. Returns ``(ids, errors)``.

    Each input record may have: decision (required), file_path, context,
    do_not_revert, session_id, tags. Bad records (missing decision) are
    skipped + reported in errors; the others still get written.
    """
    paths.ensure_dirs()

    ids: list[str] = []
    errors: list[dict[str, Any]] = []
    valid_records: list[dict[str, Any]] = []

    for i, r in enumerate(records):
        text = r.get("decision")
        if not text or not isinstance(text, str):
            errors.append({"index": i, "error": "decision must be a non-empty string"})
            continue

        norm_tags = sorted(
            {str(t).strip().lower() for t in (r.get("tags") or []) if str(t).strip()}
        )
        valid_records.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": r.get("session_id") or default_session_id(),
                "file_path": r.get("file_path"),
                "decision": text.strip(),
                "context": r.get("context"),
                "do_not_revert": bool(r.get("do_not_revert", False)),
                "tags": norm_tags,
                "supersedes": None,
                "superseded_by": None,
                "outcome": None,
                # v3.1.0 M1: provenance tagging (see record() above).
                "origin": origin.current_origin(),
            }
        )

    # Use individual append_with_generated_id calls so each gets an ID
    # (append_many doesn't auto-assign IDs).
    for rec in valid_records:
        did = jsonl_store.append_with_generated_id(paths.decisions_path(), rec)
        rec["id"] = did
        ids.append(did)
        try:
            manifest.incremental_add(paths.manifest_path(), rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions_store.record_many: manifest update failed for %s: %s",
                did,
                exc,
            )
        try:
            fts5_index.add_decision(paths.fts5_path(), rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions_store.record_many: FTS5 update failed for %s: %s",
                did,
                exc,
            )
        try:
            digest_rec = digest.digest_record(rec)
            jsonl_store.append(paths.digest_path(), digest_rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions_store.record_many: digest update failed for %s: %s",
                did,
                exc,
            )

    # Phase D — single AGENTS.md regen for the whole batch (cheap; ~10ms).
    if ids:
        _sync_agents_md_best_effort()

    return ids, errors


# ─── Reads ───────────────────────────────────────────────────────────


def list_all(
    *,
    limit: int = 20,
    since: str | None = None,
    file_pattern: str | None = None,
    protected_only: bool = False,
    session_id: str | None = None,
    tags: list[str] | None = None,
    include_superseded: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """Filter + paginate decisions. Filters are AND-combined.

    ``tags`` filter is intersection: a decision matches only if it has
    ALL the requested tags.
    """
    merged = _read_merged()
    filtered: list[dict[str, Any]] = []

    norm_tags_filter = (
        {str(t).strip().lower() for t in tags if str(t).strip()} if tags else None
    )

    for d in merged:
        if not include_superseded and (
            d.get("is_superseded") or d.get("superseded_by")
        ):
            continue
        if protected_only and not d.get("do_not_revert"):
            continue
        if session_id is not None and d.get("session_id") != session_id:
            continue
        if since and (d.get("ts") or "") < since:
            continue
        if file_pattern:
            fp = d.get("file_path") or ""
            if not fnmatch.fnmatch(fp, file_pattern):
                continue
        if norm_tags_filter is not None:
            d_tags = {str(t).lower() for t in (d.get("tags") or [])}
            if not norm_tags_filter.issubset(d_tags):
                continue
        filtered.append(d)

    # Most recent first
    filtered.sort(key=lambda d: d.get("ts") or "", reverse=True)
    total = len(filtered)
    paged = filtered[:limit]

    if full:
        decisions = paged
    else:
        # Slim shape — preserve the v2.1.2 contract: id, decision, file_path,
        # do_not_revert, tags, created_at.
        decisions = [
            {
                "id": d.get("id"),
                "decision": _truncate(d.get("decision") or "", 200),
                "file_path": d.get("file_path"),
                "do_not_revert": bool(d.get("do_not_revert", False)),
                "tags": d.get("tags") or [],
                "created_at": d.get("ts"),
                "session_id": d.get("session_id"),
            }
            for d in paged
        ]

    return {
        "count": len(paged),
        "total": total,
        "has_more": total > limit,
        "decisions": decisions,
    }


def search(
    query: str, *, limit: int = 5, since: str | None = None
) -> list[dict[str, Any]]:
    """BM25 search via FTS5; returns digest records ranked by relevance.

    ``since`` filter is post-applied (cheap; small result set).
    """
    if not query or not query.strip():
        return []

    # Lazy rebuild if stale.
    if fts5_index.staleness_check(paths.decisions_path(), paths.fts5_path()):
        try:
            fts5_index.rebuild_from_jsonl(paths.decisions_path(), paths.fts5_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions_store.search: FTS5 rebuild failed: %s",
                exc,
            )

    hits = fts5_index.search(paths.fts5_path(), query, limit=limit * 2)
    if not hits:
        return []

    # Load merged decisions; map by id for quick lookup.
    merged = _read_merged()
    by_id = {str(d.get("id")): d for d in merged}

    results: list[dict[str, Any]] = []
    for hit in hits:
        d = by_id.get(hit["decision_id"])
        if d is None:
            continue
        # Skip superseded (defensive — FTS5 already excludes them on
        # rebuild, but if cache is stale we re-filter).
        if d.get("is_superseded") or d.get("superseded_by"):
            continue
        if since and (d.get("ts") or "") < since:
            continue
        result = {
            "id": d.get("id"),
            "decision": d.get("decision"),
            "file_path": d.get("file_path"),
            "do_not_revert": bool(d.get("do_not_revert", False)),
            "tags": d.get("tags") or [],
            "created_at": d.get("ts"),
            "score": hit["score"],
            "snippet": hit.get("snippet"),
            # v3.1.0 M1: pass origin through so check_conflict can
            # surface provenance per candidate. None for v3.0.x records
            # (callers treat absent → ide="unknown").
            "origin": d.get("origin"),
        }
        results.append(result)
        if len(results) >= limit:
            break

    return results


def list_tags_with_counts() -> dict[str, Any]:
    """Read manifest.yaml; return tags sorted by count desc, name asc."""
    m = manifest.load(paths.manifest_path())
    tags_with_counts = [
        {"tag": t, "count": len(ids)} for t, ids in m.get("tags", {}).items()
    ]
    tags_with_counts.sort(key=lambda x: (-x["count"], x["tag"]))
    return {"tags": tags_with_counts, "total_unique": len(tags_with_counts)}


# ─── Mutations (append-as-amendment) ────────────────────────────────


def mark_protected(decision_id: str) -> dict[str, Any]:
    """Flip do_not_revert=True via an amendment line."""
    paths.ensure_dirs()
    if get(decision_id) is None:
        return {"success": False, "error": f"decision {decision_id} not found"}

    amendment = {
        "id": decision_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": decision_id,
        "do_not_revert": True,
    }
    jsonl_store.append(paths.decisions_path(), amendment)
    rebuild_indexes()
    return {"success": True, "decision_id": decision_id, "do_not_revert": True}


def set_flag(
    decision_id: str,
    *,
    do_not_revert: bool | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Lightweight in-place flag/tag updates via an amendment line.

    v3.0 RC-audit follow-up: ``supersede`` is the only general-purpose
    way to change a decision in v2.2/v3.0, but it requires you to
    rewrite the full decision text and a reason — overkill for flipping
    ``do_not_revert`` from True → False or correcting a tag typo.
    ``set_flag`` writes a single amendment record that the read path
    (``_read_merged``) merges into the canonical view.

    Returns ``{success, decision_id, updates}`` where ``updates`` is the
    dict of fields actually changed. No-op (returns ``updates={}``) if
    the caller passed no field updates.
    """
    paths.ensure_dirs()
    existing = get(decision_id)
    if existing is None:
        return {"success": False, "error": f"decision {decision_id} not found"}

    updates: dict[str, Any] = {}
    if do_not_revert is not None:
        updates["do_not_revert"] = bool(do_not_revert)
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return {"success": False, "error": "tags must be a list[str]"}
        updates["tags"] = tags

    if not updates:
        return {"success": True, "decision_id": decision_id, "updates": {}}

    amendment = {
        "id": decision_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": decision_id,
        **updates,
    }
    jsonl_store.append(paths.decisions_path(), amendment)
    rebuild_indexes()
    return {"success": True, "decision_id": decision_id, "updates": updates}


def supersede(
    old_id: str,
    new_decision: str,
    reason: str,
    *,
    file_path: str | None = None,
    do_not_revert: bool = False,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Append a new decision; amend the old to is_superseded=True.

    v3.0.0 (2026-05-22 round-2 G5 fix): when ``file_path`` is None,
    INHERIT from the superseded decision. Pre-fix, the new decision
    would land with file_path=None — silently detaching it from the
    file the old decision was protecting. The DecisionLock policy
    then couldn't fire on edits to that file (it filters by
    file_path). Same inheritance for ``tags``. Callers wanting to
    explicitly detach can still pass ``file_path=""`` — only ``None``
    triggers the inheritance.
    """
    paths.ensure_dirs()
    old = get(old_id)
    if old is None:
        return {"success": False, "error": f"decision {old_id} not found"}

    effective_file_path = file_path if file_path is not None else old.get("file_path")
    effective_tags = tags if tags is not None else old.get("tags")

    new_id = record(
        decision=new_decision,
        file_path=effective_file_path,
        context=f"[supersedes {old_id}: {reason}]",
        do_not_revert=do_not_revert,
        tags=effective_tags,
    )
    amendment = {
        "id": old_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": old_id,
        "is_superseded": True,
        "superseded_by": new_id,
    }
    jsonl_store.append(paths.decisions_path(), amendment)
    rebuild_indexes()
    return {
        "success": True,
        "old_id": old_id,
        "new_id": new_id,
        "reason": reason,
    }


# ─── Index maintenance ──────────────────────────────────────────────


def rebuild_indexes() -> None:
    """Full rebuild of manifest + digest + FTS5 from decisions.jsonl.

    Called after amendments (mark_protected, supersede) and on
    ``codevira sync``. Also triggers an AGENTS.md regen so the slim
    contract reflects the new state.
    """
    paths.ensure_dirs()
    try:
        manifest.regenerate(paths.decisions_path(), paths.manifest_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.rebuild_indexes: manifest failed: %s", exc)
    try:
        digest.regenerate(paths.decisions_path(), paths.digest_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.rebuild_indexes: digest failed: %s", exc)
    try:
        fts5_index.rebuild_from_jsonl(paths.decisions_path(), paths.fts5_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.rebuild_indexes: FTS5 failed: %s", exc)
    _sync_agents_md_best_effort()


def _sync_agents_md_best_effort() -> None:
    """Regenerate AGENTS.md from current decisions. Never raises (P9)."""
    try:
        from mcp_server.storage import agents_md_generator

        agents_md_generator.regenerate()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "decisions_store._sync_agents_md_best_effort: AGENTS.md regen failed: %s",
            exc,
        )


# ─── Helpers ────────────────────────────────────────────────────────


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"
