"""Eval orchestration + reporting — E3 (Phase 21). NON-GATING.

Runs the read-side relevance sweep, scores precision (LLM judge when a
sampling client is wired, else the lexical proxy), prints a compact report,
and appends the headline metrics to a per-machine trend log so quality can
be watched over time. Nothing here fails a build — it's a quality signal,
not a gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from mcp_server.eval import judge as judge_mod
from mcp_server.eval import relevance


def run_eval(
    *,
    k: int = relevance.DEFAULT_K,
    max_cases: int = relevance.DEFAULT_MAX_CASES,
    ask: Callable[[str], str] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the read-side relevance eval and return a structured result.

    ``decisions`` overrides the corpus (test seam); otherwise the canonical
    JSONL store is read (D000002). ``ask`` enables the LLM judge; absent it,
    the deterministic lexical proxy is used and ``judge_mode='lexical'``.
    """
    if decisions is None:
        from mcp_server.storage import decisions_store

        page = decisions_store.list_all(limit=max_cases * 3, full=True)
        decisions = page.get("decisions", [])

    cases = relevance.build_cases(decisions, max_cases=max_cases)
    results = relevance.run_recall(cases, k=k)

    used_llm = judge_mod.score_llm(results, ask)
    if not used_llm:
        judge_mod.score_lexical(results)

    report = relevance.summarize(results, k=k)
    return {
        "metrics": report.to_dict(),
        "judge_mode": "llm" if used_llm else "lexical",
        "report": report,
        "n_decisions_scanned": len(decisions),
        "n_cases": len(cases),
    }


def format_report(result: dict[str, Any]) -> str:
    m = result["metrics"]
    report = result["report"]
    lines = [
        "Codevira read-side relevance eval (E3)",
        "─" * 56,
        f"corpus:        {result['n_decisions_scanned']} decision(s) "
        f"→ {result['n_cases']} self-derived case(s)",
        f"judge:         {result['judge_mode']}",
        f"recall@{m['k']}:      {m['recall_at_k']:.1%}  "
        f"(target decision surfaced in top-{m['k']})",
        f"MRR:           {m['mrr']:.3f}  (1.0 = always rank #1)",
    ]
    if m["mean_precision"] is not None:
        lines.append(
            f"precision@{m['k']}:   {m['mean_precision']:.1%}  "
            f"(share of top-{m['k']} that are relevant — higher = less noise)"
        )
    # Surface the worst few (buried or missed) — these are the read-side gaps.
    misses = [r for r in report.results if not r.hit]
    buried = sorted(
        (r for r in report.results if r.rank and r.rank > 1),
        key=lambda r: -r.rank,
    )
    lines.append("─" * 56)
    if misses:
        lines.append(
            f"missed ({len(misses)}): target not in top-{m['k']} for queries like:"
        )
        for r in misses[:3]:
            lines.append(f"  · {r.case.decision_id}  q={r.case.query!r}")
    if buried[:3]:
        lines.append("buried (relevant but low rank):")
        for r in buried[:3]:
            lines.append(f"  · {r.case.decision_id}  rank={r.rank}  q={r.case.query!r}")
    if not misses and not buried:
        lines.append("clean: every target surfaced at rank #1.")
    return "\n".join(lines)


def append_trend(result: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Append headline metrics to ``.codevira-cache/eval/relevance.jsonl``
    (per-machine, gitignored). Best-effort: never raises."""
    try:
        from mcp_server.paths import get_project_root

        out_dir = get_project_root() / ".codevira-cache" / "eval"
        out_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            **result["metrics"],
            "judge_mode": result["judge_mode"],
            "n_cases": result["n_cases"],
        }
        with (out_dir / "relevance.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception:  # noqa: BLE001 — trend logging never breaks the eval
        return False
