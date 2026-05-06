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


def get_decision_confidence(file_path: str | None = None, pattern: str | None = None) -> dict:
    """Get confidence scores for decisions about a file or pattern."""
    db = _get_db()
    try:
        confidence = db.get_decision_confidence(file_path=file_path, pattern=pattern)
        label = file_path or pattern or "project-wide"
        return {
            "scope": label,
            **confidence,
            "interpretation": _interpret_confidence(confidence["confidence"]),
        }
    finally:
        db.close()


def get_preferences(category: str | None = None) -> dict:
    """Get learned developer preferences."""
    db = _get_db()
    try:
        prefs = db.get_preferences(category=category, min_frequency=1)
        return {
            "preferences": prefs,
            "total": len(prefs),
            "hint": "Apply these preferences when writing code to match the developer's style."
                    if prefs else "No preferences learned yet. They build up over sessions.",
        }
    finally:
        db.close()


def get_learned_rules(file_path: str | None = None, category: str | None = None) -> dict:
    """Get auto-generated rules from observed patterns."""
    db = _get_db()
    try:
        rules = db.get_learned_rules(category=category, file_pattern=file_path, min_confidence=0.3)
        return {
            "rules": [
                {
                    "rule": r["rule_text"],
                    "confidence": r["confidence"],
                    "category": r["category"],
                    "applies_to": r.get("file_pattern"),
                }
                for r in rules
            ],
            "total": len(rules),
            "hint": "These rules were learned from past sessions. Higher confidence = more reliable."
                    if rules else "No rules learned yet. They emerge after multiple sessions.",
        }
    finally:
        db.close()


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


# v1.8: Focus inference stop-list. A `next_action` composed entirely of these
# tokens is considered a weak signal (e.g. "continue work", "fix the thing")
# and is discarded in favour of the chronological fallback.
_WEAK_FOCUS_TOKENS = frozenset({
    "continue", "work", "fix", "add", "update", "improve",
    "todo", "the", "a", "implement", "build",
})


def _infer_focus(open_changesets: list[dict], current_phase: dict) -> tuple[str | None, str | None]:
    """Infer what the agent is currently focused on.

    Returns ``(focus, focus_source)``:
      - ``focus`` is a query string suitable for ``db.search_decisions()``
        (file path or extracted keywords), or None if no confident signal.
      - ``focus_source`` is ``"open_changeset:<id>"``, ``"next_action"``, or
        None — exposed to the agent so it can see *why* it got these
        decisions and override if inference went wrong.

    Priority order (first hit wins):
      1. Open changesets with ``files_pending`` — use the first file of the
         most recently created changeset. The list is already filtered to
         in_progress; we sort by ``created`` desc to tie-break.
      2. Current phase ``next_action`` with a strong signal — see
         ``_WEAK_FOCUS_TOKENS``.
      3. Otherwise ``(None, None)``.
    """
    if open_changesets:
        # Sort by `created` (ISO string) desc. No `last_updated` field exists
        # on the changeset payload, so `created` is the best proxy.
        ranked = sorted(
            open_changesets,
            key=lambda c: c.get("created") or "",
            reverse=True,
        )
        for cs in ranked:
            pending = cs.get("files_pending") or []
            if pending:
                return pending[0], f"open_changeset:{cs.get('id')}"

    next_action = (current_phase or {}).get("next_action") or ""
    tokens = next_action.lower().split()
    if len(tokens) >= 4 and not all(t in _WEAK_FOCUS_TOKENS for t in tokens):
        # Strong signal: extract tokens >= 4 chars as the query
        keywords = " ".join(t for t in tokens if len(t) >= 4)
        if keywords:
            return keywords, "next_action"

    return None, None


def get_session_context() -> dict:
    """Single 'catch me up' call — designed to be the FIRST call in a session.

    Returns a compact snapshot (~500 tokens target) so the agent gets oriented
    without consuming its context window. Use follow-up tools (get_node,
    get_impact, search_decisions) for targeted details.

    Fields returned:
      current_phase     - {name, next_action, status}
      open_changesets   - up to 3 most recent, minimal fields
      recent_sessions   - up to 2 most recent, summary truncated to 100 chars
      recent_decisions  - up to 3 focus-weighted decisions (v1.8), truncated to 120 chars
      focus_source      - v1.8: why recent_decisions were chosen
                          ("open_changeset:<id>" | "next_action" | null)
      confidence        - {positive, negative, neutral, overall_rate}
      top_signals       - combined preferences + rules, top 3 each
      hint              - instructions for follow-up calls
    """
    db = _get_db()
    try:
        recent_sessions = db.get_recent_sessions(limit=2)
        confidence = db.get_decision_confidence()
        prefs = db.get_preferences(min_frequency=2)[:3]
        rules = db.get_learned_rules(min_confidence=0.6)[:3]

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

        # Raw changesets (full payload) — kept for focus inference.
        # Trimmed shape (below) is what goes on the response.
        raw_changesets: list[dict] = []
        try:
            from mcp_server.tools.changesets import list_open_changesets
            raw_changesets = list_open_changesets().get("open_changesets", []) or []
        except Exception:
            pass

        open_changesets = [
            {
                "id": c.get("id"),
                "description": _truncate(c.get("description"), 100),
                "files_pending_count": len(c.get("files_pending", []) or []),
            }
            for c in raw_changesets[:3]
        ]

        # v1.8: Focus-weighted `recent_decisions` ranking
        focus, focus_source = _infer_focus(raw_changesets, current_phase)
        recent_decisions: list[dict] = []
        if focus:
            try:
                recent_decisions = db.search_decisions(focus, limit=3)
            except Exception:
                recent_decisions = []
        # Fallback / pad to 3 with chronological recent decisions
        if len(recent_decisions) < 3:
            seen_ids: set[tuple] = {
                (r.get("file_path"), r.get("decision"), r.get("created_at"))
                for r in recent_decisions
            }
            for d in db.get_recent_decisions(limit=3):
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
                    recent_phase_decisions.append({
                        "decision": _truncate(decision, 120),
                        "phase_number": phase_num,
                        "phase_name": phase_name,
                        "source": "phase_completion",
                    })
                if len(recent_phase_decisions) >= 5:
                    break
            recent_phase_decisions = recent_phase_decisions[:5]
        except Exception:
            recent_phase_decisions = []

        return {
            "current_phase": current_phase,
            "open_changesets": open_changesets,
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
            "confidence": {
                "overall_rate": confidence.get("overall_rate"),
                "total_decisions": confidence.get("total", 0),
            },
            "top_signals": {
                "preferences": [
                    {"category": p["category"], "signal": _truncate(p["signal"], 80)}
                    for p in prefs
                ],
                "rules": [
                    {"rule": _truncate(r["rule_text"], 100), "confidence": round(r["confidence"], 2)}
                    for r in rules
                ],
            },
            "hint": (
                "Before touching a file: get_node(path) + get_impact(path). "
                "For past decisions: search_decisions(query). "
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
