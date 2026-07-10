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
from pathlib import Path
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
    symbol: str | None = None,
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

    # v3.6.0: optional symbol scope for region-level decision locking. A bare
    # identifier (function / class name) — strip + cap; None when blank. Not a
    # free-text field, so no secret-scrub needed, but bound the length so a
    # malformed value can't bloat the record / AGENTS.md.
    norm_symbol = symbol.strip()[:200] if symbol and symbol.strip() else None

    base_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or default_session_id(),
        "file_path": file_path,
        "symbol": norm_symbol,
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
    include_outdated: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """Filter + paginate decisions. Filters are AND-combined.

    ``tags`` filter is intersection: a decision matches only if it has
    ALL the requested tags.

    ``include_outdated`` (v3.7.0): outdated-tombstoned decisions
    (``mark_outdated``) are hidden by default so stale memory stops
    surfacing; pass True to include them.
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
        if not include_outdated and d.get("is_outdated"):
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
                # v3.7.0: expose outcome so freshness-ranking callers
                # (get_session_context) can down-rank reverted decisions.
                "outcome": d.get("outcome"),
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
    query: str,
    *,
    limit: int = 5,
    since: str | None = None,
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    """BM25 search via FTS5; returns digest records ranked by relevance.

    ``since`` filter is post-applied (cheap; small result set).

    ``project_root`` (v3.6.0) selects which project to search. ``None`` (the
    default) targets the current project — behavior is byte-for-byte unchanged.
    A non-None root searches THAT project's ``.codevira/`` store instead, which
    is how ``search_all_projects`` reuses this function per registered project.
    """
    if not query or not query.strip():
        return []

    decisions_p = paths.decisions_path(project_root)
    fts5_p = paths.fts5_path(project_root)

    # Lazy rebuild if stale.
    if fts5_index.staleness_check(decisions_p, fts5_p):
        try:
            fts5_index.rebuild_from_jsonl(decisions_p, fts5_p)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions_store.search: FTS5 rebuild failed: %s",
                exc,
            )

    hits = fts5_index.search(fts5_p, query, limit=limit * 2)
    if not hits:
        return []

    # Load merged decisions; map by id for quick lookup.
    merged = jsonl_store.read_merged(decisions_p)
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
        # Skip outdated tombstones (v3.7.0) so stale memory stops surfacing.
        if d.get("is_outdated"):
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
            # v3.7.0: expose outcome for freshness-ranking (reverted down-rank).
            "outcome": d.get("outcome"),
            # v3.1.0 M1: pass origin through so check_conflict can
            # surface provenance per candidate. None for v3.0.x records
            # (callers treat absent → ide="unknown").
            "origin": d.get("origin"),
        }
        results.append(result)
        if len(results) >= limit:
            break

    return results


#: Cap on how many projects a single cross-project search will touch. Each
#: project is a SQLite open + (possibly) an FTS rebuild + a search, run
#: sequentially, so an unbounded fan-out over a machine with hundreds of
#: registered repos would be slow. Bounds latency; logged when it bites.
_MAX_CROSS_PROJECTS = 50


def search_all_projects(
    query: str, *, limit: int = 5, since: str | None = None
) -> list[dict[str, Any]]:
    """Cross-project decision search (v3.6.0): BM25 search every registered
    project's ``.codevira/`` decision store and return the top matches, each
    tagged with the project it came from.

    Reuses ``search`` per project (so per-project ranking, staleness rebuild,
    and superseded filtering are identical to single-project search). Skipped:
    projects whose decision file is gone (moved / ghost / never recorded) and
    invalid roots ($HOME / system dirs). One project erroring out never sinks
    the whole search; at most ``_MAX_CROSS_PROJECTS`` projects are touched.

    Each returned row is the normal ``search`` row plus:
      * ``project``      — the project's name (falls back to its dir name)
      * ``project_path`` — the resolved project root on disk

    **Merge ranking.** BM25 ``score`` is a per-index distance — it is NOT
    comparable across projects (it depends on each corpus's term/length
    statistics), so we do NOT sort the union by raw score (that would let a
    weak match in a term-rare repo outrank a strong one elsewhere). Instead we
    interleave by per-project RANK: every project's #1 first (ties broken by
    raw score), then every #2, and so on. This is corpus-independent and gives
    each project — including the current one — fair representation in the top-N.
    The raw ``score`` is still returned for reference.
    """
    if not query or not query.strip():
        return []
    try:
        from mcp_server._project_inventory import enumerate_projects
        from mcp_server.paths import get_project_root, is_invalid_project_root
    except Exception:  # noqa: BLE001 — inventory unavailable → no cross-project
        return []

    def _resolve_key(root: Path) -> str:
        """Canonical dedup key. ``get_project_root`` always returns a
        ``.resolve()``-d path, but a registered ``canonical_path`` may be the
        raw string stored at registration (e.g. an unresolved ``/var`` symlink
        on macOS). Resolve both so the same dir can't be searched twice."""
        try:
            return str(root.resolve())
        except OSError:
            return str(root)

    seen_roots: set[str] = set()
    merged_hits: list[dict[str, Any]] = []

    def _search_one(root: Path, name: str | None) -> None:
        if len(seen_roots) >= _MAX_CROSS_PROJECTS:
            return
        key = _resolve_key(root)
        if key in seen_roots:
            return  # a project can surface via the slug dir, db row, AND cwd
        seen_roots.add(key)
        if is_invalid_project_root(root):
            return  # $HOME / system dir / otherwise unsafe root — never search
        resolved = Path(key)
        if not paths.decisions_path(resolved).is_file():
            return  # ghost / moved / never recorded a decision here
        try:
            hits = search(query, limit=limit, since=since, project_root=resolved)
        except Exception:  # noqa: BLE001 — isolate a bad project's failure
            return
        proj_name = name or resolved.name
        for rank, h in enumerate(hits):
            merged_hits.append(
                {**h, "project": proj_name, "project_path": key, "_rank": rank}
            )

    enumerated = 0
    for entry in enumerate_projects():
        root_str = entry.canonical_path
        if not root_str:
            continue  # slug-only entry with no resolvable on-disk path
        _search_one(Path(root_str), entry.name)
        enumerated += 1
        if enumerated >= _MAX_CROSS_PROJECTS:
            logger.warning(
                "search_all_projects: capped at %d projects; some not searched",
                _MAX_CROSS_PROJECTS,
            )
            break

    # Always include the CURRENT project, even if it isn't registered yet
    # (used only via CLI, or just initialised). No-op (dedup) if already seen.
    try:
        _search_one(get_project_root(), None)
    except Exception:  # noqa: BLE001 — cwd not a project / unresolvable → skip
        pass

    # Interleave by per-project rank (corpus-independent); raw score only
    # tie-breaks within the same rank. Strip the internal _rank before return.
    merged_hits.sort(key=lambda r: (r.get("_rank", 0), r.get("score", 0.0)))
    out = merged_hits[:limit]
    for r in out:
        r.pop("_rank", None)
    return out


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


def reaffirm(decision_id: str) -> dict[str, Any]:
    """v3.2.0: refresh a ``do_not_revert`` decision's soft-expire clock.

    Appends an amendment with ``reaffirmed_at: <now>``. Consumers (and
    :func:`compute_dnr_soft_expire`) treat this as the new effective
    age of the lock — so callers can keep a long-standing decision
    "live" without rewriting it.

    No-op semantics: the function always appends an amendment, even if
    the decision has been reaffirmed recently. Callers wanting "only if
    expired" semantics check :func:`compute_dnr_soft_expire` first.

    Returns ``{success, decision_id, reaffirmed_at}`` on success;
    ``{success: False, error}`` if the decision doesn't exist.
    """
    paths.ensure_dirs()
    if get(decision_id) is None:
        return {"success": False, "error": f"decision {decision_id} not found"}

    now_iso = datetime.now(timezone.utc).isoformat()
    amendment = {
        "id": decision_id,
        "ts": now_iso,
        "_amendment_to_id": decision_id,
        "reaffirmed_at": now_iso,
    }
    jsonl_store.append(paths.decisions_path(), amendment)
    rebuild_indexes()
    return {
        "success": True,
        "decision_id": decision_id,
        "reaffirmed_at": now_iso,
    }


# v3.2.0: do_not_revert soft-expire.
#
# Long-lived `do_not_revert` decisions can grow stale — the world that
# made them right may have changed. v3.2.0 introduces a SOFT expiry: a
# decision still loads as locked, but readers can see it's overdue for
# a check via the {dnr_soft_expired, dnr_age_days} fields surfaced by
# search/list. The lock itself does NOT auto-flip; the user (or a
# future engine policy) decides what to do.
#
# Default threshold: 180 days (~6 months). Override per process via
# CODEVIRA_DNR_SOFT_EXPIRE_DAYS. Set to 0 to disable (always live).

_DEFAULT_DNR_SOFT_EXPIRE_DAYS = 180


def _parse_iso_to_dt(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _dnr_effective_dt(decision: dict[str, Any]) -> datetime | None:
    """The reference time for measuring a do_not_revert decision's age.

    Picks the LATEST of ``reaffirmed_at`` and ``ts`` (the original write).
    Returns None when neither is parseable.
    """
    candidates = [
        _parse_iso_to_dt(decision.get("reaffirmed_at")),
        _parse_iso_to_dt(decision.get("ts")),
    ]
    valid = [d for d in candidates if d is not None]
    if not valid:
        return None
    return max(valid)


def dnr_soft_expire_days() -> int:
    """Active threshold (in days). Reads CODEVIRA_DNR_SOFT_EXPIRE_DAYS env
    var; falls back to the default. 0 means disabled."""
    import os

    raw = os.environ.get("CODEVIRA_DNR_SOFT_EXPIRE_DAYS")
    if raw is None or not raw.strip():
        return _DEFAULT_DNR_SOFT_EXPIRE_DAYS
    try:
        v = int(raw.strip())
        return v if v >= 0 else _DEFAULT_DNR_SOFT_EXPIRE_DAYS
    except ValueError:
        return _DEFAULT_DNR_SOFT_EXPIRE_DAYS


def compute_dnr_soft_expire(
    decision: dict[str, Any],
    *,
    max_age_days: int | None = None,
) -> dict[str, Any]:
    """Compute soft-expire status for one merged decision dict.

    Returns ``{soft_expired, age_days, max_age_days, effective_ts}``.

    - ``soft_expired`` is True iff the decision is protected
      (``do_not_revert``) AND the effective age (max of ``reaffirmed_at``
      and original ``ts``) exceeds ``max_age_days``.
    - When the threshold is 0 (disabled), ``soft_expired`` is always
      False — but ``age_days`` is still computed for observability.
    - A non-protected decision has ``soft_expired=False`` regardless of
      age (no lock to expire).
    """
    threshold = max_age_days if max_age_days is not None else dnr_soft_expire_days()
    eff = _dnr_effective_dt(decision)
    if eff is None:
        return {
            "soft_expired": False,
            "age_days": None,
            "max_age_days": threshold,
            "effective_ts": None,
        }
    now = datetime.now(timezone.utc)
    age_days = max(0, int((now - eff).total_seconds() // 86400))
    is_protected = bool(decision.get("do_not_revert"))
    soft_expired = bool(is_protected and threshold > 0 and age_days > threshold)
    return {
        "soft_expired": soft_expired,
        "age_days": age_days,
        "max_age_days": threshold,
        "effective_ts": eff.isoformat(),
    }


def mark_outdated(
    decision_id: str, *, reason: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Tombstone a decision as *outdated* so it stops surfacing in
    ``list_all`` / ``search`` / ``get_session_context`` — WITHOUT deleting it.

    v3.7.0 staleness read-side. Unlike ``supersede`` there is no
    replacement decision; use this when a decision is simply no longer true
    and has no successor. Reversible: writes an amendment (audit preserved),
    cleared via ``set_flag(is_outdated=False)``.

    A ``do_not_revert`` (protected) decision is NOT retired without
    ``force=True`` — otherwise this would be a silent back door around the
    protection contract (any AI, any IDE, one call, and the decision vanishes
    from every reader). Refusal returns ``{success: False, do_not_revert:
    True}``; the caller should surface the reasoning and pass ``force=True`` or
    use ``supersede_decision`` instead.
    """
    paths.ensure_dirs()
    existing = get(decision_id)
    if existing is None:
        return {"success": False, "error": f"decision {decision_id} not found"}
    if existing.get("do_not_revert") and not force:
        return {
            "success": False,
            "error": (
                f"decision {decision_id} is do_not_revert (protected). Surface "
                f"its reasoning and pass force=True to retire it, or use "
                f"supersede_decision to replace it."
            ),
            "do_not_revert": True,
        }
    now = datetime.now(timezone.utc).isoformat()
    amendment = {
        "id": decision_id,
        "ts": now,
        "_amendment_to_id": decision_id,
        "is_outdated": True,
        "outdated_at": now,
        "outdated_reason": (
            reason.strip()[:500] if reason and reason.strip() else None
        ),
    }
    jsonl_store.append(paths.decisions_path(), amendment)
    rebuild_indexes()
    return {"success": True, "decision_id": decision_id, "is_outdated": True}


def set_flag(
    decision_id: str,
    *,
    do_not_revert: bool | None = None,
    tags: list[str] | None = None,
    is_outdated: bool | None = None,
    force: bool = False,
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
    if is_outdated is not None:
        # Setting the outdated tombstone on a protected decision is the same
        # back door as mark_outdated — gate it behind force. Clearing it
        # (is_outdated=False) is always allowed.
        if bool(is_outdated) and existing.get("do_not_revert") and not force:
            return {
                "success": False,
                "error": (
                    f"decision {decision_id} is do_not_revert (protected); "
                    f"pass force=True to mark it outdated, or use supersede_decision."
                ),
                "do_not_revert": True,
            }
        updates["is_outdated"] = bool(is_outdated)

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
    symbol: str | None = None,
    context: str | None = None,
    alternatives_considered: list[str] | None = None,
    would_re_examine_if: str | None = None,
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

    v3.7.0 (Phase 30 follow-up): ``symbol`` / ``context`` /
    ``alternatives_considered`` / ``would_re_examine_if`` are now threaded onto
    the new decision so supersede-on-write can't silently DROP a region-lock
    scope, the rationale, or the counter-decision fields. ``symbol`` inherits
    from the old decision when None; ``context`` falls back to the
    ``[supersedes …]`` marker only when the caller passes none.
    """
    paths.ensure_dirs()
    old = get(old_id)
    if old is None:
        return {"success": False, "error": f"decision {old_id} not found"}

    effective_file_path = file_path if file_path is not None else old.get("file_path")
    effective_tags = tags if tags is not None else old.get("tags")
    effective_symbol = symbol if symbol is not None else old.get("symbol")
    supersede_marker = f"[supersedes {old_id}: {reason}]"
    effective_context = (
        f"{context}\n{supersede_marker}" if context else supersede_marker
    )

    new_id = record(
        decision=new_decision,
        file_path=effective_file_path,
        symbol=effective_symbol,
        context=effective_context,
        do_not_revert=do_not_revert,
        tags=effective_tags,
        alternatives_considered=alternatives_considered,
        would_re_examine_if=would_re_examine_if,
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


def find_semantic_duplicates(*, threshold: float = 0.75) -> list[dict[str, Any]]:
    """Tier-1 (semantic) surface: cluster ACTIVE decisions whose TEXT is a
    near-duplicate even though their ids differ (two engineers recorded the
    same decision in different words).

    Reporting only — the pairs are SURFACED for a human/agent to merge via
    supersede_decision, never auto-merged. The structural tier (repair_ids) is
    authoritative for correctness; the semantic tier escalates, so it can't
    silently destroy or mis-merge a genuine decision. O(n^2) over active
    decisions; fine for the bounded decision log.
    """
    from mcp_server.storage import reconcile

    active = list_all(limit=100000)["decisions"]
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(active):
        a_text = a.get("decision") or ""
        for b in active[i + 1 :]:
            c = reconcile.classify(
                a_text,
                b.get("decision") or "",
                b_protected=bool(b.get("do_not_revert")),
            )
            if c["kind"] == reconcile.KIND_DUPLICATE and c["similarity"] >= threshold:
                pairs.append(
                    {
                        "a_id": a.get("id"),
                        "b_id": b.get("id"),
                        "similarity": round(c["similarity"], 4),
                    }
                )
    pairs.sort(key=lambda p: (-p["similarity"], str(p["a_id"]), str(p["b_id"])))
    return pairs


def repair_ids(*, apply: bool = False) -> dict[str, Any]:
    """Detect (and optionally repair) base-id collisions in decisions.jsonl.

    v3.7.0 (Phase 25) cross-engineer surface. Two engineers who share a repo
    can both mint the same id; ``git merge`` combines the appended lines
    cleanly and ``read_merged`` then silently shadows one. This runs the
    deterministic :func:`id_repair.normalize` over the raw store:

      - ``apply=False`` (default): report only — how many collisions/dups and
        the id remap, without writing.
      - ``apply=True``: atomically rewrite decisions.jsonl with the normalized
        records (winners keep their id, losers get content-derived ids,
        byte-identical dups dropped) and rebuild all indexes.

    Idempotent: a store with no collisions is unchanged. Returns
    ``{collisions, deduped, remap, applied, changed}``.
    """
    from mcp_server.storage import id_repair

    path = paths.decisions_path()
    # Read raw AND malformed so a repair rewrite can preserve unparseable lines
    # (a git-conflict marker, a truncated write) — never silent data loss.
    raw, malformed = jsonl_store.read_records_and_malformed(path)
    result = id_repair.normalize(raw)
    changed = bool(result["collisions"] or result["deduped"])

    applied = False
    if apply and changed:
        jsonl_store.rewrite_all(path, result["records"], extra_lines=malformed)
        if malformed:
            logger.warning(
                "decisions_store.repair_ids: preserved %d malformed line(s) "
                "verbatim through the rewrite (run `codevira doctor` to clean).",
                len(malformed),
            )
        rebuild_indexes()
        applied = True

    return {
        "collisions": result["collisions"],
        "deduped": result["deduped"],
        "remap": result["remap"],
        "malformed_preserved": len(malformed),
        "changed": changed,
        "applied": applied,
    }


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


def one_line_summary(text: str | None, cap: int = 140) -> str:
    """Collapse ``text`` to a single line ≤ ``cap`` chars (E1, Phase 19).

    The summary-first tool defaults need a compact one-liner per decision:
    newlines/runs of whitespace collapse to single spaces, then the string
    is cut at a sentence boundary (``. ``) if one sits past the halfway
    mark, else at the last word boundary, with an ellipsis. Whole text is
    returned verbatim when it already fits (no spurious ellipsis).
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    cut = collapsed[:cap]
    dot = cut.rfind(". ")
    if dot >= cap // 2:
        return cut[: dot + 1]
    space = cut.rfind(" ")
    if space >= cap // 2:
        cut = cut[:space]
    return cut + "…"
