"""4.0 Step 6 — the shared ranking primitives.

Codevira grew four scoring implementations on three incompatible scales.
Two were removed in the 4.0 subtraction; these primitives give the
remaining three one definition each, with a stated range.

The contract every primitive keeps: the result is in [0, 1]. Raw BM25 is
unbounded and negative, which is exactly how three incomparable scales
arose in the first place.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from mcp_server.retrieval import score


NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestRankNorm:
    def test_top_hit_scores_one(self) -> None:
        assert score.rank_norm(0, 10) == 1.0

    def test_is_monotonically_decreasing(self) -> None:
        vals = [score.rank_norm(i, 10) for i in range(10)]
        assert vals == sorted(vals, reverse=True)

    def test_stays_bounded(self) -> None:
        for i in range(20):
            assert 0.0 <= score.rank_norm(i, 20) <= 1.0

    @pytest.mark.parametrize(
        "pos,total", [(0, 0), (-1, 10), (5, 0), (10, 10), (99, 10)]
    )
    def test_degrades_rather_than_raising(self, pos: int, total: int) -> None:
        """Ranking is a hot path — bad input must not explode."""
        assert score.rank_norm(pos, total) == 0.0


class TestTagJaccard:
    def test_identical_sets_score_one(self) -> None:
        assert score.tag_jaccard(["auth", "db"], ["auth", "db"]) == 1.0

    def test_disjoint_sets_score_zero(self) -> None:
        assert score.tag_jaccard(["auth"], ["cache"]) == 0.0

    def test_half_overlap(self) -> None:
        # {a} ∩ {a,b} = 1 ; {a} ∪ {a,b} = 2
        assert score.tag_jaccard(["a"], ["a", "b"]) == 0.5

    def test_is_case_insensitive(self) -> None:
        assert score.tag_jaccard(["AUTH"], ["auth"]) == 1.0

    def test_empty_side_scores_zero(self) -> None:
        assert score.tag_jaccard([], ["a"]) == 0.0
        assert score.tag_jaccard(["a"], []) == 0.0

    def test_blank_entries_are_ignored(self) -> None:
        assert score.tag_jaccard(["a", "  "], ["a"]) == 1.0


class TestRecencyDecay:
    def test_now_scores_one(self) -> None:
        assert score.recency_decay(NOW.isoformat(), now=NOW) == 1.0

    def test_future_timestamps_clamp_to_one(self) -> None:
        future = (NOW + timedelta(days=5)).isoformat()
        assert score.recency_decay(future, now=NOW) == 1.0

    def test_half_life_curve_halves_at_the_half_life(self) -> None:
        ref = (NOW - timedelta(days=90)).isoformat()
        assert score.recency_decay(ref, now=NOW, half_life_days=90) == pytest.approx(
            0.5, abs=1e-3
        )

    def test_tau_curve_hits_one_over_e_at_tau(self) -> None:
        """The two curves are NOT the same, and that is the point."""
        ref = (NOW - timedelta(days=30)).isoformat()
        assert score.recency_decay(ref, now=NOW, tau_days=30) == pytest.approx(
            1 / math.e, abs=1e-6
        )

    def test_the_two_curves_are_pinned_apart(self) -> None:
        """Guard against someone 'simplifying' one into the other: at the
        same 30-day age they differ by ~13 points, which would silently
        re-rank every skill."""
        ref = (NOW - timedelta(days=30)).isoformat()
        half = score.recency_decay(ref, now=NOW, half_life_days=30)
        tau = score.recency_decay(ref, now=NOW, tau_days=30)
        assert half == pytest.approx(0.5, abs=1e-3)
        assert tau == pytest.approx(0.368, abs=1e-3)
        assert abs(half - tau) > 0.1

    def test_matches_the_inline_formula_skills_store_shipped(self) -> None:
        """Consolidating onto this function must be arithmetically
        identical, not a quiet behaviour change."""
        for days in (0, 1, 7, 30, 90, 365):
            ref = (NOW - timedelta(days=days)).isoformat()
            inline = math.exp(-days / 30.0)
            assert score.recency_decay(ref, now=NOW, tau_days=30) == pytest.approx(
                inline, abs=1e-9
            )

    @pytest.mark.parametrize("bad", [None, "not-a-date", "", "2026-13-45T99:99:99"])
    def test_unparseable_scores_zero_not_fresh(self, bad) -> None:
        """An unknown age must never score as fresh — otherwise a
        malformed record outranks a real one."""
        assert score.recency_decay(bad, now=NOW) == 0.0

    def test_accepts_epoch_seconds(self) -> None:
        assert score.recency_decay(NOW.timestamp(), now=NOW) == 1.0

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        naive = NOW.replace(tzinfo=None).isoformat()
        assert score.recency_decay(naive, now=NOW) == 1.0


class TestOutcomeWeight:
    @pytest.mark.parametrize(
        "outcome,expected",
        [("kept", 1.0), ("modified", 0.6), ("reverted", 0.2), ("archived", 0.0)],
    )
    def test_known_outcomes(self, outcome: str, expected: float) -> None:
        assert score.outcome_weight(outcome) == expected

    def test_absent_outcome_is_mid_range(self) -> None:
        """Most decisions have no outcome label. Absence of evidence is
        not evidence of absence, so it must not be treated as reverted."""
        assert score.outcome_weight(None) == score.NO_OUTCOME_WEIGHT
        assert score.outcome_weight("") == score.NO_OUTCOME_WEIGHT

    def test_unknown_label_falls_back_rather_than_zeroing(self) -> None:
        assert score.outcome_weight("wat") == score.NO_OUTCOME_WEIGHT

    def test_only_archived_can_zero_a_score(self) -> None:
        zeros = [
            k
            for k in ("kept", "modified", "reverted", "archived")
            if score.outcome_weight(k) == 0.0
        ]
        assert zeros == ["archived"]


class TestEveryPrimitiveIsBounded:
    def test_the_contract_that_makes_scores_comparable(self) -> None:
        vals = [
            score.rank_norm(3, 10),
            score.tag_jaccard(["a", "b"], ["b", "c"]),
            score.recency_decay((NOW - timedelta(days=14)).isoformat(), now=NOW),
            score.outcome_weight("modified"),
        ]
        assert all(0.0 <= v <= 1.0 for v in vals), vals
