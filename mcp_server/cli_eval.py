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
