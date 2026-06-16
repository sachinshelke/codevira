"""Relevance judging for the read-side eval — E3 (Phase 21).

Scores how many of the top-k results a query surfaced are actually RELEVANT
(the signal-to-noise D00005N cares about). Two paths:

* **lexical** (default, deterministic, CI-safe): a surfaced decision is
  relevant to the case if it shares the source's file, a tag, or ≥2 salient
  topic tokens. Zero dependencies; runs everywhere.
* **LLM-as-judge** (the intelligent path, offline + opt-in): the host LLM is
  asked, per (query, surfaced decision), "is this relevant?". Wired through
  an injected ``ask`` callable so it's testable and never required — mirrors
  how ``reflect`` uses MCP sampling. Absent a sampling client, the harness
  falls back to lexical and says so.

Both only run OFFLINE (eval/CI), so using the LLM here doesn't violate the
hot-path ML-out-of-default identity (D0000PV).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from mcp_server.eval.relevance import CaseResult, _salient

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _salient(text or "", top=50)}


def lexical_relevance(source: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Deterministic proxy: is ``candidate`` relevant to ``source``'s intent?"""
    if not candidate:
        return False
    # Same id → trivially relevant (the target itself).
    if candidate.get("id") and candidate.get("id") == source.get("id"):
        return True
    # Same file.
    sfp, cfp = source.get("file_path"), candidate.get("file_path")
    if sfp and cfp and sfp == cfp:
        return True
    # Shared tag.
    stags = {str(t).lower() for t in (source.get("tags") or [])}
    ctags = {str(t).lower() for t in (candidate.get("tags") or [])}
    if stags & ctags:
        return True
    # ≥2 shared salient tokens across decision text.
    shared = _tokens(source.get("decision") or "") & _tokens(
        candidate.get("decision") or ""
    )
    return len(shared) >= 2


def _load(decision_id: str, cache: dict[str, dict | None]) -> dict | None:
    if decision_id not in cache:
        from mcp_server.storage import decisions_store

        try:
            cache[decision_id] = decisions_store.get(decision_id)
        except Exception:  # noqa: BLE001
            cache[decision_id] = None
    return cache[decision_id]


def score_lexical(results: list[CaseResult]) -> None:
    """Fill ``relevant_in_topk`` on each result using the lexical proxy."""
    cache: dict[str, dict | None] = {}
    for r in results:
        relevant = 0
        for did in r.returned_ids:
            cand = _load(did, cache)
            if cand and lexical_relevance(r.case.source, cand):
                relevant += 1
        r.relevant_in_topk = relevant


def build_judge_prompt(query: str, candidates: list[dict[str, Any]]) -> str:
    """One compact yes/no-per-line prompt for the LLM judge."""
    lines = [
        "You are judging search relevance for an AI coding agent's memory.",
        f'The agent searched for: "{query}"',
        "For EACH numbered decision below, answer relevant (y) or not (n) — "
        "is it worth surfacing to an agent working on that query?",
        "Reply with one line per item as `<n>: y` or `<n>: n`. Nothing else.",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        text = (c.get("decision") or "")[:200]
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def _parse_verdicts(text: str, n: int) -> list[bool]:
    verdicts = [False] * n
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d+)\s*[:.\)]\s*([yn])", line.strip(), re.IGNORECASE)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n:
                verdicts[idx] = m.group(2).lower() == "y"
    return verdicts


def score_llm(
    results: list[CaseResult],
    ask: Callable[[str], str] | None,
) -> bool:
    """Score precision via an LLM judge. ``ask`` takes a prompt → model text.

    Returns True if the LLM path ran, False if it was unavailable (``ask is
    None``) — the caller then keeps/uses the lexical scores. Per-case failures
    fall back to the lexical proxy for that case so one bad call never voids
    the sweep.
    """
    if ask is None:
        return False
    cache: dict[str, dict | None] = {}
    for r in results:
        candidates = [c for c in (_load(did, cache) for did in r.returned_ids) if c]
        if not candidates:
            r.relevant_in_topk = 0
            continue
        try:
            verdicts = _parse_verdicts(
                ask(build_judge_prompt(r.case.query, candidates)), len(candidates)
            )
            r.relevant_in_topk = sum(1 for v in verdicts if v)
        except Exception:  # noqa: BLE001 — fall back to lexical for this case
            r.relevant_in_topk = sum(
                1 for c in candidates if lexical_relevance(r.case.source, c)
            )
    return True
