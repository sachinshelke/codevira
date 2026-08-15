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

    Unknown or absent outcomes get ``NO_OUTCOME_WEIGHT``.

    ``archived`` is in the table but the outcome classifier never emits it
    — ``indexer/outcome_classifier.py`` returns only kept / modified /
    reverted. The entry is kept because the mapping is data, not control
    flow, and a future archiver would land on it; nothing reaches it today.
    """
    if not outcome:
        return NO_OUTCOME_WEIGHT
    return _OUTCOME_WEIGHTS.get(str(outcome).strip().lower(), NO_OUTCOME_WEIGHT)


#: Recency score for a decision whose timestamp is missing or unparseable.
#: NOT 0.0: ``recency_decay`` returns 0.0 there so a malformed record cannot
#: masquerade as fresh, which is right when recency is the whole score. As a
#: *factor* it would instead zero the decision out of the ranking entirely —
#: a missing ``ts`` would hide a real decision rather than merely fail to
#: promote it. Neutral is the safe reading of "we don't know how old this is".
UNKNOWN_AGE_RECENCY = 0.5


def freshness(
    outcome: Any,
    timestamp: str | float | None,
    *,
    now: datetime,
    dnr_soft_expired: bool = False,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    weights: dict[str, float] | None = None,
    no_outcome: float = NO_OUTCOME_WEIGHT,
) -> float:
    """How much should reality's verdict on a decision promote it? ``[0, 1]``.

    ``outcome_weight`` × ``recency_decay`` — the composite every staleness
    read-side surface needs (Phase 26), in one place so ``get_session_context``,
    ``search`` and the relevance-injection hook cannot drift apart.

    ``now`` is REQUIRED and keyword-only, deliberately — unlike
    ``recency_decay``, which defaults it and re-samples the clock. One instant
    must cover a whole comparison: sampling per row makes rows evaluated later
    score microseconds younger, so the result becomes a function of evaluation
    order rather than of the data. That exact bug shipped once. Having no
    default makes it unrepresentable at every call site rather than merely
    discouraged.

    ``dnr_soft_expired`` halves the score: a ``do_not_revert`` lock nobody has
    re-confirmed past the soft-expire threshold is weaker evidence than a
    current one.

    ``weights`` / ``no_outcome`` override the outcome table for callers with a
    *stated* reason to disagree — see ``SESSION_BRIEF_OUTCOME_WEIGHTS`` and
    ``SESSION_BRIEF_NO_OUTCOME``. Absent, the shared table applies, so surfaces
    agree by default. They exist so consolidating an already-shipped scorer onto
    this function is arithmetically identical rather than a quiet re-tune.
    """
    table = _OUTCOME_WEIGHTS if weights is None else weights
    key = str(outcome).strip().lower() if outcome else ""
    confidence = table.get(key, no_outcome) if key else no_outcome
    if dnr_soft_expired:
        confidence *= 0.5
    # ABSENT timestamp -> neutral. PRESENT but ancient or unparseable -> whatever
    # recency_decay says, including 0.0.
    #
    # An earlier cut rewrote a 0.0 from recency_decay back to neutral, reasoning
    # that 0.0 meant "unparseable". It cannot: recency_decay returns 0.0 for a
    # parse failure AND for anything old enough to round to zero. Conflating
    # them promoted a 2020 decision to the same recency as one written today —
    # measured, not hypothesised. Scoring a corrupt timestamp lowest is the safe
    # direction: the row still returns, it just ranks last.
    recency = (
        recency_decay(timestamp, now=now, half_life_days=half_life_days)
        if timestamp
        else UNKNOWN_AGE_RECENCY
    )
    return confidence * recency


#: The catch-up brief disagrees with the shared table on ``modified``,
#: deliberately. ``modified`` means the tracker saw the decision's file change
#: underneath it. In a *brief* that is a liability — it is the churn signal that
#: earns ``needs_review``, so it should sink below an untested decision. In a
#: *search* the same decision is a strong hit: you asked about this area and
#: someone touched it recently, so the shared 0.6 is right there. Same
#: arithmetic, two stated policies, rather than one number that fits neither.
#:
#: ``reverted`` is absent because the brief FILTERS those rows out before
#: ranking (``_is_stale``); it never reaches the table.
SESSION_BRIEF_OUTCOME_WEIGHTS: dict[str, float] = {
    "kept": 1.0,
    "modified": 0.4,
}

#: The brief's unobserved weight. Higher than the shared 0.5: a brief is a
#: short list where an untested decision is still worth surfacing, whereas in a
#: ranked search it competes against hits with real evidence behind them.
SESSION_BRIEF_NO_OUTCOME = 0.7
