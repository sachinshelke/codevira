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


class TestFreshness:
    """Phase 26's composite: outcome-confidence x recency, shared by
    get_session_context, search and the relevance-injection hook."""

    def _ts(self, days_ago: float) -> str:
        return (NOW - timedelta(days=days_ago)).isoformat()

    def test_kept_beats_unobserved_beats_reverted_at_equal_age(self) -> None:
        t = self._ts(10)
        kept = score.freshness("kept", t, now=NOW)
        unobserved = score.freshness(None, t, now=NOW)
        reverted = score.freshness("reverted", t, now=NOW)
        assert kept > unobserved > reverted

    def test_recent_beats_old_at_equal_outcome(self) -> None:
        assert score.freshness("kept", self._ts(1), now=NOW) > score.freshness(
            "kept", self._ts(400), now=NOW
        )

    def test_half_life_is_ninety_days(self) -> None:
        full = score.freshness("kept", self._ts(0), now=NOW)
        half = score.freshness("kept", self._ts(90), now=NOW)
        assert abs(half / full - 0.5) < 0.01, (full, half)

    def test_soft_expired_lock_is_halved(self) -> None:
        t = self._ts(10)
        assert (
            abs(
                score.freshness("kept", t, now=NOW, dnr_soft_expired=True)
                - score.freshness("kept", t, now=NOW) * 0.5
            )
            < 1e-9
        )

    def test_absent_timestamp_is_neutral(self) -> None:
        """No ts at all is 'we don't know how old this is', not 'ancient'."""
        assert score.freshness("kept", None, now=NOW) == pytest.approx(
            1.0 * score.UNKNOWN_AGE_RECENCY
        )

    def test_an_ancient_decision_is_not_rescued_to_neutral(self) -> None:
        """The bug this asserts against: an earlier cut treated recency_decay's
        0.0 as 'unparseable' and rewrote it to neutral 0.5, which promoted a
        2020 decision to the same recency as one written today. A present
        timestamp must be taken at face value however old it is.
        """
        ancient = score.freshness("modified", self._ts(2400), now=NOW)
        fresh_untested = score.freshness(None, self._ts(0), now=NOW)
        assert ancient < 0.01, ancient
        assert ancient < fresh_untested

    def test_bounded_like_every_other_primitive(self) -> None:
        vals = [
            score.freshness(o, self._ts(d), now=NOW, dnr_soft_expired=x)
            for o in (None, "kept", "modified", "reverted", "archived", "bogus")
            for d in (0, 1, 90, 10_000)
            for x in (False, True)
        ]
        assert all(0.0 <= v <= 1.0 for v in vals), [v for v in vals if not 0 <= v <= 1]

    def test_session_brief_override_reproduces_the_shipped_brief_exactly(self) -> None:
        """The consolidation must be arithmetically identical to the _rank that
        shipped in 4.1.0, or it is a silent re-tune of a documented surface.

        Shipped table: kept 1.0, modified 0.4, anything else 0.7; x0.5 when
        dnr_soft_expired; recency 0.5 ** (age/90); unknown age -> 0.5.
        """

        def shipped(outcome, days, dnr=False):
            confidence = {"kept": 1.0, "modified": 0.4}.get(str(outcome or ""), 0.7)
            if dnr:
                confidence *= 0.5
            recency = 0.5 ** (days / 90.0) if days is not None else 0.5
            return confidence * recency

        for outcome in (None, "kept", "modified", "reverted", "weird"):
            for days in (0, 3, 90, 365):
                for dnr in (False, True):
                    got = score.freshness(
                        outcome,
                        self._ts(days),
                        now=NOW,
                        dnr_soft_expired=dnr,
                        weights=score.SESSION_BRIEF_OUTCOME_WEIGHTS,
                        no_outcome=score.SESSION_BRIEF_NO_OUTCOME,
                    )
                    assert got == pytest.approx(
                        shipped(outcome, days, dnr), abs=1e-3
                    ), (
                        outcome,
                        days,
                        dnr,
                        got,
                        shipped(outcome, days, dnr),
                    )

    def test_search_and_brief_disagree_on_modified_on_purpose(self) -> None:
        """Churn sinks in a catch-up brief and lifts in a search — the one
        deliberate divergence, asserted so it cannot be 'tidied' away."""
        t = self._ts(10)
        brief_mod = score.freshness(
            "modified",
            t,
            now=NOW,
            weights=score.SESSION_BRIEF_OUTCOME_WEIGHTS,
            no_outcome=score.SESSION_BRIEF_NO_OUTCOME,
        )
        brief_none = score.freshness(
            None,
            t,
            now=NOW,
            weights=score.SESSION_BRIEF_OUTCOME_WEIGHTS,
            no_outcome=score.SESSION_BRIEF_NO_OUTCOME,
        )
        assert brief_mod < brief_none, "brief: churn must sink below untested"
        assert score.freshness("modified", t, now=NOW) > score.freshness(
            None, t, now=NOW
        ), "search: churn must lift above untested"


class TestEveryPrimitiveIsBounded:
    def test_the_contract_that_makes_scores_comparable(self) -> None:
        vals = [
            score.rank_norm(3, 10),
            score.tag_jaccard(["a", "b"], ["b", "c"]),
            score.recency_decay((NOW - timedelta(days=14)).isoformat(), now=NOW),
            score.outcome_weight("modified"),
            score.freshness("kept", (NOW - timedelta(days=14)).isoformat(), now=NOW),
        ]
        assert all(0.0 <= v <= 1.0 for v in vals), vals
