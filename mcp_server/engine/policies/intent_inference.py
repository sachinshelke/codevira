"""
intent_inference.py — Hero 9: Proactive Intent Inference policy.

Fires on USER_PROMPT_SUBMIT. Classifies the user's intent (regex-based),
extracts file mentions, and pre-fetches the signals THAT intent needs
into a single inject — so the AI's first turn already has the context
it would otherwise burn 3-5 round-trips fetching.

Differs from Hero 5 (Cross-Session Consistency):
  - Hero 5 surfaces past *decisions* via keyword search across all
    decisions in the project.
  - Hero 9 surfaces *intent-specific context* — fixes for fix-bug,
    impact for refactor, outcomes for add-feature.
  - They COMPLEMENT: both inject contexts concatenate via the engine's
    verdict combiner, sorted by priority (Hero 5 = 30 > Hero 9 = 20).

See ``docs/heroes/09-intent-inference.md`` for spec.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.intent_classifier import (
    classify_intent, extract_file_mentions,
    INTENT_FIX_BUG, INTENT_ADD_FEATURE, INTENT_REFACTOR,
    INTENT_EXPLAIN, INTENT_TEST, INTENT_DOCS, INTENT_OTHER,
)
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Defaults + bounds
# ---------------------------------------------------------------------

_DEFAULT_MODE = "inject"
_MODES = ("off", "inject")

_DEFAULT_MAX_FILES = 3
_MAX_FILES_FLOOR = 1
_MAX_FILES_CEIL = 10

_DEFAULT_MIN_PROMPT_CHARS = 10  # matches Hero 5

_DEFAULT_MAX_FIXES_PER_FILE = 3
_DEFAULT_MAX_DECISIONS_PER_FILE = 3
_DEFAULT_MAX_OUTCOMES = 3

#: Intents that DON'T get an inject (Hero 5 + others handle these).
_NO_INJECT_INTENTS: frozenset[str] = frozenset({INTENT_TEST, INTENT_DOCS})

#: Truncate decision/fix snippets in inject so context block stays small.
_TEXT_DISPLAY_CHARS = 100


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _coerce_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def _coerce_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _truncate(text: str, n: int = _TEXT_DISPLAY_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# ---------------------------------------------------------------------
# Per-intent signal fetcher
# ---------------------------------------------------------------------


def _fetch_signals_for_intent(
    *,
    intent: str,
    file_mentions: list[str],
    project_root: Path,
    signals: SignalContext,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Fetch the signals that this intent benefits from.

    Returns a dict shaped:
      {
        "fixes":     {file: [fix_record, ...]},   # only for fix-bug
        "decisions": {file: [decision_row, ...]}, # for fix-bug, refactor, explain, other
        "impact":    {file: impact_dict},         # only for fix-bug, refactor (if include_impact)
        "outcomes":  [aggregated_decision_row],   # only for add-feature, explain
      }

    Each signal call is wrapped in try/except — failures yield empty
    sub-results, never propagate. Hero 9 is advisory; data flakiness
    must not break UserPromptSubmit dispatch.
    """
    out: dict[str, Any] = {
        "fixes": {}, "decisions": {}, "impact": {}, "outcomes": [],
    }

    # --- Per-file signals ---
    if intent in (INTENT_FIX_BUG, INTENT_REFACTOR, INTENT_EXPLAIN, INTENT_OTHER):
        for file_str in file_mentions:
            try:
                # Resolve to absolute path (project_root + file_str).
                # The signals impl handles both absolute and project-relative.
                abs_path = (project_root / file_str).resolve()
            except (OSError, ValueError):
                continue

            # decisions(file=...)
            try:
                decs = signals.decisions(
                    file=file_str,
                    limit=config["max_decisions_per_file"],
                )
                if decs:
                    out["decisions"][file_str] = decs
            except Exception as e:  # noqa: BLE001
                logger.debug("intent_inference: decisions(%s) failed: %s", file_str, e)

            # fixes(file) — only for fix-bug
            if intent == INTENT_FIX_BUG:
                try:
                    fxs = signals.fixes(abs_path)
                    if fxs:
                        # Cap per-file fix count
                        out["fixes"][file_str] = fxs[: config["max_fixes_per_file"]]
                except Exception as e:  # noqa: BLE001
                    logger.debug("intent_inference: fixes(%s) failed: %s", file_str, e)

            # impact(file) — only for fix-bug, refactor (and only if enabled)
            if intent in (INTENT_FIX_BUG, INTENT_REFACTOR) and config["include_impact"]:
                try:
                    imp = signals.impact(abs_path)
                    if imp:
                        out["impact"][file_str] = imp
                except Exception as e:  # noqa: BLE001
                    logger.debug("intent_inference: impact(%s) failed: %s", file_str, e)

    # --- Project-wide signals ---
    if intent in (INTENT_ADD_FEATURE, INTENT_EXPLAIN):
        try:
            outs = signals.outcomes(
                since_days=30,
                min_outcomes=2,
                limit=20,
            )
            # Pick top-N stable
            stable = [o for o in outs if o.get("score", 0.0) >= 0.7]
            out["outcomes"] = stable[: config["max_outcomes"]]
        except Exception as e:  # noqa: BLE001
            logger.debug("intent_inference: outcomes() failed: %s", e)

    return out


# ---------------------------------------------------------------------
# Inject formatter — pure; no I/O
# ---------------------------------------------------------------------


def _format_inject(
    *,
    intent: str,
    file_mentions: list[str],
    fetched: dict[str, Any],
) -> str:
    """Build the inject context. Returns empty string if nothing to surface."""
    sections: list[str] = []
    sections.append(f"## Codevira pre-fetch — intent: {intent}")
    sections.append("")
    if file_mentions:
        sections.append(f"Files mentioned: {', '.join(file_mentions)}")
        sections.append("")

    # Recent fixes (fix-bug only)
    fixes = fetched.get("fixes", {})
    if fixes:
        sections.append("### Recent fixes touching this area:")
        for file_str, fix_list in fixes.items():
            for fx in fix_list:
                desc = _truncate(str(fx.get("description") or ""))
                date = str(fx.get("commit_date") or fx.get("recorded_at") or "")[:10]
                sections.append(f"- {date} ({file_str}): {desc}")
        sections.append("")

    # Related decisions
    decisions = fetched.get("decisions", {})
    if decisions:
        sections.append("### Related decisions:")
        for file_str, dec_list in decisions.items():
            for d in dec_list:
                text = _truncate(str(d.get("decision") or ""))
                date = str(d.get("timestamp") or d.get("created_at") or "")[:10]
                sections.append(f"- {date} ({file_str}): {text}")
        sections.append("")

    # Blast radius
    impact = fetched.get("impact", {})
    if impact:
        sections.append("### Blast radius:")
        for file_str, imp in impact.items():
            count = imp.get("affected_count", 0) or len(imp.get("affected_files", []) or [])
            if count:
                sections.append(f"- {file_str}: {count} caller(s) / dependent file(s)")
        sections.append("")

    # Top-stable outcomes (add-feature, explain)
    outcomes = fetched.get("outcomes", [])
    if outcomes:
        sections.append("### Top stable decisions in this project:")
        for o in outcomes:
            file_str = o.get("file_path") or "(unknown)"
            text = _truncate(str(o.get("decision") or ""))
            score = o.get("score", 0.0)
            sections.append(f"- {file_str}: \"{text}\" (score {score:.2f})")
        sections.append("")

    # Trailing line
    if len(sections) <= 4:
        # Only the header was added — no actual content.
        return ""

    sections.append(
        "Use this context to avoid round-trips. If the prompt's intent "
        "looks miscategorized, ignore this section."
    )
    return "\n".join(sections)


# ---------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------


class ProactiveIntentInference(Policy):
    """Pre-fetch intent-specific context on UserPromptSubmit.

    Verdict shapes:
      - allow  : non-prompt event, mode=off, no signals, prompt too short,
                 intent in {test, docs} (handled by other policies),
                 or no signals returned anything.
      - inject : a tailored context block based on the inferred intent.

    Never blocks. Never warns. Strictly advisory.
    """

    name = "intent_inference"
    handles = (EventType.USER_PROMPT_SUBMIT,)
    enabled_by_default = True
    priority = 20  # below Hero 5's 30; both inject contexts concatenate

    # ------- config (env-driven) -------

    def _config(self) -> dict[str, Any]:
        mode_raw = (
            os.environ.get("CODEVIRA_INTENT_INFERENCE_MODE", _DEFAULT_MODE) or ""
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {
            "mode": mode,
            "max_files": _coerce_int(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_MAX_FILES"),
                _DEFAULT_MAX_FILES, _MAX_FILES_FLOOR, _MAX_FILES_CEIL,
            ),
            "min_prompt_chars": _coerce_int(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_MIN_PROMPT_CHARS"),
                _DEFAULT_MIN_PROMPT_CHARS, 1, 1000,
            ),
            "max_fixes_per_file": _coerce_int(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_MAX_FIXES_PER_FILE"),
                _DEFAULT_MAX_FIXES_PER_FILE, 1, 50,
            ),
            "max_decisions_per_file": _coerce_int(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_MAX_DECISIONS_PER_FILE"),
                _DEFAULT_MAX_DECISIONS_PER_FILE, 1, 50,
            ),
            "max_outcomes": _coerce_int(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_MAX_OUTCOMES"),
                _DEFAULT_MAX_OUTCOMES, 1, 20,
            ),
            "include_impact": _coerce_bool(
                os.environ.get("CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT"),
                True,
            ),
        }

    def describe(self) -> dict[str, Any]:
        cfg = self._config()
        return {
            "name": self.name,
            "priority": self.priority,
            "handles": [str(h) for h in self.handles],
            "enabled_by_default": self.enabled_by_default,
            **cfg,
        }

    # ------- main entry point -------

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 0: event-type gate
        if event.event_type != EventType.USER_PROMPT_SUBMIT:
            return PolicyVerdict.allow()

        # Stage 1: config / mode gate
        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: prompt sanity gate
        prompt = (event.prompt_text or "").strip()
        if len(prompt) < config["min_prompt_chars"]:
            return PolicyVerdict.allow()

        # Stage 3: signals gate
        if signals is None:
            return PolicyVerdict.allow()

        # Stage 4: classify + extract — pure, no I/O
        intent = classify_intent(prompt)
        if intent in _NO_INJECT_INTENTS:
            # test, docs — let Hero 5 handle decisions on those topics
            return PolicyVerdict.allow()

        file_mentions = extract_file_mentions(
            prompt, max_files=config["max_files"],
        )

        # Stage 5: per-intent signal fetch (each call wrapped in try/except)
        fetched = _fetch_signals_for_intent(
            intent=intent,
            file_mentions=file_mentions,
            project_root=event.project_root,
            signals=signals,
            config=config,
        )

        # Stage 6: format. _format_inject returns "" if no actual content.
        context = _format_inject(
            intent=intent,
            file_mentions=file_mentions,
            fetched=fetched,
        )
        if not context:
            return PolicyVerdict.allow()

        return PolicyVerdict(
            action="inject",
            inject_context=context,
            policy=self.name,
            metadata={
                "intent": intent,
                "file_mentions": file_mentions,
                "fixes_count": sum(len(v) for v in fetched.get("fixes", {}).values()),
                "decisions_count": sum(len(v) for v in fetched.get("decisions", {}).values()),
                "impact_count": len(fetched.get("impact", {})),
                "outcomes_count": len(fetched.get("outcomes", [])),
            },
        )
