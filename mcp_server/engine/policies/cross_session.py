"""
cross_session.py — Hero 5: Cross-Session Consistency policy.

Fires on USER_PROMPT_SUBMIT. Extracts content keywords from the user's
prompt, searches codevira's decisions database for matching prior
decisions, and INJECTS the matches into the AI's context as
``additionalContext`` so the AI is reminded of past architectural
choices before responding.

Different shape than Hero 1 / Hero 4 — this is the first policy to
use the engine's ``inject`` verdict path. The wiring layer
(``mcp_server/engine/wiring/claude_code_hooks.py``) already handles
inject correctly (caught + fixed during Week-1 R5 schema verification).

See ``docs/heroes/05-cross-session.md`` for the spec — decision tree,
keyword extraction rules, edge cases, and acceptance scenarios.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "inject"
_DEFAULT_MAX_INJECT = 5
_MODES = ("off", "inject")

#: Min prompt length to bother searching. Shorter prompts (e.g. "ok",
#: "thanks", "got it") rarely have content keywords worth surfacing on,
#: and the SQL-query overhead isn't justified.
_MIN_PROMPT_CHARS = 10

#: Min token length after lowercasing+stripping.  3-letter words
#: like "api", "css", "sql" are useful; 1-2 letter tokens are usually
#: noise (variable names, single letters, numbers).
_MIN_TOKEN_CHARS = 3

#: Cap on distinct keywords we extract per prompt. Each keyword
#: triggers one SQL search; capping bounds the SQL load and keeps
#: latency predictable on long prompts.
_MAX_KEYWORDS = 5

#: Per-keyword search limit. Each search returns up to N decisions;
#: dedup + recency-sort happens across all results.
_PER_KEYWORD_SEARCH_LIMIT = 3


# ---------------------------------------------------------------------
# Stop-words list (small + conservative)
# ---------------------------------------------------------------------

# Common English stop-words. Kept short — false negatives (a relevant
# stop-word missed) are far more costly than false positives (a
# non-stop-word triggering an extra SQL query). Lowercase comparison.
_STOP_WORDS: frozenset[str] = frozenset({
    # Articles + determiners
    "the", "a", "an", "this", "that", "these", "those",
    # Pronouns
    "i", "we", "you", "he", "she", "it", "they", "me", "us",
    "him", "her", "them", "my", "our", "your", "his", "their", "its",
    # Be / have / do
    "is", "are", "was", "were", "be", "been", "being", "am",
    "has", "have", "had", "having",
    "do", "does", "did", "doing", "done",
    # Modals
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "ought",
    # Prepositions
    "of", "in", "on", "at", "by", "for", "with", "from", "to", "into",
    "onto", "upon", "about", "above", "below", "under", "over",
    "between", "among", "through", "during", "before", "after",
    # Conjunctions / common functional words
    "and", "or", "but", "nor", "so", "yet", "if", "then", "else",
    "when", "where", "why", "how", "what", "who", "whom", "whose",
    "which", "while", "because", "since", "though", "although",
    "unless", "until",
    # Verbs / common imperative starters in prompts
    "let", "lets", "make", "makes", "made", "use", "uses", "using",
    "used", "add", "adds", "added", "adding",
    "get", "gets", "got", "getting",
    "set", "sets", "setting",
    "want", "wants", "wanted", "need", "needs", "needed",
    # Misc filler
    "very", "just", "only", "also", "even", "still", "more", "most",
    "less", "least", "much", "many", "any", "some", "all", "each",
    "every", "such", "same", "other", "another", "both",
    "yes", "no", "not", "ok", "okay",
    "thanks", "please",
})


# ---------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------

#: Tokenizer regex: identifier-ish runs (letters, digits, underscores,
#: hyphens, dots — but NOT slashes, since those are path separators
#: that we want as token boundaries). Conservative — favors recall.
_TOKEN_RE = re.compile(r"[A-Za-z][\w.\-]{1,}")


def _extract_keywords(prompt: str) -> list[str]:
    """Pure function — extract content keywords from a user prompt.

    Returns up to ``_MAX_KEYWORDS`` distinct lowercase tokens, in
    order of first appearance. Filters: stop-words, < 3 chars, all-
    digit, pure-punctuation.
    """
    if not prompt:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _TOKEN_RE.finditer(prompt):
        token = match.group(0).lower()
        if len(token) < _MIN_TOKEN_CHARS:
            continue
        if token.isdigit():
            continue
        if token in _STOP_WORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= _MAX_KEYWORDS:
            break
    return out


# ---------------------------------------------------------------------
# Match collection + dedup
# ---------------------------------------------------------------------


def _match_key(decision: dict[str, Any]) -> tuple:
    """Stable dedup key for a decision row. Two decisions with the
    same (decision_text, file_path) collapse to one — they're the
    same entry surfaced by different keywords.
    """
    return (
        (decision.get("decision") or "").strip(),
        (decision.get("file_path") or "").strip(),
    )


def _collect_matches(
    signals: SignalContext,
    keywords: list[str],
    *,
    max_per_keyword: int = _PER_KEYWORD_SEARCH_LIMIT,
    total_cap: int = _DEFAULT_MAX_INJECT,
) -> list[dict[str, Any]]:
    """Search ``signals.search_decisions`` for each keyword, dedup,
    sort by recency. Returns up to ``total_cap`` decisions.
    """
    seen: set[tuple] = set()
    collected: list[dict[str, Any]] = []
    for kw in keywords:
        try:
            results = signals.search_decisions(kw, limit=max_per_keyword)
        except Exception:  # noqa: BLE001 — signal layer must never break a policy
            continue
        for d in results:
            k = _match_key(d)
            if k in seen:
                continue
            seen.add(k)
            collected.append(d)

    # Sort by created_at DESC. Decisions without a created_at sink
    # to the bottom (we use ``""`` as a sortable lowest value).
    def _sort_key(d: dict[str, Any]) -> str:
        return str(d.get("created_at") or "")

    collected.sort(key=_sort_key, reverse=True)
    return collected[:total_cap]


# ---------------------------------------------------------------------
# Injection text formatting
# ---------------------------------------------------------------------


def _format_decision_line(idx: int, d: dict[str, Any]) -> str:
    """Format one decision as a single-line entry in the inject context."""
    date = _format_date(d.get("created_at"))
    file_path = (d.get("file_path") or "").strip()
    file_part = f"[{file_path}] " if file_path else ""
    text = (d.get("decision") or "").strip()
    # Cap each entry at ~200 chars to keep the injection token-efficient
    if len(text) > 200:
        text = text[:197] + "..."
    return f"{idx}. {date} — {file_part}{text}"


def _format_date(created_at: Any) -> str:
    """Format created_at as YYYY-MM-DD. Fallback: '????-??-??'."""
    if created_at is None:
        return "????-??-??"
    try:
        if isinstance(created_at, (int, float)):
            return datetime.fromtimestamp(
                float(created_at), tz=timezone.utc,
            ).date().isoformat()
        return datetime.fromisoformat(
            str(created_at).replace("Z", "+00:00"),
        ).date().isoformat()
    except Exception:  # noqa: BLE001
        return "????-??-??"


def _format_injection(matches: list[dict[str, Any]]) -> str:
    """Build the additionalContext payload from the matched decisions."""
    lines = [
        "## Prior decisions you may want to consider",
        "",
        "Based on your prompt, here are recent codevira-tracked "
        "decisions on related topics:",
        "",
    ]
    for i, d in enumerate(matches, start=1):
        lines.append(_format_decision_line(i, d))
    lines.append("")
    lines.append(
        "If your current request conflicts with any of these, surface "
        "the conflict to the user before proceeding."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------


class CrossSessionConsistency(Policy):
    """Inject relevant prior decisions when the user submits a prompt."""

    name = "cross_session_consistency"
    handles = (EventType.USER_PROMPT_SUBMIT,)
    enabled_by_default = True
    # Lower priority than block-class policies — inject is advisory.
    # Composed AFTER blocks decide; only matters if other inject
    # policies are also active.
    priority = 30

    # ---- Configuration ----

    def _config(self) -> dict[str, Any]:
        mode_raw = os.environ.get(
            "CODEVIRA_CROSS_SESSION_MODE", _DEFAULT_MODE,
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE

        max_inject_raw = os.environ.get("CODEVIRA_CROSS_SESSION_MAX_INJECT")
        max_inject = _DEFAULT_MAX_INJECT
        if max_inject_raw:
            try:
                v = int(max_inject_raw)
                if 1 <= v <= 20:
                    max_inject = v
            except (ValueError, TypeError):
                pass  # keep default

        return {"mode": mode, "max_inject": max_inject}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_CROSS_SESSION_MODE",
                "description": "off | inject",
            },
            "max_inject": {
                "type": "integer",
                "default": _DEFAULT_MAX_INJECT,
                "env": "CODEVIRA_CROSS_SESSION_MAX_INJECT",
                "description": "Total decisions to surface across all keywords (clamped 1-20)",
            },
        }

    # ---- Evaluation ----

    def evaluate(
        self, event: HookEvent, signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 1: filter to USER_PROMPT_SUBMIT only
        if event.event_type != EventType.USER_PROMPT_SUBMIT:
            return PolicyVerdict.allow()
        if not event.prompt_text:
            return PolicyVerdict.allow()
        if len(event.prompt_text) < _MIN_PROMPT_CHARS:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        # Stage 2: extract keywords
        keywords = _extract_keywords(event.prompt_text)
        if not keywords:
            return PolicyVerdict.allow()

        # Stage 3: search + dedup
        matches = _collect_matches(
            signals, keywords,
            max_per_keyword=_PER_KEYWORD_SEARCH_LIMIT,
            total_cap=config["max_inject"],
        )
        if not matches:
            return PolicyVerdict.allow()

        # Stage 4: inject
        return PolicyVerdict.inject(
            context=_format_injection(matches),
            metadata={
                "policy": self.name,
                "keywords": keywords,
                "matched_count": len(matches),
                "matched_decision_ids": [
                    m.get("id") for m in matches
                ],
            },
        )
