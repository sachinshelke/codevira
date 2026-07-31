"""4.0 Step 2.2 — a refusal must explain itself.

Before this, `decision_lock`'s block message was an id, the decision text
truncated to 120 chars, and a date. At the product's hero moment — refusing
an edit — codevira showed no reasoning, even though `context` is populated
on ~81% of decisions and was already carried into the policy by
signals.py. The agent had nothing to surface to the user beyond
"something says no".

These tests are RED against the pre-4.0 renderer.
"""

from __future__ import annotations

from typing import Any

from mcp_server.engine.policies.decision_lock import DecisionLock, _clip


def _decision(**over: Any) -> dict[str, Any]:
    d = {
        "id": "D000123",
        "file_path": "src/cache.py",
        "symbol": None,
        "decision": "Do not add a cache layer in front of the invalidation path.",
        "context": (
            "We tried it in March and deploys started serving stale reads for "
            "up to 90 seconds because invalidation races the rollout."
        ),
        "alternatives_considered": [
            "TTL-only cache — simpler, but still stale across a deploy",
            "Write-through cache — correct, but doubles write latency",
        ],
        "would_re_examine_if": "invalidation becomes synchronous with rollout",
        "locked": True,
        "timestamp": "2026-03-11T10:00:00+00:00",
    }
    d.update(over)
    return d


class TestBlockMessageCarriesEvidence:
    def test_block_shows_the_reasoning(self) -> None:
        lines = "\n".join(
            DecisionLock()._sample_lines([_decision()], with_evidence=True)
        )
        assert "#D000123" in lines
        assert "why:" in lines
        assert "stale reads" in lines, "context must reach the refusal"

    def test_block_shows_rejected_alternatives(self) -> None:
        lines = "\n".join(
            DecisionLock()._sample_lines([_decision()], with_evidence=True)
        )
        assert "rejected:" in lines
        assert "TTL-only cache" in lines
        assert "Write-through cache" in lines

    def test_block_shows_the_release_condition(self) -> None:
        """A do_not_revert lock with a stated release condition is a lock
        someone can actually satisfy, rather than a one-way ratchet."""
        lines = "\n".join(
            DecisionLock()._sample_lines([_decision()], with_evidence=True)
        )
        assert "revisit if:" in lines
        assert "synchronous with rollout" in lines

    def test_missing_evidence_degrades_quietly(self) -> None:
        """Legacy decisions have no context/alternatives. They must render
        exactly as before, with no empty 'why:' scaffolding."""
        legacy = _decision(
            context=None, alternatives_considered=None, would_re_examine_if=None
        )
        lines = DecisionLock()._sample_lines([legacy], with_evidence=True)
        assert len(lines) == 1
        assert "why:" not in lines[0]
        assert "rejected:" not in lines[0]


class TestNoticesStayCompact:
    def test_downgrade_notice_omits_evidence(self) -> None:
        """Notices fire far more often than blocks. Padding them trains
        people to skim the block message too."""
        lines = DecisionLock()._sample_lines([_decision()], with_evidence=False)
        assert len(lines) == 1
        assert "why:" not in lines[0]


class TestBounded:
    def test_long_context_is_clipped_on_a_word_boundary(self) -> None:
        long = _decision(context="word " * 400)
        lines = "\n".join(DecisionLock()._sample_lines([long], with_evidence=True))
        why = [ln for ln in lines.splitlines() if "why:" in ln][0]
        assert len(why) < 400
        assert why.endswith("…")

    def test_clip_does_not_split_mid_word(self) -> None:
        assert _clip("alpha beta gamma delta", 14) == "alpha beta…"
        assert _clip("short", 100) == "short"
        assert _clip("", 10) == ""

    def test_only_top_three_decisions_render(self) -> None:
        lines = DecisionLock()._sample_lines(
            [_decision(id=f"D00000{i}") for i in range(6)], with_evidence=True
        )
        assert sum(1 for ln in lines if ln.startswith("  • ")) == 3
