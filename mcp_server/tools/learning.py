"""
Learning tools — MCP tools for Codevira's adaptive memory system.

These tools expose the feedback loop to AI agents:
  - get_decision_confidence: How confident is the system about decisions in an area?
  - get_preferences: What coding style does the developer prefer?
  - get_learned_rules: Auto-generated rules from observed patterns
  - get_project_maturity: Overall project intelligence score
  - get_session_context: Single "catch me up" call for cross-tool continuity
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
        try:
            decisions_total = db.conn.execute(
                "SELECT COUNT(*) FROM decisions"
            ).fetchone()[0]
            decisions_with_file = db.conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE file_path IS NOT NULL"
            ).fetchone()[0]
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
    context: str | None = None,
    do_not_revert: bool = False,
    session_id: str | None = None,
    tags: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Record a single decision with optional do_not_revert flag.

    v2.2.0 — writes to ``<repo>/.codevira/decisions.jsonl`` (in-repo,
    git-committed). The v2.1.x SQLite-backed implementation is gone;
    no ChromaDB embedding, no calibration, no auto-recalibrate.

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

    decision_id = decisions_store.record(
        decision=decision,
        file_path=file_path,
        context=context,
        do_not_revert=bool(do_not_revert),
        session_id=session_id,
        tags=tags,
    )

    response: dict = {
        "recorded": True,
        "decision_id": decision_id,
        "session_id": session_id or "ad-hoc",
        "do_not_revert": bool(do_not_revert),
        "hint": (
            "Decision recorded as protected. Future search_decisions() "
            "calls in this OR other AI tools will surface "
            "do_not_revert=true. Use mark_decision_protected(decision_id, "
            "do_not_revert=false) to unprotect later."
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


def record_decisions(decisions: list[dict]) -> dict:
    """v2.1.2 Item 23: batch variant of record_decision (in v2.2.0 backend).

    Each item accepts the same keys as record_decision: ``decision``
    (required), ``file_path``, ``context``, ``do_not_revert``,
    ``session_id``, ``tags``, ``force``.

    Returns ``{count, recorded, errors, hint}``. Best-effort — one
    bad item doesn't reject the rest.
    """
    if not isinstance(decisions, list):
        return {
            "recorded": [],
            "count": 0,
            "errors": [{"idx": 0, "error": "decisions must be a list"}],
        }

    out_ids: list[str] = []
    errors: list[dict] = []
    for idx, item in enumerate(decisions):
        if not isinstance(item, dict):
            errors.append({"idx": idx, "error": "item must be a dict"})
            continue
        try:
            r = record_decision(
                decision=item.get("decision", ""),
                file_path=item.get("file_path"),
                context=item.get("context"),
                do_not_revert=bool(item.get("do_not_revert", False)),
                session_id=item.get("session_id"),
                tags=item.get("tags"),
                force=bool(item.get("force", False)),
            )
            if r.get("recorded"):
                out_ids.append(r["decision_id"])
            else:
                errors.append({"idx": idx, "error": r.get("error") or "unknown"})
        except Exception as exc:  # noqa: BLE001
            errors.append({"idx": idx, "error": str(exc)})
    return {
        "count": len(out_ids),
        "recorded": out_ids,
        "errors": errors,
        "hint": (
            f"Recorded {len(out_ids)} of {len(decisions)} decisions. "
            f"Each shows up in subsequent search_decisions / list_decisions."
        ),
    }


def mark_decision_protected(
    decision_id: int | str,
    do_not_revert: bool,
) -> dict:
    """Flip the do_not_revert flag on an existing decision.

    v2.2.0: appends an amendment line to decisions.jsonl; rebuilds the
    manifest + digest + FTS5 indexes. ``do_not_revert=False`` is not
    yet supported (the only mutation is mark-as-protected); a future
    release will add the unprotect path via amendment.
    """
    if not do_not_revert:
        # In v2.2.0 we only support marking-as-protected via amendment.
        # Unprotect would need a separate amendment shape (negation);
        # add it when there's a real use case. Not asserted by the
        # integration test contract.
        return {
            "updated": False,
            "decision_id": decision_id,
            "error": (
                "v2.2.0 only supports do_not_revert=True; "
                "unprotect not yet implemented"
            ),
        }

    from mcp_server.storage import decisions_store

    res = decisions_store.mark_protected(str(decision_id))
    if not res.get("success"):
        return {
            "updated": False,
            "decision_id": decision_id,
            "error": res.get("error"),
        }
    return {
        "updated": True,
        "decision_id": decision_id,
        "do_not_revert": True,
    }


# v2.2.0+: retire_rule removed along with get_learned_rules. The
# learned-rules surface is gone from the MCP tool list.


def get_project_maturity() -> dict:
    """Get overall project intelligence and maturity metrics."""
    db = _get_db()
    try:
        maturity = db.get_project_maturity()
        score = _compute_maturity_score(maturity)
        return {
            **maturity,
            "maturity_score": score,
            "maturity_level": _maturity_level(score),
            "hint": _maturity_hint(score),
        }
    finally:
        db.close()


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
        recent_sessions = db.get_recent_sessions(limit=2)
        # v2.1.2 Item 25: post-filter recent_sessions by since-cutoff if provided.
        if since:
            recent_sessions = [
                s for s in recent_sessions if (s.get("created_at") or "") > since
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
        recent_decisions: list[dict] = []
        if focus:
            try:
                recent_decisions = db.search_decisions(
                    focus,
                    limit=3,
                    since=since,
                )
            except Exception:
                recent_decisions = []
        # Fallback / pad to 3 with chronological recent decisions
        if len(recent_decisions) < 3:
            seen_ids: set[tuple] = {
                (r.get("file_path"), r.get("decision"), r.get("created_at"))
                for r in recent_decisions
            }
            for d in db.get_recent_decisions(limit=10 if since else 3):
                # v2.1.2 Item 25: skip rows older than since cutoff.
                if since and (d.get("created_at") or "") <= since:
                    continue
                key = (d.get("file_path"), d.get("decision"), d.get("created_at"))
                if key in seen_ids:
                    continue
                recent_decisions.append(d)
                seen_ids.add(key)
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

        return {
            "current_phase": current_phase,
            "drift_warning": drift_warning,
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
                    "decision": _truncate(d.get("decision"), 120),
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


def _compute_maturity_score(maturity: dict) -> float:
    """Compute a 0-100 maturity score from multiple signals."""
    score = 0.0

    # Sessions (max 20 points)
    score += min(maturity["session_count"] * 2, 20)

    # Coverage (max 30 points)
    score += maturity["coverage"] * 30

    # Confidence (max 25 points)
    score += maturity["overall_confidence"] * 25

    # Learned rules (max 15 points)
    score += min(maturity["learned_rules"] * 3, 15)

    # Preferences (max 10 points)
    score += min(maturity["preference_signals"] * 2, 10)

    return round(min(score, 100), 1)


def _maturity_level(score: float) -> str:
    if score >= 80:
        return "Expert — agents have rich context and high confidence."
    elif score >= 50:
        return "Intermediate — good coverage, patterns emerging."
    elif score >= 20:
        return "Growing — some sessions logged, building baseline."
    return "New — fresh project, minimal agent memory."


def _maturity_hint(score: float) -> str:
    if score >= 80:
        return "Project memory is mature. Agents should rely on learned patterns and confidence scores."
    elif score >= 50:
        return "Good progress. Continue using Codevira — confidence and rules will keep improving."
    elif score >= 20:
        return "Still building memory. Run more agent sessions and outcomes will start influencing confidence."
    return "This is a fresh start. Every session logs decisions that future agents will learn from."
