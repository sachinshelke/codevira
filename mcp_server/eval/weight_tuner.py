"""Cold-path weight tuner — Phase 13. NO model, deterministic.

Grid-searches the relevance_inject ranking weights ``{tag, file, fts}`` to
maximize the E3 composite objective (recall@k, MRR tiebreak) over cases
self-derived from real ``.codevira/`` memory, then persists the winner ONLY
IF it strictly beats the shipped defaults — so a tune run can never make the
hot path worse, and a no-signal corpus simply keeps the proven defaults.

Runs at cold path (Stop hook, debounced, or ``codevira tune-weights``); the
hot path just reads the persisted vector. The shipped defaults are included
in the grid, so the winner is always ≥ the baseline by construction.
"""

from __future__ import annotations

from typing import Any, Iterator

from mcp_server.eval import composite, relevance

# Grid: each weight over {0.1 … 0.6} (step 0.1). 6³ = 216 combos — includes
# the shipped (0.4, 0.4, 0.2) so the search can only match or beat baseline.
_GRID_VALUES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)

# Conservatism guards so the learner can't ship NOISE to a sensitive hot path:
# need enough cases to be reliable, and a MEANINGFUL win over the defaults
# (a sub-0.02 metric wiggle on a small corpus is overfit, not signal).
_MIN_CASES = 20
_MIN_IMPROVEMENT = 0.02


def _grid() -> Iterator[dict[str, float]]:
    for tag in _GRID_VALUES:
        for file in _GRID_VALUES:
            for fts in _GRID_VALUES:
                yield {"tag": tag, "file": file, "fts": fts}


def _score(metric: dict[str, float]) -> tuple[float, float]:
    return (metric["recall_at_k"], metric["mrr"])


def tune(
    *,
    k: int = 5,
    max_cases: int = relevance.DEFAULT_MAX_CASES,
    decisions: list[dict[str, Any]] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Search the weight grid and (optionally) persist the winner.

    ``decisions`` overrides the corpus (test seam). Returns the best/default
    metrics, whether it improved, and whether it persisted.
    """
    if decisions is None:
        from mcp_server.storage import decisions_store

        decisions = decisions_store.list_all(limit=max_cases * 3, full=True).get(
            "decisions", []
        )
    cases = relevance.build_cases(decisions, max_cases=max_cases)
    if len(cases) < _MIN_CASES:
        return {
            "status": "too_few_cases",
            "n_cases": len(cases),
            "min_cases": _MIN_CASES,
            "improved": False,
            "persisted": False,
        }

    # Pools are weight-independent → fetch once, then score 216 vectors fast.
    pools = composite.build_pools(cases)
    default_metric = composite.evaluate_weights(
        cases, composite.DEFAULT_WEIGHTS, k=k, pools=pools
    )

    best_w = dict(composite.DEFAULT_WEIGHTS)
    best_metric = default_metric
    for weights in _grid():
        m = composite.evaluate_weights(cases, weights, k=k, pools=pools)
        if _score(m) > _score(best_metric):
            best_metric, best_w = m, weights

    # Require a MEANINGFUL win on either axis — a sub-threshold wiggle is noise.
    gain = max(
        best_metric["recall_at_k"] - default_metric["recall_at_k"],
        best_metric["mrr"] - default_metric["mrr"],
    )
    improved = gain >= _MIN_IMPROVEMENT
    persisted = False
    if improved and persist:
        from mcp_server.storage import learned_weights

        persisted = learned_weights.save(
            best_w, metric=best_metric, baseline=default_metric
        )

    return {
        "status": "ok",
        # The actual best vector found (transparent). ``improved`` gates whether
        # it's meaningful enough to apply; only then is it persisted.
        "best_weights": best_w,
        "best_metric": best_metric,
        "default_weights": dict(composite.DEFAULT_WEIGHTS),
        "default_metric": default_metric,
        "improvement": round(gain, 4),
        "improved": improved,
        "persisted": persisted,
        "n_cases": len(cases),
    }
