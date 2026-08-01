"""
score.py — the ranking primitives every codevira scorer shares.

Each function here has ONE definition and a stated range, so a score from
one surface means the same thing as a score from another. See the package
docstring for why this is primitives-only rather than a single scorer.

Every primitive returns a value in ``[0.0, 1.0]``. That is the contract —
raw BM25 is unbounded and negative and therefore cannot be mixed with
anything, which is exactly how three incomparable scales arose.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

#: Half-life for recency, in days. A skill or decision untouched for this
#: long scores 0.5 on the recency term.
DEFAULT_HALF_LIFE_DAYS = 90.0

#: Multipliers for a decision's git-derived outcome. A reverted decision
#: is down-ranked rather than hidden: it is still real memory, and "we
#: tried this and backed it out" is often the most useful thing to know.
_OUTCOME_WEIGHTS: dict[str, float] = {
    "kept": 1.0,
    "modified": 0.6,
    "reverted": 0.2,
    "archived": 0.0,
}

#: Applied when a decision has no outcome label yet — most of them.
#: Deliberately mid-range: absence of evidence is not evidence of absence.
NO_OUTCOME_WEIGHT = 0.5


def rank_norm(position: int, total: int) -> float:
    """Convert a 0-indexed rank into a bounded [0, 1] score.

    Top hit scores 1.0, last scores just above 0.0. This exists because
    raw BM25 cannot be compared across queries — its magnitude depends on
    corpus statistics — whereas a rank can.

    ``total <= 0`` or a negative position yields 0.0 rather than raising:
    ranking is a hot path and must degrade, not explode.
    """
    if total <= 0 or position < 0:
        return 0.0
    if position >= total:
        return 0.0
    return round(1.0 - (position / total), 4)


def tag_jaccard(query_tokens: Iterable[str], tags: Iterable[str]) -> float:
    """Jaccard overlap between query tokens and an item's tags, in [0, 1].

    Case-insensitive; empty on either side yields 0.0.
    """
    q = {str(t).strip().lower() for t in query_tokens if str(t).strip()}
    a = {str(t).strip().lower() for t in tags if str(t).strip()}
    if not q or not a:
        return 0.0
    union = q | a
    return round(len(q & a) / len(union), 4) if union else 0.0


def recency_decay(
    timestamp: str | float | None,
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    tau_days: float | None = None,
) -> float:
    """Exponential recency score in [0, 1]: 1.0 now, decaying with age.

    TWO CURVES, named rather than silently chosen. They are not the same
    and picking one by accident would change ranking:

    * ``half_life_days`` (default) — ``0.5`` at one half-life.
    * ``tau_days`` — ``exp(-Δ/τ)``, i.e. ``1/e ≈ 0.368`` at τ. This is
      what ``skills_store`` has always used; it is passed explicitly so
      that consolidating onto this function is arithmetically identical
      rather than a quiet behaviour change.

    Which curve is *right* is a real question, and the honest answer is
    that nobody has measured it. Until someone does, both stay named and
    each caller keeps the one it shipped with.

    Accepts an ISO-8601 string or epoch seconds. Anything unparseable
    returns 0.0 — an unknown age must not score as "fresh", because that
    would let a malformed record outrank a real one.
    """
    if timestamp is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    try:
        if isinstance(timestamp, (int, float)):
            then = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        else:
            s = str(timestamp).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            then = datetime.fromisoformat(s)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return 0.0

    age_days = (now - then).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    if tau_days is not None:
        if tau_days <= 0:
            return 0.0
        return math.exp(-age_days / tau_days)
    if half_life_days <= 0:
        return 0.0
    return round(math.exp(-age_days * math.log(2) / half_life_days), 4)


def outcome_weight(outcome: Any) -> float:
    """Multiplier for a decision's git-derived outcome, in [0, 1].

    Unknown or absent outcomes get ``NO_OUTCOME_WEIGHT``. Note that
    ``archived`` is the only value that can zero a score outright.
    """
    if not outcome:
        return NO_OUTCOME_WEIGHT
    return _OUTCOME_WEIGHTS.get(str(outcome).strip().lower(), NO_OUTCOME_WEIGHT)
