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


def get_session_context() -> dict:
    """Single 'catch me up' call — designed to be the FIRST call in a session.

    Returns a compact snapshot (~500 tokens target) so the agent gets oriented
    without consuming its context window. Use follow-up tools (get_node,
    get_impact, search_decisions) for targeted details.

    Fields returned:
      current_phase     - {name, next_action, status}
      open_changesets   - up to 3 most recent, minimal fields
      recent_sessions   - up to 2 most recent, summary truncated to 100 chars
      recent_decisions  - up to 3 most recent, decision text truncated to 120 chars
      confidence        - {positive, negative, neutral, overall_rate}
      top_signals       - combined preferences + rules, top 3 each
      hint              - instructions for follow-up calls
    """
    db = _get_db()
    try:
        recent_sessions = db.get_recent_sessions(limit=2)
        recent_decisions = db.get_recent_decisions(limit=3)
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

        # Open changesets: minimal shape, max 3
        open_changesets = []
        try:
            from mcp_server.tools.changesets import list_open_changesets
            cs = list_open_changesets().get("changesets", [])
            for c in cs[:3]:
                open_changesets.append({
                    "id": c.get("id"),
                    "description": _truncate(c.get("description"), 100),
                    "files_pending_count": len(c.get("files_pending", []) or []),
                })
        except Exception:
            pass

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
                }
                for d in recent_decisions
            ],
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
