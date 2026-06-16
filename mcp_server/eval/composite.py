"""Composite-ranking objective for weight tuning — Phase 13.

The relevance_inject hot path ranks decisions against a prompt with a
hand-tuned weighted sum::

    score = (W_tag·tag_hits + W_file·file_hit + W_fts·fts_falloff) · outcome_weight

Phase 13 LEARNS those weights instead of hard-coding them. To learn them we
need an objective: this module replays the EXACT same scoring (parameterized
by a weight vector) over the E3 self-derived cases, and reports recall@k /
MRR for a given weight vector. The tuner then searches the weight space to
maximize it; the hot path applies the winner deterministically.

Faithful to ``relevance_inject._score_candidates``: tag = W_tag per shared
tag, file = W_file if the decision's file stem appears in the query, fts =
W_fts·0.5^rank over the FTS pool, all × the decision's outcome weight.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from mcp_server.eval.relevance import EvalCase

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")

# The hand-tuned defaults that relevance_inject ships today (the baseline the
# learner must beat before its weights are worth applying).
DEFAULT_WEIGHTS = {"tag": 0.4, "file": 0.4, "fts": 0.2}
POOL_SIZE = 30  # candidate pool per query (FTS top-N) the weights re-rank


def _query_tokens(query: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(query or "")}


def _outcome_weight(decision: dict[str, Any]) -> float:
    """kept=1.0 / modified=0.6 / reverted=0.2 / archived=0.0 / none=0.5,
    mirroring relevance_inject's digest weight. Git-optional: if no outcome
    is recorded the neutral 0.5 is used (same as the hot path)."""
    outcome = (decision.get("outcome") or "").lower()
    return {
        "kept": 1.0,
        "modified": 0.6,
        "reverted": 0.2,
        "archived": 0.0,
    }.get(outcome, float(decision.get("weight", 0.5)))


def composite_score(
    qtokens: set[str],
    decision: dict[str, Any],
    fts_rank: int | None,
    weights: dict[str, float],
) -> float:
    """Replicate relevance_inject scoring for one (query, decision) pair."""
    dtags = {str(t).lower() for t in (decision.get("tags") or [])}
    tag = weights.get("tag", 0.0) * len(qtokens & dtags)

    file = 0.0
    fp = decision.get("file_path")
    if isinstance(fp, str) and fp:
        stem = PurePosixPath(fp).stem.lower()
        if stem and stem in qtokens:
            file = weights.get("file", 0.0)

    fts = weights.get("fts", 0.0) * (0.5**fts_rank) if fts_rank is not None else 0.0

    base = tag + file + fts
    return base * max(_outcome_weight(decision), 0.1)


def build_pools(
    cases: list[EvalCase], *, pool_size: int = POOL_SIZE
) -> list[tuple[EvalCase, set[str], list[dict[str, Any]]]]:
    """Fetch each case's FTS candidate pool ONCE (the pool is weight-
    independent — only the re-ranking depends on the weights). Returns
    ``[(case, query_tokens, pool_records)]`` for fast repeated scoring during
    the grid search."""
    from mcp_server.storage import decisions_store

    pools: list[tuple[EvalCase, set[str], list[dict[str, Any]]]] = []
    for case in cases:
        try:
            pool = decisions_store.search(case.query, limit=pool_size)
        except Exception:  # noqa: BLE001
            continue
        pools.append((case, _query_tokens(case.query), pool))
    return pools


def evaluate_weights(
    cases: list[EvalCase],
    weights: dict[str, float],
    *,
    k: int = 5,
    pool_size: int = POOL_SIZE,
    pools: list[tuple[EvalCase, set[str], list[dict[str, Any]]]] | None = None,
) -> dict[str, float]:
    """Recall@k + MRR of the composite ranking with ``weights``.

    Pass precomputed ``pools`` (from :func:`build_pools`) to score many weight
    vectors without re-querying FTS — this is what makes the grid search fast.
    """
    if pools is None:
        pools = build_pools(cases, pool_size=pool_size)

    hits = 0
    rr_sum = 0.0
    n = len(pools)
    for case, qtokens, pool in pools:
        scored = [
            (composite_score(qtokens, hit, rank, weights), str(hit.get("id")))
            for rank, hit in enumerate(pool)
        ]
        # Stable: score desc, then original FTS order (index) as tiebreak.
        order = sorted(range(len(scored)), key=lambda i: (-scored[i][0], i))
        ranked_ids = [scored[i][1] for i in order]
        if case.decision_id in ranked_ids[:k]:
            hits += 1
            rr_sum += 1.0 / (ranked_ids.index(case.decision_id) + 1)
    return {
        "recall_at_k": (hits / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "n": n,
    }
