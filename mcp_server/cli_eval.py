"""``codevira eval`` — read-side relevance eval CLI (E3, Phase 21).

NON-GATING by design: it prints a quality report and appends the headline
metrics to a per-machine trend log, but always exits 0 (unless you opt into
``--gate`` thresholds in CI). The eval cases are self-derived from the
project's own ``.codevira/`` memory, so there's nothing to maintain.

The CLI uses the deterministic lexical relevance proxy (no sampling client
is attached to a plain CLI invocation). The intelligent LLM-as-judge path is
available programmatically via ``mcp_server.eval.run_eval(ask=...)``.
"""

from __future__ import annotations

import sys


def cmd_eval(
    *,
    k: int = 5,
    max_cases: int = 200,
    trend: bool = True,
    min_recall: float | None = None,
) -> int:
    """Run the relevance eval and print the report.

    Returns 0 (non-gating) unless ``min_recall`` is set and recall@k falls
    below it (opt-in CI gate).
    """
    from mcp_server.eval import append_trend, format_report, run_eval

    try:
        result = run_eval(k=k, max_cases=max_cases)
    except Exception as exc:  # noqa: BLE001 — a quality signal must never hard-crash
        sys.stderr.write(f"codevira eval: could not run ({exc}); skipping.\n")
        return 0

    sys.stdout.write(format_report(result) + "\n")
    if trend:
        if append_trend(result):
            sys.stdout.write(
                "\ntrend appended to .codevira-cache/eval/relevance.jsonl\n"
            )

    if min_recall is not None:
        recall = result["metrics"]["recall_at_k"]
        if recall < min_recall:
            sys.stderr.write(
                f"codevira eval: recall@{k} {recall:.1%} < gate {min_recall:.1%}\n"
            )
            return 1
    return 0


def cmd_tune_weights(*, k: int = 5, max_cases: int = 200, apply: bool = True) -> int:
    """``codevira tune-weights`` (Phase 13) — learn relevance_inject weights
    from real memory via the E3 objective; persist only a meaningful win.
    Always exits 0 (cold-path maintenance, never gates)."""
    from mcp_server.eval import weight_tuner

    try:
        res = weight_tuner.tune(k=k, max_cases=max_cases, persist=apply)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"codevira tune-weights: could not run ({exc}).\n")
        return 0

    status = res.get("status")
    if status == "too_few_cases":
        sys.stdout.write(
            f"tune-weights: only {res['n_cases']} case(s) (< {res['min_cases']} "
            f"needed) — keeping shipped defaults.\n"
        )
        return 0

    dm, bm = res["default_metric"], res["best_metric"]
    sys.stdout.write(
        f"tune-weights: {res['n_cases']} cases\n"
        f"  default {res['default_weights']}  "
        f"recall@{k}={dm['recall_at_k']:.1%} mrr={dm['mrr']:.3f}\n"
        f"  best    {res['best_weights']}  "
        f"recall@{k}={bm['recall_at_k']:.1%} mrr={bm['mrr']:.3f}\n"
        f"  improvement: {res['improvement']:+.3f}  "
        f"({'meaningful' if res['improved'] else 'below threshold — keeping defaults'})\n"
    )
    if res["persisted"]:
        sys.stdout.write(
            "  persisted to .codevira/learned_weights.json — enable at the hot "
            "path with CODEVIRA_LEARNED_WEIGHTS=1\n"
        )
    return 0
