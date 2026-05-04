"""
promotion_score.py — Hero 10's pure scoring functions.

Reads the existing ``outcomes`` and ``learned_rules`` tables. Writes
nothing. Used by:

  - ``mcp_server/engine/policies/ai_promotion.py`` (the SessionStart inject)
  - ``mcp_server/cli_insights.py`` (the ``codevira insights`` CLI)

The score formula intentionally simple — see ``docs/heroes/10-ai-promotion.md``
for the rationale and the v2.1 evolution path (Bayesian smoothing).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------


def score_decision(*, kept: int, modified: int, reverted: int) -> float:
    """Compute a promotion score from outcome counts.

    score = (kept + 0.5 * modified) / max(total, 1)

    Range [0.0, 1.0]:
      - 1.0 = every outcome was 'kept'
      - 0.5 = mix of modified + reverted (uncertain)
      - 0.0 = every outcome was 'reverted'

    Defensive: negative inputs clamped to 0; non-int coerced.
    """
    k = max(int(kept or 0), 0)
    m = max(int(modified or 0), 0)
    r = max(int(reverted or 0), 0)
    total = k + m + r
    if total == 0:
        return 0.0  # caller filters by min_outcomes; this is just safe
    return (k + 0.5 * m) / total


# ---------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------


# Days lookback bound — clamped on every query to keep `since` predictable.
_MIN_SINCE_DAYS = 1
_MAX_SINCE_DAYS = 365


def _clamp_since_days(value: int | None) -> int:
    if value is None:
        return 30
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 30
    return max(_MIN_SINCE_DAYS, min(v, _MAX_SINCE_DAYS))


def aggregate_decision_outcomes(
    conn: sqlite3.Connection,
    *,
    since_days: int = 30,
    min_outcomes: int = 2,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Aggregate outcomes per decision, joined with decision text.

    Returns a list of dicts with keys:
      ``id``, ``decision``, ``file_path``, ``created_at``, ``locked``,
      ``kept``, ``modified``, ``reverted``, ``total``, ``score``.

    Decisions with fewer than ``min_outcomes`` total outcomes are filtered
    OUT — they have insufficient signal to score reliably.

    Sort: by score DESC, then total DESC (more outcomes = stronger signal
    when scores tie), then created_at DESC (recency tiebreaker).

    Returns ``[]`` on any SQL error (corrupted DB, missing table). Never
    raises — Hero 10 is advisory; data layer flakiness must not break
    SessionStart.
    """
    since_days = _clamp_since_days(since_days)
    limit = max(1, min(int(limit), 1000))
    min_outcomes = max(0, int(min_outcomes))

    sql = """
        SELECT
            d.id              AS id,
            d.decision        AS decision,
            d.file_path       AS file_path,
            d.created_at      AS created_at,
            COALESCE(n.do_not_revert, 0) AS locked,
            COUNT(o.id)       AS total,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'kept' THEN 1 ELSE 0 END), 0) AS kept,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'modified' THEN 1 ELSE 0 END), 0) AS modified,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'reverted' THEN 1 ELSE 0 END), 0) AS reverted
        FROM decisions d
        LEFT JOIN outcomes o ON o.decision_id = d.id
        LEFT JOIN nodes    n ON n.file_path   = d.file_path
        WHERE d.created_at >= datetime('now', ?)
        GROUP BY d.id
        HAVING total >= ?
        ORDER BY (kept * 1.0 + modified * 0.5) / MAX(total, 1) DESC,
                 total DESC,
                 d.created_at DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(
            sql,
            (f"-{since_days} days", min_outcomes, limit),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("aggregate_decision_outcomes failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["score"] = score_decision(
            kept=d.get("kept", 0),
            modified=d.get("modified", 0),
            reverted=d.get("reverted", 0),
        )
        out.append(d)
    return out


def top_stable_decisions(
    conn: sqlite3.Connection,
    *,
    since_days: int = 30,
    min_outcomes: int = 2,
    min_score: float = 0.7,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """Decisions whose score is at-or-above ``min_score``.

    Filters from ``aggregate_decision_outcomes`` then truncates to
    ``max_items``. Already sorted by score DESC, so this just slices.
    """
    aggregated = aggregate_decision_outcomes(
        conn, since_days=since_days, min_outcomes=min_outcomes,
        limit=max(int(max_items) * 4, 20),  # over-fetch then filter
    )
    filtered = [d for d in aggregated if d["score"] >= float(min_score)]
    return filtered[: max(1, int(max_items))]


def top_reverted_decisions(
    conn: sqlite3.Connection,
    *,
    since_days: int = 30,
    min_outcomes: int = 2,
    max_score: float = 0.4,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """Decisions whose score is at-or-BELOW ``max_score`` — the
    "AI keeps trying, you keep undoing" cluster.

    Different sort: ascending by score (worst first), then by ``reverted``
    DESC (most reverted first within a score bucket).
    """
    aggregated = aggregate_decision_outcomes(
        conn, since_days=since_days, min_outcomes=min_outcomes,
        limit=max(int(max_items) * 4, 20),
    )
    filtered = [d for d in aggregated if d["score"] <= float(max_score)]
    # Re-sort: low score first, then most reverts.
    filtered.sort(key=lambda d: (d["score"], -d.get("reverted", 0)))
    return filtered[: max(1, int(max_items))]


def top_rules(
    conn: sqlite3.Connection,
    *,
    min_confidence: float = 0.7,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """Top-N learned rules above a confidence threshold.

    Reads ``learned_rules`` table (populated by ``rule_learner``).
    """
    sql = """
        SELECT id, rule_text, confidence, category, file_pattern,
               created_at, updated_at
        FROM learned_rules
        WHERE confidence >= ?
        ORDER BY confidence DESC, updated_at DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(
            sql, (float(min_confidence), max(1, min(int(max_items), 100))),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("top_rules failed: %s", e)
        return []
    return [dict(r) for r in rows]
