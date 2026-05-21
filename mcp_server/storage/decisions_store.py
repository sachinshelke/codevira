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
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import digest, fts5_index, jsonl_store, manifest, paths

logger = logging.getLogger(__name__)


# ─── Internal: merge amendments into base records ─────────────────────


def _read_merged() -> list[dict[str, Any]]:
    """Read decisions.jsonl + fold amendment lines into their base records.

    Amendment lines have ``_amendment_to_id`` matching an original id;
    their fields overlay the original (later amendments win over earlier).
    Original records are emitted in their original order.
    """
    raw = jsonl_store.read_all(paths.decisions_path())
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []  # preserve insertion order of base records

    for rec in raw:
        did = str(rec.get("id", ""))
        if not did:
            continue
        if rec.get("_amendment_to_id"):
            # Overlay onto existing base.
            base = by_id.get(did)
            if base is None:
                # Amendment without a base — shouldn't happen, but
                # don't crash; surface it as its own record so the
                # user can diagnose.
                by_id[did] = dict(rec)
                order.append(did)
            else:
                base.update({k: v for k, v in rec.items() if not k.startswith("_")})
        else:
            if did not in by_id:
                order.append(did)
            by_id[did] = dict(rec)

    return [by_id[did] for did in order]


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
) -> str:
    """Append one decision; return the generated ID.

    Side effects: incrementally updates manifest.yaml + FTS5 cache.
    """
    paths.ensure_dirs()

    # Normalize tags: lowercase, strip whitespace, dedup, sort
    # (lowercase normalization is the same rule manifest.incremental_add
    # applies; doing it here keeps the on-disk record clean too).
    norm_tags = sorted({str(t).strip().lower() for t in (tags or []) if str(t).strip()})

    base_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or "ad-hoc",
        "file_path": file_path,
        "decision": decision.strip(),
        "context": context,
        "do_not_revert": bool(do_not_revert),
        "tags": norm_tags,
        "supersedes": None,
        "superseded_by": None,
        "outcome": None,
    }

    decision_id = jsonl_store.append_with_generated_id(
        paths.decisions_path(), base_record
    )
    base_record["id"] = decision_id

    # Best-effort manifest + FTS5 update (P9: never fail the write).
    try:
        manifest.incremental_add(paths.manifest_path(), base_record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.record: manifest update failed: %s", exc)

    try:
        fts5_index.add_decision(paths.fts5_path(), base_record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_store.record: FTS5 update failed: %s", exc)

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
                "session_id": r.get("session_id") or "ad-hoc",
                "file_path": r.get("file_path"),
                "decision": text.strip(),
                "context": r.get("context"),
                "do_not_revert": bool(r.get("do_not_revert", False)),
                "tags": norm_tags,
                "supersedes": None,
                "superseded_by": None,
                "outcome": None,
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


def supersede(
    old_id: str,
    new_decision: str,
    reason: str,
    *,
    file_path: str | None = None,
    do_not_revert: bool = False,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Append a new decision; amend the old to is_superseded=True."""
    paths.ensure_dirs()
    if get(old_id) is None:
        return {"success": False, "error": f"decision {old_id} not found"}

    new_id = record(
        decision=new_decision,
        file_path=file_path,
        context=f"[supersedes {old_id}: {reason}]",
        do_not_revert=do_not_revert,
        tags=tags,
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
