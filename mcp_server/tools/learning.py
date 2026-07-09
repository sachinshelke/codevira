"""
Learning tools — MCP tools for Codevira's adaptive memory system.

v3.0.0 surface (2026-05-22 surface-cut audit):
  - record_decision    : capture an architectural decision (+ tags / do_not_revert)
  - supersede_decision : retire an old decision and link to its replacement
  - get_session_context: single "catch me up" call for cross-tool continuity

v2.x tools deleted in the audit and NOT exposed here:
  - get_decision_confidence: surfaced a number nobody acted on
  - get_preferences        : returned noise more often than signal
  - get_learned_rules      : same — see ``retire_rule`` decision
  - get_project_maturity   : vanity metric; no policy consumed it
  - retire_rule            : was the cleanup for get_learned_rules

The internal helper ``get_decision_confidence`` is kept (used by
``get_session_context``'s confidence summary block); the standalone
MCP tool that exposed it is gone.
"""

from __future__ import annotations

import logging
from mcp_server.paths import get_data_dir
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)


def _get_db() -> SQLiteGraph:
    return SQLiteGraph(get_data_dir() / "graph" / "graph.db")


def get_decision_confidence(
    file_path: str | None = None, pattern: str | None = None
) -> dict:
    """Get confidence scores for decisions about a file or pattern.

    P0-D (rc.5 audit): the underlying outcomes table is only populated for
    decisions that were recorded WITH a non-null file_path AND have had at
    least a couple of subsequent commits to classify (kept / modified /
    reverted). When the table is empty users see ``total_decisions: 0`` and
    assume the feature is broken; in reality they may have recorded plenty
    of decisions but all without file_path. We now distinguish those cases
    explicitly in the returned dict + interpretation text.
    """
    db = _get_db()
    try:
        confidence = db.get_decision_confidence(file_path=file_path, pattern=pattern)
        label = file_path or pattern or "project-wide"
        # Diagnostic counts so the user can tell which case they're in.
        # v3.0 silent-storage fix (2026-05-23 RC audit): pre-fix this read
        # `SELECT COUNT(*) FROM decisions` against legacy SQLiteGraph which
        # is empty in v3.0 (all decision writes go to JSONL via
        # decisions_store). Users saw "No data — new territory" even with
        # dozens of decisions. Now reads the JSONL store directly.
        try:
            from mcp_server.storage import jsonl_store, paths as _storage_paths

            decision_records = jsonl_store.read_all(_storage_paths.decisions_path())
            decisions_total = len(decision_records)
            decisions_with_file = sum(1 for d in decision_records if d.get("file_path"))
            decisions_eligible_for_outcomes = decisions_with_file
        except Exception:
            decisions_total = decisions_with_file = decisions_eligible_for_outcomes = 0

        result = {
            "scope": label,
            **confidence,
            "decisions_in_db_total": decisions_total,
            "decisions_eligible_for_outcomes": decisions_eligible_for_outcomes,
            "interpretation": _interpret_confidence_with_eligibility(
                confidence_score=confidence["confidence"],
                outcomes_total=confidence["total_decisions"],
                decisions_total=decisions_total,
                decisions_eligible=decisions_eligible_for_outcomes,
            ),
        }
        return result
    finally:
        db.close()


def _interpret_confidence_with_eligibility(
    *,
    confidence_score: float,
    outcomes_total: int,
    decisions_total: int,
    decisions_eligible: int,
) -> str:
    """P0-D (rc.5): rich interpretation that distinguishes empty cases.

    Cases:
      A. outcomes > 0  → use existing _interpret_confidence (has data).
      B. outcomes = 0, eligible > 0 → 'awaiting commits' — git classifier
         hasn't classified yet because the project's git history doesn't
         have enough subsequent commits after the recorded decisions.
      C. outcomes = 0, eligible = 0, decisions > 0 → 'no eligible decisions':
         user has recorded decisions but ALL without file_path → tracker
         can't classify any of them. Tell them to use file_path.
      D. outcomes = 0, decisions = 0 → genuinely fresh project.
    """
    if outcomes_total > 0:
        return _interpret_confidence(confidence_score)
    if decisions_eligible > 0:
        return (
            f"Awaiting outcomes — {decisions_eligible} decision(s) recorded with "
            f"file_path are queued for classification. The git-based outcome "
            f"tracker classifies each as kept/modified/reverted after a few "
            f"subsequent commits touch the file. Make a few more commits and "
            f"re-run."
        )
    if decisions_total > 0:
        return (
            f"No eligible decisions — {decisions_total} decision(s) recorded "
            f"but all without file_path. Outcome tracking requires file_path "
            f"so the git-based classifier knows which file to watch. Re-record "
            f"key decisions via record_decision(decision=..., file_path=..., "
            f"...) to populate the outcomes table."
        )
    return _interpret_confidence(confidence_score)  # falls through to "No data"


# v2.2.0+ surface cut: get_preferences and get_learned_rules removed.
# Per the 2026-05-22 audit, preference/rule extraction surfaced noise
# rather than signal, and the founder never read the results in real
# sessions. The underlying tables stay (SQLiteGraph still records via
# log_session for back-compat) but they're no longer surfaced as MCP
# tools or via get_session_context. Slated for full storage cleanup
# in v2.3.0.


def record_decision(
    decision: str,
    file_path: str | None = None,
    symbol: str | None = None,
    context: str | None = None,
    do_not_revert: bool = False,
    session_id: str | None = None,
    tags: list[str] | None = None,
    force: bool = False,
    alternatives_considered: list[str] | None = None,
    would_re_examine_if: str | None = None,
) -> dict:
    """Record a single decision with optional do_not_revert flag.

    v2.2.0 — writes to ``<repo>/.codevira/decisions.jsonl`` (in-repo,
    git-committed). The v2.1.x SQLite-backed implementation is gone;
    no ChromaDB embedding, no calibration, no auto-recalibrate.

    v3.6.0 — ``symbol`` (optional) scopes the decision to a single function
    or class within ``file_path`` (e.g. ``file_path="auth.py",
    symbol="login"``). When the decision is ``do_not_revert`` and
    content-aware locking is on, the lock then blocks only edits that land
    INSIDE that symbol; edits elsewhere in the file downgrade to a warn. With
    no ``symbol`` (the default) the lock stays file-scoped, exactly as before.
    ``symbol`` requires ``file_path`` to take effect.

    Conflict / duplicate check (Item 20): runs FTS5 pre-write search
    against existing protected decisions; surfaces ``_conflict_warning``
    if the new decision matches one. Pass ``force=True`` to suppress.

    Input coercion (Item 30): non-bool ``do_not_revert`` is accepted and
    coerced (with ``_input_coerced_warning`` so the caller knows).

    Returns ``{recorded, decision_id, session_id, do_not_revert, tags,
    hint, [_conflict_warning], [_input_coerced_warning]}``.
    """
    if not decision or not isinstance(decision, str):
        return {
            "recorded": False,
            "error": "decision must be a non-empty string",
        }

    # Item 30: detect non-bool do_not_revert input.
    input_coerced_warning = None
    if not isinstance(do_not_revert, bool):
        input_coerced_warning = (
            f"do_not_revert passed as {type(do_not_revert).__name__} "
            f"({do_not_revert!r}); coerced to {bool(do_not_revert)}"
        )

    # Item 20: pre-write conflict check (FTS5-based in v2.2.0).
    conflict_warning = None
    if not force:
        try:
            from mcp_server.tools.check_conflict import check_conflict

            check = check_conflict(decision_text=decision.strip(), file_path=file_path)
            conflicts = check.get("conflicts") or []
            duplicates = check.get("duplicates") or []
            if conflicts:
                conflict_warning = {
                    "kind": "conflict",
                    "message": (
                        f"This decision may conflict with {len(conflicts)} "
                        f"protected (do_not_revert=True) decision(s). Pass "
                        f"force=True to record anyway, or use "
                        f"supersede_decision(old_id, new_decision, reason) "
                        f"to explicitly retire the prior one."
                    ),
                    "conflicting_decision_ids": [
                        c.get("decision_id") for c in conflicts
                    ],
                }
            elif duplicates:
                conflict_warning = {
                    "kind": "duplicate",
                    "message": (
                        f"This decision looks similar to {len(duplicates)} "
                        f"existing decision(s). Pass force=True to record "
                        f"anyway. Existing ids: {[d.get('decision_id') for d in duplicates]}."
                    ),
                    "duplicate_decision_ids": [
                        d.get("decision_id") for d in duplicates
                    ],
                }
        except Exception:
            # P9: never block the write on the conflict check failing.
            pass

    from mcp_server.storage import decisions_store

    # v3.0.1: resolve the effective session_id ONCE up front so the
    # response (echoed to the agent) matches the on-disk record. Prior
    # to this, learning.py echoed the literal "ad-hoc" while
    # decisions_store.record() wrote a unique "ad-hoc-XXXXXX" slug,
    # leaving caller-visible and persisted state divergent.
    effective_session_id = session_id or decisions_store.default_session_id()

    decision_id = decisions_store.record(
        decision=decision,
        file_path=file_path,
        symbol=symbol,
        context=context,
        do_not_revert=bool(do_not_revert),
        session_id=effective_session_id,
        tags=tags,
        alternatives_considered=alternatives_considered,
        would_re_examine_if=would_re_examine_if,
    )

    response: dict = {
        "recorded": True,
        "decision_id": decision_id,
        "session_id": effective_session_id,
        "do_not_revert": bool(do_not_revert),
        "hint": (
            "Decision recorded as protected. Future search_decisions() "
            "calls in this OR other AI tools will surface "
            "do_not_revert=true. To later change this decision (text "
            "or flag), use `supersede_decision(old_id=<this id>, "
            "new_decision=<text>, reason=<why>)` — that preserves "
            "the audit trail."
            if do_not_revert
            else "Decision recorded. Pass do_not_revert=true if it should "
            "be locked against future revert."
        ),
    }
    if input_coerced_warning:
        response["_input_coerced_warning"] = input_coerced_warning
    if conflict_warning:
        response["_conflict_warning"] = conflict_warning
    if tags:
        response["tags"] = sorted(
            {str(t).strip().lower() for t in tags if str(t).strip()}
        )
    return response


def supersede_decision(
    old_id: int | str,
    new_decision: str,
    reason: str,
    *,
    file_path: str | None = None,
    context: str | None = None,
    do_not_revert: bool = False,
    tags: list[str] | None = None,
) -> dict:
    """v2.2.0 Item 26: retire ``old_id`` and link the replacement.

    Writes the new decision (text prefixed with
    ``[supersedes <old_id>: <reason>] <new_text>``), then appends an
    amendment line flagging the old as superseded.

    Returns ``{success, old_id, new_id, reason}``.
    """
    if not new_decision or not isinstance(new_decision, str):
        return {"success": False, "error": "new_decision must be a non-empty string"}
    if not reason or not isinstance(reason, str):
        return {"success": False, "error": "reason must be a non-empty string"}

    from mcp_server.storage import decisions_store

    # The new prefixed text matches the v2.1.x convention for back-compat
    # with anything that parses decision text for a "supersedes #" hint.
    prefixed = f"[supersedes {old_id}: {reason.strip()}] {new_decision.strip()}"
    return decisions_store.supersede(
        old_id=str(old_id),
        new_decision=prefixed,
        reason=reason.strip(),
        file_path=file_path,
        do_not_revert=bool(do_not_revert),
        tags=tags,
    )


def set_decision_flag(
    decision_id: str,
    *,
    do_not_revert: bool | None = None,
    tags: list[str] | None = None,
    is_outdated: bool | None = None,
) -> dict:
    """v3.0.0 (2026-05-23 RC-audit follow-up): lightweight in-place
    update for do_not_revert and/or tags on an existing decision.

    Earlier the only way to flip ``do_not_revert`` was
    ``supersede_decision(old_id, new_decision, reason, do_not_revert=...)``
    which requires rewriting the full decision text and a reason — heavy
    for a one-flag toggle. ``set_decision_flag`` appends a single
    amendment record to ``.codevira/decisions.jsonl`` and rebuilds the
    indexes.

    For semantic rewrites (different intent or scope), keep using
    ``supersede_decision`` — that preserves the lineage history.

    Args:
        decision_id: ID of the decision to amend (e.g. ``"D000007"``).
        do_not_revert: New flag value (True / False / None to leave
            unchanged).
        tags: New tag list (replaces the existing set; None to leave
            unchanged).

    Returns ``{success, decision_id, updates}`` where ``updates`` lists
    only the fields actually changed. No-op if no fields are supplied.
    """
    if not decision_id or not isinstance(decision_id, str):
        return {"success": False, "error": "decision_id must be a non-empty string"}
    if do_not_revert is None and tags is None and is_outdated is None:
        return {
            "success": False,
            "error": "supply at least one of do_not_revert / tags / is_outdated",
        }

    from mcp_server.storage import decisions_store

    return decisions_store.set_flag(
        decision_id=decision_id,
        do_not_revert=do_not_revert,
        tags=tags,
        is_outdated=is_outdated,
    )


def mark_decision_outdated(decision_id: str, reason: str | None = None) -> dict:
    """v3.7.0 staleness read-side: tombstone a decision as *outdated* so it
    stops surfacing in ``get_session_context`` / ``search_decisions`` /
    ``list_decisions`` — without deleting it.

    Use when a decision is simply no longer true and has NO successor (for a
    replacement, use ``supersede_decision`` to preserve lineage). Reversible:
    ``set_decision_flag(decision_id, is_outdated=False)`` clears it. The
    record and its audit trail are preserved.

    Args:
        decision_id: ID of the decision to retire (e.g. ``"D000007"``).
        reason: Optional short note on why it's outdated (capped, stored).

    Returns ``{success, decision_id, is_outdated}`` or ``{success: False,
    error}`` if the id is unknown.
    """
    if not decision_id or not isinstance(decision_id, str):
        return {"success": False, "error": "decision_id must be a non-empty string"}

    from mcp_server.storage import decisions_store

    return decisions_store.mark_outdated(decision_id, reason=reason)


def reaffirm_decision(decision_id: str) -> dict:
    """v3.2.0: refresh a ``do_not_revert`` decision's soft-expire clock.

    Long-lived locked decisions can grow stale — the world that made
    them right may have changed. v3.2.0 introduces a soft expiry surfaced
    via ``dnr_soft_expired`` on search/list output (default 180 days,
    override via ``CODEVIRA_DNR_SOFT_EXPIRE_DAYS``).

    When a soft-expired decision is still load-bearing, call
    ``reaffirm_decision(id)`` to reset the clock. The amendment is
    append-only — the lineage of reaffirmations is preserved in the
    JSONL log.

    For semantic rewrites use ``supersede_decision``; for flipping
    ``do_not_revert`` itself use ``set_decision_flag``. ``reaffirm_decision``
    is the lightweight "still valid" signal.

    Returns ``{success, decision_id, reaffirmed_at}`` on success;
    ``{success: False, error}`` if the decision doesn't exist.
    """
    if not decision_id or not isinstance(decision_id, str):
        return {"success": False, "error": "decision_id must be a non-empty string"}

    from mcp_server.storage import decisions_store

    return decisions_store.reaffirm(decision_id=decision_id)


# v2.2.0+ (2026-05-22 surface-cut audit batch 6):
#   - record_decisions (batch) deleted: agents called single endpoints
#     in practice; batch saved theoretical round-trips that never
#     happened in real data. Use record_decision directly.
#   - mark_decision_protected deleted: redundant with
#     supersede_decision(old_id, new_decision, reason,
#     do_not_revert=True) which gives an audit trail for free.
#
# v2.2.0+: retire_rule removed along with get_learned_rules. The
# learned-rules surface is gone from the MCP tool list.


# v3.0.0 audit cleanup: get_project_maturity + _compute_maturity_score
# + _maturity_level + _maturity_hint deleted. The MCP tool wrapper was
# already removed in the 2026-05-22 audit (server.py dispatcher gone);
# the underlying functions had no other callers.


def _truncate(text: str | None, max_chars: int = 120) -> str | None:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _smart_truncate(text: str | None, max_chars: int = 160) -> str | None:
    """2026-05-18 v2.1.2 Item 6: word-boundary truncation that respects
    paths.

    Field-test Report 3 §"Truncation mid-word" flagged the existing
    `_truncate` cutting mid-path: `"test/uni…"` (truncated inside
    `test/unit/`). For learned rules — which often reference directory
    paths — that's actively misleading. This variant:

    1. Prefers cutting at the last whitespace boundary before `max_chars`
       (so words stay whole).
    2. If the text ends with a path-like segment that would be cut, keeps
       the FULL last path segment if it fits within `max_chars + 30`
       slack (so `test/unit/` stays intact at the small cost of a
       slightly-longer line).
    3. Falls back to character-truncate with `…` only when neither
       strategy produces a sensible cut.

    Used by `top_signals.rules` because learned rules embed paths.
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text

    # Strategy 1: word-boundary truncate at the last space ≤ max_chars.
    cut = text[: max_chars - 1]
    last_space = cut.rfind(" ")
    if last_space >= max_chars - 40:  # only honor if reasonably close to limit
        return text[:last_space] + " …"

    # Strategy 2: if the next path-like segment can fit in the slack zone,
    # keep it whole. Look ahead 30 chars for a slash-ended segment.
    slack = text[max_chars - 1 : max_chars + 29]
    slash_in_slack = slack.find("/")
    if 0 <= slash_in_slack <= 29:
        # Keep up to that slash. Length = max_chars-1 + slash_in_slack + 1
        end = max_chars - 1 + slash_in_slack + 1
        if end < len(text):
            return text[:end] + " …"
        return text  # the whole thing fits in slack — return unchanged

    # Strategy 3: character-truncate fallback (the original behavior).
    return text[: max_chars - 1] + "…"


# v1.8: Focus inference stop-list. A `next_action` composed entirely of these
# tokens is considered a weak signal (e.g. "continue work", "fix the thing")
# and is discarded in favour of the chronological fallback.
_WEAK_FOCUS_TOKENS = frozenset(
    {
        "continue",
        "work",
        "fix",
        "add",
        "update",
        "improve",
        "todo",
        "the",
        "a",
        "implement",
        "build",
    }
)


def _infer_focus(current_phase: dict) -> tuple[str | None, str | None]:
    """Infer what the agent is currently focused on.

    v2.2.0+: changesets removed. Focus inference now uses only the
    current phase's ``next_action`` field.

    Returns ``(focus, focus_source)``:
      - ``focus`` is a query string suitable for ``db.search_decisions()``
        (extracted keywords), or None if no confident signal.
      - ``focus_source`` is ``"next_action"`` or None — exposed to the
        agent so it can see *why* it got these decisions.
    """
    next_action = (current_phase or {}).get("next_action") or ""
    tokens = next_action.lower().split()
    if len(tokens) >= 4 and not all(t in _WEAK_FOCUS_TOKENS for t in tokens):
        keywords = " ".join(t for t in tokens if len(t) >= 4)
        if keywords:
            return keywords, "next_action"

    return None, None


def get_session_context(since: str | None = None) -> dict:
    """Single 'catch me up' call — designed to be the FIRST call in a session.

    Returns a compact snapshot (~500 tokens target) so the agent gets oriented
    without consuming its context window. Use follow-up tools (get_node,
    get_impact, search_decisions) for targeted details.

    2026-05-18 v2.1.2 Item 25: optional ``since`` (ISO 8601 / YYYY-MM-DD)
    filter — restricts ``recent_decisions`` and ``recent_sessions`` to
    entries created after the cutoff. Useful for "what's new since I
    was last here" session bootstrap.

    Fields returned:
      current_phase     - {name, next_action, status}
      recent_sessions   - up to 2 most recent, summary truncated to 100 chars
      recent_decisions  - up to 3 focus-weighted decisions (v1.8), truncated to 120 chars
      focus_source      - v1.8: why recent_decisions were chosen
                          ("next_action" | null)
      confidence        - {positive, negative, neutral, overall_rate}
      top_signals       - combined preferences + rules, top 3 each
      hint              - instructions for follow-up calls
    """
    db = _get_db()
    try:
        # v3.0 silent-storage fix (2026-05-23 RC audit): pre-fix this read
        # sessions from SQLiteGraph which is empty in v3.0 (all session
        # writes go to .codevira/sessions.jsonl). Users saw recent_sessions=[]
        # in get_session_context even after recording sessions. Now reads
        # the JSONL store.
        try:
            from mcp_server.storage import sessions_store as _sessions_store

            recent_sessions = _sessions_store.read_recent(limit=2)
        except Exception:
            recent_sessions = []
        # v2.1.2 Item 25: post-filter recent_sessions by since-cutoff if provided.
        if since:
            recent_sessions = [
                s
                for s in recent_sessions
                if (s.get("created_at") or s.get("ts") or "") > since
            ]
        confidence = db.get_decision_confidence()
        # v2.2.0+: preferences + learned_rules dropped from session context
        # (auto-extracted signals were noise per 2026-05-22 audit).

        # Roadmap: only current phase name + next action (skip upcoming/completed)
        current_phase = {}
        try:
            from mcp_server.tools.roadmap import get_roadmap

            roadmap = get_roadmap()
            cp = roadmap.get("current_phase", {}) or {}
            current_phase = {
                "name": cp.get("name"),
                "status": cp.get("status"),
                "next_action": _truncate(cp.get("next_action"), 200),
            }
        except Exception:
            pass

        # v2.2.0+: changesets removed (never reached real usage). Focus
        # inference uses only the current phase's next_action.
        focus, focus_source = _infer_focus(current_phase)
        # v3.0.0 round-3 (2026-05-23 system-test finding): rewired to
        # read recent decisions from the JSONL canonical store via
        # decisions_store, NOT the legacy SQLiteGraph decisions table.
        # Pre-fix, every v3.0.0 project saw recent_decisions=[] in the
        # session-context payload because the SQLite decisions table is
        # never populated in v3.0.0 (all writes go to .codevira/decisions.jsonl).
        # Caught during the AgentStore system test in
        # scripts/system_test_agentstore.py::A8.
        from mcp_server.storage import decisions_store

        # v3.7.0 staleness read-side: don't surface decisions the git
        # outcome-tracker labeled "reverted" — reality moved past them, so
        # they read as stale/confusing in a fresh session. (Outdated
        # tombstones + superseded are already filtered by search/list_all.)
        def _is_stale(d: dict) -> bool:
            return (d.get("outcome") == "reverted") or bool(d.get("is_outdated"))

        recent_decisions: list[dict] = []
        if focus:
            try:
                hits = decisions_store.search(focus, limit=5, since=since)
                recent_decisions = [d for d in hits if not _is_stale(d)][:3]
            except Exception:
                recent_decisions = []
        # Fallback / pad to 3 with chronological-recent decisions from JSONL.
        if len(recent_decisions) < 3:
            seen_ids: set[str] = {
                str(r.get("id") or "") for r in recent_decisions if r.get("id")
            }
            try:
                page = decisions_store.list_all(
                    limit=10 if since else 3,
                    since=since,
                    full=False,
                )
                all_recent = page.get("decisions", []) if page else []
            except Exception:
                all_recent = []
            for d in all_recent:
                did = str(d.get("id") or "")
                if not did or did in seen_ids:
                    continue
                if _is_stale(d):
                    continue
                recent_decisions.append(d)
                seen_ids.add(did)
                if len(recent_decisions) >= 3:
                    break

        # v2.0-rc.2: Surface key_decisions from recently-completed phases.
        # Bug 5 — ``complete_phase(key_decisions=[...])`` writes to the
        # roadmap store, NOT the ``decisions`` table. Without this block
        # those decisions are invisible to ``get_session_context``, so a
        # new session starting fresh after a phase completion has no way
        # to learn what was just decided. We pull the most recent
        # completed phase's key_decisions (capped at 5) and surface them
        # tagged with their source so the AI can distinguish.
        recent_phase_decisions: list[dict] = []
        try:
            from mcp_server.tools.roadmap import _load_roadmap

            roadmap_data = _load_roadmap()
            completed = roadmap_data.get("completed_phases", []) or []
            # Latest first
            for phase in reversed(completed[-3:]):
                phase_num = phase.get("number")
                phase_name = phase.get("name")
                for decision in (phase.get("key_decisions") or [])[:5]:
                    recent_phase_decisions.append(
                        {
                            "decision": _truncate(decision, 120),
                            "phase_number": phase_num,
                            "phase_name": phase_name,
                            "source": "phase_completion",
                        }
                    )
                if len(recent_phase_decisions) >= 5:
                    break
            recent_phase_decisions = recent_phase_decisions[:5]
        except Exception:
            recent_phase_decisions = []

        # v2.0-rc.3: Bug 8 — roadmap drift detection.
        # If codevira's claimed phase hasn't been updated in days but
        # commits keep landing, the roadmap is stale and the AI should
        # be prompted to reconcile. The check is best-effort and never
        # crashes the session-context call.
        drift_warning = None
        try:
            from mcp_server.roadmap_drift import check_drift
            from mcp_server.paths import get_project_root

            drift_warning = check_drift(
                project_root=get_project_root(),
                current_phase=current_phase,
            )
        except Exception:
            drift_warning = None

        # v3.1.0 M2 Phase 3: working-memory panel. Surfaces the top-3
        # live observations/goals so the agent sees its own recent
        # scratchpad in the catch-me-up payload. Capped at 3 entries
        # (~150 tokens) to honor the get_session_context token budget.
        # Best-effort: any failure (no working.jsonl yet, store error)
        # surfaces an empty entries list rather than crashing the
        # session-context call.
        working_panel: dict = {"entries": [], "count": 0}
        try:
            from mcp_server.storage import working_store

            top = working_store.list_top_k(top_k=3)
            working_panel = {
                "entries": [
                    {
                        "entry_id": e.get("id"),
                        "kind": e.get("kind"),
                        "content": _truncate(e.get("content"), 120),
                        "importance": e.get("importance"),
                    }
                    for e in top
                ],
                "count": len(top),
            }
        except Exception:
            pass

        # v3.1.0 M6 Phase B: consensus panel. Top-3 pending cross-IDE
        # conflicts ordered by (do_not_revert × recency). Capped at
        # ~200 tokens. Best-effort: missing pending_conflicts.jsonl,
        # store errors, etc. surface an empty count without crashing.
        consensus_panel: dict = {"pending_count": 0, "top": []}
        try:
            from mcp_server.storage import consensus_store

            pending = consensus_store.list_pending(limit=20)
            # Sort: do_not_revert first, then by recency (already
            # newest-first from read_recent).
            pending.sort(
                key=lambda r: (bool(r.get("do_not_revert")), r.get("ts") or ""),
                reverse=True,
            )
            consensus_panel = {
                "pending_count": len(pending),
                "top": [
                    {
                        "pending_conflict_id": r.get("id"),
                        "foreign_decision_id": r.get("foreign_decision_id"),
                        "foreign_ide": (r.get("foreign_origin") or {}).get("ide"),
                        "current_decision_id": r.get("current_decision_id"),
                        "conflict_kind": r.get("conflict_kind"),
                        "do_not_revert": r.get("do_not_revert"),
                        "summary": _truncate(r.get("summary"), 80),
                    }
                    for r in pending[:3]
                ],
            }
        except Exception:
            pass

        # v3.3.0 Phase 4 (D0000LU): one budgeted style line from LLM-
        # distilled preferences (~30 tokens). Omitted entirely when no
        # communication preferences exist — token-frugal per D000018.
        style_line: str | None = None
        try:
            from mcp_server.tools.preferences import search_preferences

            _prefs = search_preferences(category="communication", top_k=3)
            _signals = [
                p["signal"] for p in _prefs.get("preferences", []) if p.get("signal")
            ]
            if _signals:
                style_line = "; ".join(_signals)[:160]
        except Exception:  # noqa: BLE001 — the brief must never fail on this
            style_line = None

        return {
            "current_phase": current_phase,
            "drift_warning": drift_warning,
            **({"style": style_line} if style_line else {}),
            "working": working_panel,
            "consensus": consensus_panel,
            "recent_sessions": [
                {
                    "session_id": s["session_id"],
                    "summary": _truncate(s.get("summary"), 100),
                    "phase": s.get("phase"),
                }
                for s in recent_sessions
            ],
            "recent_decisions": [
                {
                    # E1 (Phase 19): one-line summary (collapses newlines) so
                    # the brief stays genuinely single-line, not just ≤120 chars.
                    "decision": decisions_store.one_line_summary(
                        d.get("decision"), 120
                    ),
                    "file_path": d.get("file_path"),
                    "source": "session",
                }
                for d in recent_decisions[:3]
            ],
            "recent_phase_decisions": recent_phase_decisions,
            "focus_source": focus_source,
            # 2026-05-18 v2.1.2 Item 8: hide empty auto-signal fields on
            # fresh projects. Field-test Report 3 §"Auto-populated signals
            # stay empty for early projects" and Report 2 both flagged
            # confidence=null / preferences=[] as misleading for new
            # users (looks broken). For projects with no classified
            # outcomes yet, replace `confidence` with a human-readable
            # `confidence_note`. For empty `preferences` / `rules`,
            # OMIT them entirely (cleaner) and surface a single
            # `top_signals_note` instead.
            **(
                {
                    "confidence": {
                        "overall_rate": confidence.get("overall_rate"),
                        "total_decisions": confidence.get("total", 0),
                    }
                }
                if confidence.get("total", 0) > 0
                else {
                    "confidence_note": (
                        "Outcome tracker has not classified any decisions yet. "
                        "Outcomes accumulate over git commits (kept / modified / "
                        "reverted) — confidence scores appear once data is "
                        "available."
                    )
                }
            ),
            # v2.2.0+: top_signals (preferences + rules) removed.
            # The auto-extracted signals produced noise rather than value
            # per the 2026-05-22 audit; nobody read them in real sessions.
            "hint": (
                "Before touching a file: get_node(path) + get_impact(path). "
                "For past decisions: search_decisions(query). "
                "After meaningful work, call update_phase_status / "
                "complete_phase / write_session_log to keep memory current. "
                "Admin tools (export_graph, find_hotspots) available via CLI."
            ),
        }
    finally:
        db.close()


def _interpret_confidence(score: float) -> str:
    if score >= 0.8:
        return "High confidence — consistent successful patterns in this area."
    elif score >= 0.5:
        return "Moderate confidence — some patterns established but results are mixed."
    elif score > 0:
        return "Low confidence — limited history or frequent corrections. Proceed carefully."
    return "No data — this is new territory. Decisions here will build the baseline."


# v3.0.0 audit cleanup: _compute_maturity_score / _maturity_level /
# _maturity_hint deleted along with get_project_maturity. They scored
# the project on session count, coverage, learned_rules count, and
# preference signal count — three of those inputs are zero in v3.0.0
# because their MCP tools were deleted in the 2026-05-22 audit.
