"""
ai_promotion.py — Hero 10: AI Promotion Score policy.

Fires on SESSION_START. Reads aggregated outcome data + high-confidence
learned rules and INJECTS a brief digest into the AI's first turn so
it sees what's been working in the project.

This is the FIRST SESSION_START policy — analogous to how Hero 7 was
the first PostToolUse policy. The Bug 4 lesson (Week-9 integration QA)
applies: every wiring path that hasn't been exercised end-to-end is a
candidate for silent fail-open. Tests cover the
``claude_code_hooks.handle("SessionStart")`` path explicitly.

See ``docs/heroes/10-ai-promotion.md`` for spec.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Defaults + bounds
# ---------------------------------------------------------------------

_DEFAULT_MODE = "inject"
_MODES = ("off", "inject")

#: Decisions with score < this aren't surfaced as "stable".
_DEFAULT_MIN_SCORE = 0.7
_MIN_SCORE_FLOOR = 0.0
_MIN_SCORE_CEIL = 1.0

#: Learned rules below this confidence aren't surfaced.
_DEFAULT_MIN_CONFIDENCE = 0.7

#: Cap on items per category in the inject (3 stable + 3 rules = 6 max).
_DEFAULT_MAX_INJECT = 3
_MAX_INJECT_FLOOR = 1
_MAX_INJECT_CEIL = 10

#: Decisions with fewer than N outcomes aren't scored (low signal).
_DEFAULT_MIN_OUTCOMES = 2
_MIN_OUTCOMES_FLOOR = 1
_MIN_OUTCOMES_CEIL = 100

#: Lookback window for the SessionStart inject (broader than the CLI's
#: weekly digest so new sessions see longer context).
_INJECT_SINCE_DAYS = 30

#: Truncate decision text in the inject so the context block stays small.
_DECISION_DISPLAY_CHARS = 80


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _coerce_float(raw: str | None, default: float, lo: float, hi: float) -> float:
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def _coerce_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def _truncate(text: str, n: int = _DECISION_DISPLAY_CHARS) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# ---------------------------------------------------------------------
# Inject formatter — pure; no I/O
# ---------------------------------------------------------------------


def _format_inject(
    stable: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    drift: dict[str, Any] | None = None,
) -> str:
    """Build the human-readable context block injected into the AI's
    first turn. Kept tight (< 50 lines on a typical project) to respect
    the token budget."""
    lines: list[str] = []
    lines.append("## Codevira insights — what's been working in this project")
    lines.append("")

    # v2.0-rc.3: Bug 8 — surface roadmap drift FIRST so the AI can act
    # on it before relying on the stale state below.
    if drift and drift.get("drifted"):
        lines.append("### ⚠ Roadmap drift detected")
        lines.append(drift.get("message", "Codevira roadmap may be stale."))
        recent = drift.get("recent_commit_subjects") or []
        if recent:
            lines.append("Recent commits not yet reflected in codevira:")
            for subj in recent[:3]:
                lines.append(f"  - {subj}")
        lines.append("")

    if stable:
        lines.append("### Stable past decisions (kept across multiple commits)")
        for i, d in enumerate(stable, 1):
            score = d.get("score", 0.0)
            kept = d.get("kept", 0)
            total = d.get("total", 0)
            file_path = d.get("file_path") or "(unknown file)"
            decision = _truncate(str(d.get("decision") or ""))
            locked = d.get("locked", 0)
            lock_marker = "🔒 " if locked else ""
            lines.append(
                f"{i}. {lock_marker}{file_path} — \"{decision}\" "
                f"(score {score:.2f}, {kept}/{total} kept)"
            )
        lines.append("")

    if rules:
        lines.append("### High-confidence learned rules")
        for i, r in enumerate(rules, 1):
            conf = r.get("confidence", 0.0)
            text = _truncate(str(r.get("rule_text") or ""))
            cat = r.get("category") or ""
            cat_marker = f"[{cat}] " if cat else ""
            lines.append(f"{i}. {cat_marker}{text} (confidence {conf:.2f})")
        lines.append("")

    lines.append(
        "These reflect codevira's outcome tracking. Honor stable "
        "decisions; surface conflicts to the user before reverting them."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------


class AIPromotionScore(Policy):
    """Surface the project's stable decisions + learned rules at SessionStart.

    Verdict shapes:
      - allow            : event is not SessionStart, mode=off, no signals,
                           or no high-score data to surface.
      - inject           : a digest of top-N stable decisions + top-N rules.

    Never blocks. Never warns. Strictly advisory.
    """

    name = "ai_promotion_score"
    handles = (EventType.SESSION_START,)
    enabled_by_default = True
    priority = 10  # advisory; runs last among inject policies

    # ------- config (env-driven) -------

    def _config(self) -> dict[str, Any]:
        mode_raw = (
            os.environ.get("CODEVIRA_AI_PROMOTION_MODE", _DEFAULT_MODE) or ""
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE

        return {
            "mode": mode,
            "min_score": _coerce_float(
                os.environ.get("CODEVIRA_AI_PROMOTION_MIN_SCORE"),
                _DEFAULT_MIN_SCORE, _MIN_SCORE_FLOOR, _MIN_SCORE_CEIL,
            ),
            "min_confidence": _coerce_float(
                os.environ.get("CODEVIRA_AI_PROMOTION_MIN_CONFIDENCE"),
                _DEFAULT_MIN_CONFIDENCE, _MIN_SCORE_FLOOR, _MIN_SCORE_CEIL,
            ),
            "max_inject": _coerce_int(
                os.environ.get("CODEVIRA_AI_PROMOTION_MAX_INJECT"),
                _DEFAULT_MAX_INJECT, _MAX_INJECT_FLOOR, _MAX_INJECT_CEIL,
            ),
            "min_outcomes": _coerce_int(
                os.environ.get("CODEVIRA_AI_PROMOTION_MIN_OUTCOMES"),
                _DEFAULT_MIN_OUTCOMES, _MIN_OUTCOMES_FLOOR, _MIN_OUTCOMES_CEIL,
            ),
        }

    # ------- describe (for `codevira doctor`) -------

    def describe(self) -> dict[str, Any]:
        cfg = self._config()
        return {
            "name": self.name,
            "priority": self.priority,
            "handles": [str(h) for h in self.handles],
            "enabled_by_default": self.enabled_by_default,
            "mode": cfg["mode"],
            "min_score": cfg["min_score"],
            "min_confidence": cfg["min_confidence"],
            "max_inject": cfg["max_inject"],
            "min_outcomes": cfg["min_outcomes"],
            "config": {
                "mode": {
                    "values": list(_MODES),
                    "default": _DEFAULT_MODE,
                    "env": "CODEVIRA_AI_PROMOTION_MODE",
                    "description": "off / inject; never blocks (advisory)",
                },
                "min_score": {
                    "range": [_MIN_SCORE_FLOOR, _MIN_SCORE_CEIL],
                    "default": _DEFAULT_MIN_SCORE,
                    "env": "CODEVIRA_AI_PROMOTION_MIN_SCORE",
                    "description": "Decisions with score below this not surfaced as stable",
                },
                "min_confidence": {
                    "range": [_MIN_SCORE_FLOOR, _MIN_SCORE_CEIL],
                    "default": _DEFAULT_MIN_CONFIDENCE,
                    "env": "CODEVIRA_AI_PROMOTION_MIN_CONFIDENCE",
                    "description": "Learned rules below this confidence not surfaced",
                },
                "max_inject": {
                    "range": [_MAX_INJECT_FLOOR, _MAX_INJECT_CEIL],
                    "default": _DEFAULT_MAX_INJECT,
                    "env": "CODEVIRA_AI_PROMOTION_MAX_INJECT",
                    "description": "Cap on items per category injected",
                },
                "min_outcomes": {
                    "range": [_MIN_OUTCOMES_FLOOR, _MIN_OUTCOMES_CEIL],
                    "default": _DEFAULT_MIN_OUTCOMES,
                    "env": "CODEVIRA_AI_PROMOTION_MIN_OUTCOMES",
                    "description": "Decisions with fewer outcomes filtered (low signal)",
                },
            },
        }

    # ------- main entry point -------

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 0: event-type gate
        if event.event_type != EventType.SESSION_START:
            return PolicyVerdict.allow()

        # Stage 1: config / mode gate
        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: signals gate
        if signals is None:
            return PolicyVerdict.allow()

        # Stage 3: pull aggregated outcomes + rules. Both wrappers
        # already swallow exceptions and return [] on error — Hero 10 is
        # advisory; data layer flakiness must not break SessionStart.
        try:
            aggregated = signals.outcomes(
                since_days=_INJECT_SINCE_DAYS,
                min_outcomes=config["min_outcomes"],
                limit=max(config["max_inject"] * 4, 20),
            )
        except Exception as e:  # noqa: BLE001 — defensive; signals already wraps
            logger.warning("ai_promotion: signals.outcomes failed: %s", e)
            aggregated = []

        try:
            rules = signals.learned_rules(
                min_confidence=config["min_confidence"],
                max_items=config["max_inject"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ai_promotion: signals.learned_rules failed: %s", e)
            rules = []

        # Stage 4: filter to "stable" subset
        stable = [
            d for d in aggregated
            if d.get("score", 0.0) >= config["min_score"]
        ][: config["max_inject"]]

        # Stage 4b (v2.0-rc.3 / Bug 8): roadmap drift detection. If
        # codevira's claimed phase is stale relative to git activity,
        # surface a warning so the AI proactively reconciles. Drift
        # detection is best-effort and never fails the inject path.
        drift = None
        try:
            from mcp_server.roadmap_drift import check_drift
            from mcp_server.tools.roadmap import _load_roadmap
            from mcp_server.paths import get_project_root
            roadmap = _load_roadmap()
            current_phase = roadmap.get("current_phase") or {}
            drift = check_drift(
                project_root=get_project_root(),
                current_phase=current_phase,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("ai_promotion: drift check failed: %s", e)
            drift = None

        # If we have NOTHING to inject (no stable, no rules, no drift)
        # don't bother sending an empty block.
        if not stable and not rules and not drift:
            return PolicyVerdict.allow()

        context = _format_inject(stable, rules, drift=drift)
        return PolicyVerdict(
            action="inject",
            inject_context=context,
            policy=self.name,
            metadata={
                "stable_count": len(stable),
                "rules_count": len(rules),
                "drift_detected": bool(drift),
                "since_days": _INJECT_SINCE_DAYS,
                "min_score": config["min_score"],
            },
        )
