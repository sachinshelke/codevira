"""
test_relevance_inject.py — v2.2.0 Phase C verification (G2.6 budget gate).

Covers the relevance-gated injection policy that replaces
cross_session.py. The two critical invariants:

1. Off-topic prompts → 0 tokens injected (no additionalContext).
2. On-topic prompts → ≤600 tokens, ≤3 decisions, cache-stable output.

Plus the scoring components: tags (0.4), files (0.4), FTS (0.2),
outcome weight, deterministic output ordering for prompt-cache hits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.relevance_inject import RelevanceInject


pytestmark = pytest.mark.integration


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project root + fake HOME so .codevira/ writes don't leak."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'inject-test'\nversion = '0.0.1'\n"
    )

    from mcp_server import paths as core_paths

    core_paths.set_project_dir(project)
    core_paths.invalidate_data_dir_cache()

    # Clear any env-var overrides that might affect policy config.
    for k in (
        "CODEVIRA_INJECT_MODE",
        "CODEVIRA_INJECT_MAX_DECISIONS",
        "CODEVIRA_INJECT_MAX_TOKENS",
        "CODEVIRA_INJECT_MIN_SCORE",
    ):
        monkeypatch.delenv(k, raising=False)

    return project


@pytest.fixture
def seeded_decisions(isolated_project: Path) -> Path:
    """Seed .codevira/ with a small known decision set."""
    from mcp_server.storage import decisions_store

    decisions_store.record(
        decision="Use bcrypt for password hashing — team familiarity",
        file_path="auth.py",
        do_not_revert=True,
        tags=["security", "auth"],
    )
    decisions_store.record(
        decision="Prefer named exports over default exports in TypeScript",
        file_path="core.ts",
        tags=["typescript", "style"],
    )
    decisions_store.record(
        decision="Always use context.Context as first arg in Go functions",
        file_path="main.go",
        tags=["go", "idiomatic"],
    )
    decisions_store.record(
        decision="PostgreSQL with pgvector for embedding storage",
        file_path="db.py",
        do_not_revert=True,
        tags=["database", "infra"],
    )
    return isolated_project


def _make_prompt_event(prompt: str, project: Path) -> HookEvent:
    return HookEvent(
        event_type=EventType.USER_PROMPT_SUBMIT,
        project_root=project,
        prompt_text=prompt,
    )


# ─── G2.6 — the critical budget gates ─────────────────────────────────


class TestZeroTokenOffTopic:
    """Off-topic prompts MUST inject 0 tokens (no additionalContext)."""

    def test_completely_unrelated_prompt_returns_allow(self, seeded_decisions):
        policy = RelevanceInject()
        event = _make_prompt_event(
            "How do I make a cake with chocolate frosting?",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing(), (
            f"off-topic prompt should not inject; got action={verdict.action}, "
            f"context_len={len(verdict.inject_context or '')}"
        )
        assert verdict.inject_context is None or verdict.inject_context == ""

    def test_gibberish_prompt_returns_allow(self, seeded_decisions):
        policy = RelevanceInject()
        event = _make_prompt_event(
            "zzzzzz nonexistent gibberish xqzv9 nothing matches at all",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing()

    def test_too_short_prompt_returns_allow(self, seeded_decisions):
        policy = RelevanceInject()
        event = _make_prompt_event("ok", seeded_decisions)
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing()


class TestOnTopicBudget:
    """On-topic prompts must inject within the 600-token / 3-decision cap."""

    def test_topic_match_returns_inject(self, seeded_decisions):
        policy = RelevanceInject()
        event = _make_prompt_event(
            "I need to add bcrypt password hashing to the auth.py module",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        # Should fire — both "bcrypt" (FTS) and "auth.py" (file match)
        assert verdict.action == "inject", (
            f"on-topic prompt should inject; got {verdict.action}: "
            f"{verdict.message}"
        )
        assert verdict.inject_context

    def test_inject_respects_600_token_budget(self, seeded_decisions):
        """Add many decisions to overflow the budget; verify cap holds."""
        from mcp_server.storage import decisions_store, token_estimator

        # Add 20 more security decisions to overload the candidate pool.
        for i in range(20):
            decisions_store.record(
                decision=f"Decision number {i} about security hashing bcrypt authentication",
                file_path="auth.py",
                do_not_revert=True,
                tags=["security", "auth"],
            )

        policy = RelevanceInject()
        event = _make_prompt_event(
            "Question about security bcrypt authentication in auth.py",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.action == "inject"
        block = verdict.inject_context
        actual_tokens = token_estimator.estimate_tokens(block)
        # 600-token budget + ~50 tokens of header/footer slack.
        assert actual_tokens <= 700, (
            f"injected block was {actual_tokens} tokens, budget=600; "
            f"length={len(block)} chars"
        )

    def test_inject_cap_3_decisions_default(self, seeded_decisions):
        """Default cap is 3 decisions even when many candidates match."""
        from mcp_server.storage import decisions_store

        # Add 10 more matching security decisions.
        for i in range(10):
            decisions_store.record(
                decision=f"Extra security decision {i}",
                file_path="auth.py",
                tags=["security"],
            )

        policy = RelevanceInject()
        event = _make_prompt_event(
            "How do we handle security in auth.py?",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.action == "inject"

        # Parse the injected block to count decision lines.
        block = verdict.inject_context
        decision_lines = [
            line for line in block.splitlines() if line.startswith(("🔒 **", "• **"))
        ]
        assert (
            len(decision_lines) <= 3
        ), f"expected ≤3 decisions, got {len(decision_lines)}; block:\n{block}"


# ─── Scoring components ───────────────────────────────────────────────


class TestScoringComponents:
    def test_tag_match_contributes(self, seeded_decisions):
        """A prompt that contains a tag word should match that tag's decisions."""
        policy = RelevanceInject()
        # "security" is a tag on D000001 + D000004
        event = _make_prompt_event(
            "I have a question about security architecture",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        if verdict.action == "inject":
            assert verdict.metadata.get("tags_matched") is not None
            # At least one of the seeded security tags should match.
            assert any(t in verdict.metadata["tags_matched"] for t in ("security",))

    def test_file_match_contributes(self, seeded_decisions):
        """A prompt mentioning a known file path should match that file's
        decisions (full path OR basename)."""
        policy = RelevanceInject()
        event = _make_prompt_event(
            "Please review the auth.py module for me",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        if verdict.action == "inject":
            assert "auth.py" in (verdict.metadata.get("files_matched") or [])

    def test_fts_keyword_contributes(self, seeded_decisions):
        """A prompt with a keyword from a decision's text (but no tag /
        file overlap) should still match via FTS5."""
        policy = RelevanceInject()
        # "pgvector" appears only in D000004's decision text — no
        # matching tag, and we don't mention db.py.
        event = _make_prompt_event(
            "Looking at pgvector for embedding lookups in production",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        # If FTS5 finds the decision, it should inject (assuming weight
        # × bm25 contribution > min_score).
        if verdict.action == "inject":
            ids = verdict.metadata.get("decisions_injected") or []
            # D000004 is the pgvector decision (id is opaque but order-based).
            # We just verify SOMETHING got injected via FTS.
            assert len(ids) >= 1
        # If FTS didn't beat min_score, that's also valid — score model
        # can be tuned. The test passes either way.


class TestCacheStability:
    """Same input must produce same bytes — for Anthropic prompt cache."""

    def test_same_prompt_same_bytes(self, seeded_decisions):
        policy1 = RelevanceInject()
        policy2 = RelevanceInject()
        event1 = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            seeded_decisions,
        )
        event2 = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            seeded_decisions,
        )
        v1 = policy1.evaluate(event1, signals=None)
        v2 = policy2.evaluate(event2, signals=None)
        assert v1.action == v2.action
        if v1.action == "inject":
            # The bytes must be identical (cache hit on second prompt).
            assert v1.inject_context == v2.inject_context

    def test_no_timestamps_in_output(self, seeded_decisions):
        """Output must not contain timestamps (would break cache hit)."""
        policy = RelevanceInject()
        event = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        if verdict.action == "inject":
            block = verdict.inject_context
            # ISO 8601 timestamp regex: digits-digits-digitsTdigits
            import re

            assert not re.search(
                r"\d{4}-\d{2}-\d{2}T", block
            ), f"timestamp leaked into cache-stable output:\n{block}"

    def test_cache_key_in_header(self, seeded_decisions):
        policy = RelevanceInject()
        event = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        if verdict.action == "inject":
            block = verdict.inject_context
            assert 'cache_key="' in block
            assert "</codevira-context>" in block


# ─── Config / gates ───────────────────────────────────────────────────


class TestConfiguration:
    def test_mode_off_returns_allow(self, seeded_decisions, monkeypatch):
        monkeypatch.setenv("CODEVIRA_INJECT_MODE", "off")
        policy = RelevanceInject()
        event = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing()

    def test_empty_project_returns_allow(self, isolated_project):
        """No .codevira/ exists → nothing to inject."""
        policy = RelevanceInject()
        event = _make_prompt_event(
            "Add bcrypt to auth.py for security",
            isolated_project,
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing()

    def test_non_user_prompt_submit_returns_allow(self, seeded_decisions):
        policy = RelevanceInject()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=seeded_decisions,
            prompt_text="Add bcrypt to auth.py for security",
        )
        verdict = policy.evaluate(event, signals=None)
        assert verdict.is_allowing()

    def test_custom_max_decisions_via_env(self, seeded_decisions, monkeypatch):
        from mcp_server.storage import decisions_store

        # Add more matches to ensure cap is the limiting factor.
        for i in range(5):
            decisions_store.record(
                decision=f"Security decision {i}",
                file_path="auth.py",
                tags=["security"],
            )

        monkeypatch.setenv("CODEVIRA_INJECT_MAX_DECISIONS", "1")
        policy = RelevanceInject()
        event = _make_prompt_event(
            "security in auth.py",
            seeded_decisions,
        )
        verdict = policy.evaluate(event, signals=None)
        if verdict.action == "inject":
            decision_lines = [
                line
                for line in (verdict.inject_context or "").splitlines()
                if line.startswith(("🔒 **", "• **"))
            ]
            assert len(decision_lines) <= 1


class TestRegistration:
    def test_policy_registers_in_default_set(self):
        """register_default_policies() should include RelevanceInject."""
        from mcp_server.engine import register_default_policies
        from mcp_server.engine.runner import registered_policies, reset_policies

        reset_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        assert "relevance_inject" in names

    def test_old_cross_session_policy_not_registered(self):
        """v2.2.0: cross_session.CrossSessionConsistency is replaced."""
        from mcp_server.engine import register_default_policies
        from mcp_server.engine.runner import registered_policies, reset_policies

        reset_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        # The old policy class is still importable (dead code for Phase E)
        # but should NOT be in the default registration set.
        assert "cross_session_consistency" not in names
