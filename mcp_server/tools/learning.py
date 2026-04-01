"""
Learning tools — MCP tools for Codevira's adaptive memory system.

These tools expose the feedback loop to AI agents:
  - get_decision_confidence: How confident is the system about decisions in an area?
  - get_preferences: What coding style does the developer prefer?
  - get_learned_rules: Auto-generated rules from observed patterns
  - get_project_maturity: Overall project intelligence score
  - get_session_context: Single "catch me up" call for cross-tool continuity
"""
import json
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


def get_session_context() -> dict:
    """
    Single 'catch me up' call for cross-tool continuity.
    Returns everything a new agent session needs to understand the current state:
    current roadmap phase, open changesets, recent decisions with confidence, and recent sessions.
    """
    db = _get_db()
    try:
        # Recent sessions
        recent_sessions = db.get_recent_sessions(limit=3)

        # Recent decisions with confidence
        recent_decisions = db.get_recent_decisions(limit=5)

        # Confidence overview
        confidence = db.get_decision_confidence()

        # Preferences summary (top 5 by frequency)
        prefs = db.get_preferences(min_frequency=2)[:5]

        # Learned rules (high confidence only)
        rules = db.get_learned_rules(min_confidence=0.6)[:5]

        context = {
            "recent_sessions": [
                {
                    "session_id": s["session_id"],
                    "summary": s.get("summary"),
                    "phase": s.get("phase"),
                }
                for s in recent_sessions
            ],
            "recent_decisions": [
                {
                    "decision": d["decision"],
                    "file_path": d.get("file_path"),
                    "phase": d.get("phase"),
                }
                for d in recent_decisions
            ],
            "overall_confidence": confidence,
            "top_preferences": [
                {"category": p["category"], "signal": p["signal"], "frequency": p["frequency"]}
                for p in prefs
            ],
            "top_rules": [
                {"rule": r["rule_text"], "confidence": r["confidence"]}
                for r in rules
            ],
        }

        # Add roadmap context if available
        try:
            from mcp_server.tools.roadmap import get_roadmap
            roadmap = get_roadmap()
            context["roadmap"] = {
                "current_phase": roadmap.get("current_phase", {}).get("name"),
                "next_action": roadmap.get("current_phase", {}).get("next_action"),
                "status": roadmap.get("current_phase", {}).get("status"),
            }
        except Exception as e:
            logger.debug("Could not load roadmap context: %s", e)
            context["roadmap"] = None

        # Add open changesets if any
        try:
            from mcp_server.tools.changesets import list_open_changesets
            changesets = list_open_changesets()
            context["open_changesets"] = changesets.get("changesets", [])
        except Exception as e:
            logger.debug("Could not load changesets context: %s", e)
            context["open_changesets"] = []

        return context
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
