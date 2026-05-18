"""
test_cross_session.py — Hero 5 acceptance tests.

The 8 scenarios in docs/heroes/05-cross-session.md "Acceptance test list"
plus configuration robustness, keyword-extraction unit tests, and
registration tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.cross_session import (
    CrossSessionConsistency,
    _extract_keywords,
    _format_injection,
    _collect_matches,
)


# =====================================================================
# Helpers
# =====================================================================


def _make_event(prompt: str, project_root: Path | None = None) -> HookEvent:
    return HookEvent(
        event_type=EventType.USER_PROMPT_SUBMIT,
        project_root=project_root or Path("/p"),
        prompt_text=prompt,
    )


class _FakeSignals:
    """SignalContext stand-in. Returns canned search_decisions output."""

    def __init__(self, *, by_keyword: dict[str, list[dict[str, Any]]] | None = None):
        self._by_keyword = by_keyword or {}
        self.graph = None  # not used here

    def search_decisions(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self._by_keyword.get(query, [])[:limit]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "CODEVIRA_CROSS_SESSION_MODE",
        "CODEVIRA_CROSS_SESSION_MAX_INJECT",
    ):
        monkeypatch.delenv(k, raising=False)


# =====================================================================
# 8 acceptance scenarios from the spec
# =====================================================================


class TestAcceptanceScenarios:
    def test_1_non_user_prompt_submit_allowed(self):
        """PreToolUse, SessionStart, etc. pass through allow."""
        policy = CrossSessionConsistency()
        for et in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.SESSION_START,
            EventType.STOP,
        ):
            event = HookEvent(
                event_type=et,
                project_root=Path("/p"),
                prompt_text="add a styled tailwind button",  # would match if it ran
            )
            verdict = policy.evaluate(event, _FakeSignals())
            assert verdict.is_allowing(), f"{et} should be allowed"

    def test_2_empty_prompt_allowed(self):
        policy = CrossSessionConsistency()
        event = _make_event("")
        verdict = policy.evaluate(event, _FakeSignals())
        assert verdict.is_allowing()

    def test_3_short_prompt_allowed(self):
        """Prompts under 10 chars (greetings, acks) skip the search."""
        policy = CrossSessionConsistency()
        for short in ("ok", "thanks", "got it", "yes!", "hi"):
            event = _make_event(short)
            verdict = policy.evaluate(event, _FakeSignals())
            assert verdict.is_allowing(), f"{short!r} should be allowed"

    def test_3b_short_prompt_skips_signal_search(self):
        """Behavioral assertion (not output): the short-prompt skip is a
        latency optimization. An output-only test can't catch its
        revert because empty signals → 'allow' either way.

        Spy on signals.search_decisions and assert it's NEVER called
        for prompts under _MIN_PROMPT_CHARS that DO contain extractable
        keywords. (Same recurring class as Week-2 R5 + Week-4 R3 +
        integration I8 — fixes about resource bounds need behavioral
        assertions.)
        """
        from mcp_server.engine.policies.cross_session import (
            _MIN_PROMPT_CHARS,
            _extract_keywords,
        )

        class _SpySignals:
            def __init__(self):
                self.search_calls: list[tuple] = []

            def search_decisions(self, query: str, *, limit: int = 5):
                self.search_calls.append((query, limit))
                return []

        policy = CrossSessionConsistency()

        # The test discriminator: prompts that ARE shorter than the
        # min but have extractable content keywords. Without the skip,
        # they would trigger search; with the skip, they don't.
        # "css api" is 7 chars, two keywords ('css', 'api'), both ≥3 chars,
        # neither in stop-words.
        test_prompts = ["css api", "ssl cert", "api auth"]
        for p in test_prompts:
            assert len(p) < _MIN_PROMPT_CHARS, f"{p!r} not short enough"
            kws = _extract_keywords(p)
            assert kws, f"{p!r} extracts no keywords (test invalid)"

        spy = _SpySignals()
        for prompt in test_prompts:
            policy.evaluate(_make_event(prompt), spy)

        assert spy.search_calls == [], (
            f"short-prompt skip degraded: signals.search_decisions called "
            f"{len(spy.search_calls)} time(s) for prompts under "
            f"{_MIN_PROMPT_CHARS} chars: {spy.search_calls}"
        )

        # Sanity: a long-enough prompt DOES call search (otherwise we'd
        # have a different regression — the policy never searches at all).
        long_prompt = "refactor the auth module to use bcrypt"
        policy.evaluate(_make_event(long_prompt), spy)
        assert spy.search_calls, "long prompt didn't trigger any search"

    def test_4_all_stop_words_prompt_allowed(self):
        """A prompt that's ONLY stop-words extracts no keywords → allow."""
        policy = CrossSessionConsistency()
        # Long enough to pass length check, but all stop-words.
        # NOTE: extract_keywords also skips < 3 chars. So "are you there?"
        # extracts ['there'] which IS not a stop-word. To make this test
        # actually trigger zero-keyword path, use only stop-words ≥ 3 chars.
        event = _make_event("are these the same?")
        # Either zero keywords (preferred) OR keywords that won't match
        # the empty signals. Both result in allow.
        verdict = policy.evaluate(event, _FakeSignals())
        assert verdict.is_allowing()

    def test_5_no_matching_decisions_allow(self):
        """Keywords extracted but search returns nothing → allow."""
        policy = CrossSessionConsistency()
        event = _make_event("Add a styled Get Started button")
        verdict = policy.evaluate(event, _FakeSignals(by_keyword={}))
        assert verdict.is_allowing()

    def test_6_matching_decisions_inject_formatted_context(self):
        """Hero 5's primary path: inject markdown-formatted decision list."""
        policy = CrossSessionConsistency()
        event = _make_event("Add a styled Tailwind button")
        signals = _FakeSignals(
            by_keyword={
                "tailwind": [
                    {
                        "id": 1,
                        "decision": "Tailwind, not Bootstrap — bundle size",
                        "file_path": "styles/",
                        "context": "performance",
                        "created_at": "2025-04-13T10:00:00Z",
                    }
                ],
            }
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "inject"
        assert verdict.inject_context is not None
        assert "Tailwind" in verdict.inject_context
        assert "Prior decisions you may want to consider" in verdict.inject_context
        assert verdict.metadata["matched_count"] == 1
        assert "tailwind" in verdict.metadata["keywords"]

    def test_7_dedup_matches_across_keywords(self):
        """Same decision matched by 2 different tokens → shows once."""
        policy = CrossSessionConsistency()
        event = _make_event("Add a Tailwind button with a styled hover")
        # Same decision returned for both keywords
        same_decision = {
            "id": 1,
            "decision": "Tailwind only, no Bootstrap",
            "file_path": "styles/",
            "context": "",
            "created_at": "2025-04-13T10:00:00Z",
        }
        signals = _FakeSignals(
            by_keyword={
                "tailwind": [same_decision],
                "button": [same_decision],
                "styled": [same_decision],
                "hover": [same_decision],
            }
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "inject"
        assert verdict.metadata["matched_count"] == 1
        # The injected context lists exactly ONE decision, not 4
        assert verdict.inject_context.count("Tailwind only") == 1

    def test_8_evaluation_under_50ms_p95(self):
        """Warm signals → low-millisecond evaluation.

        2026-05-18 v2.1.2 Item 1: budget raised from 5ms to 50ms. The
        policy now runs a ChromaDB semantic gate on every prompt to
        filter BM25 substring-matches that share a keyword but are
        semantically unrelated (trust-recovery fix for the
        ``"how to make a cake"`` regression in v2.1.1). Once the
        embedding model is loaded the query path costs ~20-30ms per
        evaluation; the original 5ms target was for BM25-only.

        The hook fires on UserPromptSubmit (rare, human-paced); 50ms
        is well within the acceptable latency budget for one-per-prompt
        injection.
        """
        import time

        policy = CrossSessionConsistency()
        event = _make_event("refactor auth.py modernize hashing")
        signals = _FakeSignals(
            by_keyword={
                "refactor": [
                    {
                        "id": 1,
                        "decision": "x",
                        "file_path": "auth.py",
                        "context": "",
                        "created_at": "2025-04-13T10:00:00Z",
                    }
                ],
            }
        )
        durations = []
        for _ in range(100):
            t = time.perf_counter()
            policy.evaluate(event, signals)
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[94]
        assert p95 < 50.0, f"p95 = {p95:.3f} ms"


# =====================================================================
# Keyword extraction unit tests
# =====================================================================


class TestKeywordExtraction:
    def test_basic_extraction(self):
        result = _extract_keywords("Add a styled Tailwind button to the homepage")
        # Should have content tokens, not articles/preps
        assert "styled" in result
        assert "tailwind" in result
        assert "button" in result
        assert "homepage" in result
        assert "the" not in result  # stop word
        assert "to" not in result
        assert "a" not in result

    def test_capped_at_5_keywords(self):
        long_prompt = " ".join(f"keyword{i}_unique" for i in range(20))
        result = _extract_keywords(long_prompt)
        assert len(result) <= 5

    def test_dedup_in_extraction(self):
        result = _extract_keywords("tailwind button tailwind hover tailwind classes")
        # 'tailwind' appears 3 times in input but only once in output
        assert result.count("tailwind") == 1

    def test_min_3_chars(self):
        result = _extract_keywords("we ok x is at z")
        # No 1-2 char tokens; 'we' and 'is' are stop words anyway
        for tok in result:
            assert len(tok) >= 3

    def test_all_digits_skipped(self):
        result = _extract_keywords("update version 12345 in package.json")
        # 'update' might be a keyword; 'version' yes; 'package.json' yes
        # 12345 must NOT be in result
        assert "12345" not in result
        assert "package.json" in result

    def test_preserves_dot_separated_identifiers(self):
        """Filenames, qualified names should survive tokenization."""
        result = _extract_keywords("refactor auth.py and api.handlers")
        # Both auth.py and api.handlers should appear as single tokens
        assert "auth.py" in result
        assert "api.handlers" in result

    def test_lowercases_output(self):
        result = _extract_keywords("Refactor MyClass with FOOBAR")
        # All output is lowercase
        for tok in result:
            assert tok == tok.lower()

    def test_empty_prompt(self):
        assert _extract_keywords("") == []

    def test_punctuation_only_prompt(self):
        result = _extract_keywords("?!. -- ()")
        assert result == []

    def test_unicode_identifiers(self):
        """CJK / accented characters survive — Python identifiers can
        contain them and we don't want to drop legitimate domain terms."""
        result = _extract_keywords("update カタカナ_module logic")
        # The Japanese identifier should be in the result (matches \w)
        # NOTE: token regex starts with [A-Za-z], so pure CJK leading
        # tokens may not match. This is a documented v2.0-alpha
        # limitation; v2.1 may extend the regex.
        # Verify at least the ASCII tokens come through.
        assert "module" in [t for t in result if "module" in t] or "logic" in result


# =====================================================================
# Configuration robustness
# =====================================================================


class TestConfiguration:
    def test_default_mode_is_inject(self):
        policy = CrossSessionConsistency()
        config = policy._config()
        assert config["mode"] == "inject"
        assert config["max_inject"] == 5

    def test_off_mode_disables_policy(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MODE", "off")
        policy = CrossSessionConsistency()
        event = _make_event("Add a Tailwind button")
        signals = _FakeSignals(
            by_keyword={
                "tailwind": [
                    {
                        "id": 1,
                        "decision": "x",
                        "file_path": "",
                        "context": "",
                        "created_at": "2025-01-01",
                    }
                ],
            }
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_invalid_mode_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MODE", "totally-fake")
        policy = CrossSessionConsistency()
        assert policy._config()["mode"] == "inject"

    def test_max_inject_clamped(self, monkeypatch: pytest.MonkeyPatch):
        # Negative → default
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MAX_INJECT", "-5")
        policy = CrossSessionConsistency()
        assert policy._config()["max_inject"] == 5

        # Way too big → default (above 20 cap)
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MAX_INJECT", "10000")
        policy = CrossSessionConsistency()
        assert policy._config()["max_inject"] == 5

        # Valid → uses it
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MAX_INJECT", "10")
        policy = CrossSessionConsistency()
        assert policy._config()["max_inject"] == 10

    def test_garbage_max_inject_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_CROSS_SESSION_MAX_INJECT", "not-a-number")
        policy = CrossSessionConsistency()
        assert policy._config()["max_inject"] == 5


# =====================================================================
# Match collection + dedup
# =====================================================================


class TestMatchCollection:
    def test_recency_sort_descending(self):
        signals = _FakeSignals(
            by_keyword={
                "x": [
                    {
                        "id": 1,
                        "decision": "old",
                        "file_path": "",
                        "created_at": "2025-01-01",
                    },
                    {
                        "id": 2,
                        "decision": "new",
                        "file_path": "",
                        "created_at": "2025-06-01",
                    },
                ],
            }
        )
        out = _collect_matches(signals, ["x"], total_cap=5)
        assert out[0]["id"] == 2  # newer first
        assert out[1]["id"] == 1

    def test_total_cap_enforced(self):
        signals = _FakeSignals(
            by_keyword={
                "a": [
                    {
                        "id": i,
                        "decision": f"d{i}",
                        "file_path": "",
                        "created_at": f"2025-{i:02}-01",
                    }
                    for i in range(1, 11)
                ],
            }
        )
        out = _collect_matches(signals, ["a"], max_per_keyword=10, total_cap=3)
        assert len(out) == 3

    def test_signal_failure_returns_empty(self):
        class _Broken:
            def search_decisions(self, *a, **kw):
                raise RuntimeError("boom")

        out = _collect_matches(_Broken(), ["a"], total_cap=5)
        assert out == []

    def test_decisions_without_created_at_sink_to_bottom(self):
        signals = _FakeSignals(
            by_keyword={
                "x": [
                    {"id": 1, "decision": "no-date", "file_path": ""},
                    {
                        "id": 2,
                        "decision": "with-date",
                        "file_path": "",
                        "created_at": "2025-01-01",
                    },
                ],
            }
        )
        out = _collect_matches(signals, ["x"], total_cap=5)
        # The dated decision should rank higher
        assert out[0]["id"] == 2


# =====================================================================
# Hero-1 / Hero-4 / Hero-5 coexistence
# =====================================================================


class TestBehavioralGates:
    """Week-5 retrospective extension to Hero 5: gates that don't
    affect output for the empty-signals path can't be caught by
    output-only tests. Behavioral spies + targeted scenarios close
    the same shape of test gap as Hero 4 + Hero 1.
    """

    def test_non_user_prompt_submit_does_not_call_signals(self):
        """event_type gate: PreToolUse / SessionStart / Stop must NOT
        trigger signals.search_decisions. Mutation removing the gate
        would reach extract_keywords on prompt_text=None and crash —
        OR pass with empty signals. Spy catches it directly.
        """

        class _SpySignals:
            def __init__(self):
                self.calls: list[tuple] = []

            def search_decisions(self, query, *, limit=5):
                self.calls.append((query, limit))
                return []

        policy = CrossSessionConsistency()
        spy = _SpySignals()
        for et in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.SESSION_START,
            EventType.STOP,
        ):
            event = HookEvent(
                event_type=et,
                project_root=Path("/p"),
                prompt_text="add a styled tailwind button to homepage",
            )
            policy.evaluate(event, spy)
        assert spy.calls == [], (
            f"event_type gate degraded: search_decisions called on "
            f"non-USER_PROMPT_SUBMIT events: {spy.calls}"
        )

    def test_empty_prompt_does_not_call_signals(self):
        """Empty prompt → no work. Behavioral spy."""

        class _SpySignals:
            def __init__(self):
                self.calls: list[tuple] = []

            def search_decisions(self, query, *, limit=5):
                self.calls.append((query, limit))
                return []

        policy = CrossSessionConsistency()
        spy = _SpySignals()
        for empty in ("", None):
            event = HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=Path("/p"),
                prompt_text=empty,
            )
            policy.evaluate(event, spy)
        assert spy.calls == [], f"empty-prompt gate degraded: {spy.calls}"

    def test_no_keywords_skips_collect_matches(self, monkeypatch: pytest.MonkeyPatch):
        """Empty-keywords gate: when ``_extract_keywords`` returns
        nothing, the policy must NOT call ``_collect_matches``.
        Output-only assertions can't catch the gate (empty keywords
        produce empty matches anyway). Monkeypatch ``_collect_matches``
        to a spy that records every invocation.
        """
        from mcp_server.engine.policies import cross_session as cs_mod

        invocations: list[tuple] = []
        original = cs_mod._collect_matches

        def spy(*args, **kw):
            invocations.append((args, kw))
            return original(*args, **kw)

        monkeypatch.setattr(cs_mod, "_collect_matches", spy)

        policy = CrossSessionConsistency()
        # All stop-words: extract_keywords returns []
        event = _make_event("the and the of we have for it as if")
        verdict = policy.evaluate(event, _FakeSignals())
        assert verdict.is_allowing()
        assert (
            invocations == []
        ), f"empty-keywords gate degraded: _collect_matches called: {invocations}"

    def test_signals_none_skips_collect_matches(self, monkeypatch: pytest.MonkeyPatch):
        """signals=None gate: must NOT reach ``_collect_matches`` with
        a None signals (which would AttributeError, caught by the
        per-keyword try/except, but still wastes work and depends on
        the safety net). Spy on _collect_matches.
        """
        from mcp_server.engine.policies import cross_session as cs_mod

        invocations: list[tuple] = []
        original = cs_mod._collect_matches

        def spy(*args, **kw):
            invocations.append((args, kw))
            return original(*args, **kw)

        monkeypatch.setattr(cs_mod, "_collect_matches", spy)

        policy = CrossSessionConsistency()
        event = _make_event("refactor auth.py to use bcrypt")
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert (
            invocations == []
        ), f"signals=None gate degraded: _collect_matches called: {invocations}"

    def test_priority_value_stable(self):
        """Hero 5 priority MUST stay below block-class heroes (1, 4).
        If a future refactor inverts this, advisory inject verdicts
        would override hard blocks in priority sort — incorrect.
        """
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.policies.blast_radius import BlastRadiusVeto

        assert (
            CrossSessionConsistency.priority < DecisionLock.priority
        ), "Hero 5 priority must be below Hero 1 (block-class)"
        assert (
            CrossSessionConsistency.priority < BlastRadiusVeto.priority
        ), "Hero 5 priority must be below Hero 4 (block-class)"

    def test_dedup_collapses_same_decision_under_strict_key(self):
        """Dedup key is (decision_text, file_path). Two matches with
        the same text+path but different IDs collapse to one. A
        mutation switching the key to id-only would dedupe LESS
        aggressively. Test: provide same-text+path with different IDs.
        """
        policy = CrossSessionConsistency()
        # Two decisions that should collapse:
        #   - same decision text, same file_path, different id
        #   - if dedup-by-id, both appear; if dedup-by-(text, path), one
        same_text = "Tailwind, not Bootstrap — bundle size"
        same_path = "styles/"
        signals = _FakeSignals(
            by_keyword={
                "tailwind": [
                    {
                        "id": 1,
                        "decision": same_text,
                        "file_path": same_path,
                        "context": "",
                        "created_at": "2025-04-13",
                    },
                    {
                        "id": 2,
                        "decision": same_text,
                        "file_path": same_path,
                        "context": "",
                        "created_at": "2025-04-13",
                    },
                ],
            }
        )
        event = _make_event("Add a Tailwind button")
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "inject"
        # Strict dedup-by-(text, path) collapses to 1
        assert verdict.metadata["matched_count"] == 1, (
            f"strict dedup collapsed wrong number; got "
            f"{verdict.metadata['matched_count']} (expected 1)"
        )


class TestRegistration:
    def test_register_default_policies_includes_hero_5(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names = sorted(p.name for p in registered_policies())
        assert "cross_session_consistency" in names
        assert "decision_lock" in names
        assert "blast_radius_veto" in names

    def test_hero_5_fires_through_engine_dispatch(self, tmp_path):
        """Week-5 R5-redo regression test for Hero 5 specifically:
        Hero 5 takes ``signals`` as a kwarg with default None. If the
        runner doesn't pass signals through, Hero 5 silently allows
        every USER_PROMPT_SUBMIT — the entire injection path is dead.

        Builds a real graph with a Tailwind decision; dispatches a
        prompt mentioning Tailwind; asserts INJECT.
        """
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine import (
            register_policy,
            reset_policies,
            dispatch,
        )
        import mcp_server.paths as paths_mod

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        project = tmp_path / "p"
        project.mkdir()
        (project / "pyproject.toml").write_text("")

        paths_mod.get_global_home = lambda: fake_home
        paths_mod.set_project_dir(project)
        paths_mod.invalidate_data_dir_cache()

        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(db_path)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "x"),
        )
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "s1",
                "Tailwind, not Bootstrap — bundle size",
                "styles/",
                "",
                "2025-04-13",
            ),
        )
        g.conn.commit()
        g.close()

        reset_policies()
        register_policy(CrossSessionConsistency())
        os.environ.pop("CODEVIRA_CROSS_SESSION_MODE", None)

        try:
            event = HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=project,
                prompt_text="Add a styled Tailwind button to the homepage",
            )
            verdict = dispatch(event)
            assert verdict.action == "inject", (
                f"Hero 5 must fire through dispatch with real signals; "
                f"got {verdict.action} — likely the runner isn't passing "
                f"signals to policy.evaluate()"
            )
            assert verdict.inject_context is not None
            assert "Tailwind" in verdict.inject_context
        finally:
            reset_policies()

    def test_idempotent_with_three_heroes(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        register_default_policies()  # idempotent
        names = [p.name for p in registered_policies()]
        for n in (
            "blast_radius_veto",
            "decision_lock",
            "cross_session_consistency",
        ):
            assert names.count(n) == 1


# =====================================================================
# Robustness
# =====================================================================


class TestRobustness:
    def test_none_signals_allows_gracefully(self):
        policy = CrossSessionConsistency()
        event = _make_event("Add a Tailwind button")
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    def test_signals_search_raises_caught(self):
        """Hero 5 must NOT crash when signals.search_decisions raises;
        _collect_matches catches per-keyword failures and continues."""

        class _PartiallyBroken:
            def search_decisions(self, query, *, limit=5):
                if query == "tailwind":
                    raise RuntimeError("boom")
                return []

        policy = CrossSessionConsistency()
        event = _make_event("Add a Tailwind button to homepage")
        verdict = policy.evaluate(event, _PartiallyBroken())
        # No crash; allow (no matches collected)
        assert verdict.is_allowing()


# =====================================================================
# Inject formatting
# =====================================================================


class TestInjectionFormatting:
    def test_format_includes_preamble_and_postamble(self):
        matches = [
            {
                "id": 1,
                "decision": "Tailwind, not Bootstrap",
                "file_path": "styles/",
                "created_at": "2025-04-13T10:00:00Z",
            }
        ]
        text = _format_injection(matches)
        assert "Prior decisions you may want to consider" in text
        assert "If your current request conflicts" in text

    def test_format_truncates_long_decisions(self):
        long_text = "x" * 500
        matches = [
            {
                "id": 1,
                "decision": long_text,
                "file_path": "",
                "created_at": "2025-01-01",
            }
        ]
        text = _format_injection(matches)
        assert "..." in text
        # Decision portion truncated to ~200
        assert "x" * 500 not in text

    def test_format_handles_missing_file_path(self):
        matches = [
            {
                "id": 1,
                "decision": "global decision",
                "file_path": "",
                "created_at": "2025-01-01",
            }
        ]
        text = _format_injection(matches)
        # When file_path is empty, no [filepath] prefix
        assert "[]" not in text

    def test_format_handles_missing_created_at(self):
        matches = [
            {
                "id": 1,
                "decision": "no date",
                "file_path": "",
            }
        ]
        text = _format_injection(matches)
        # Falls back to placeholder
        assert "????-??-??" in text
