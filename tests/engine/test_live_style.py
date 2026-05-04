"""
test_live_style.py — Hero 7 acceptance + behavioral + mutation tests.

Tier-0 pre-flight from start:
- Real preferences DB via SQLiteGraph.add_preference (not mocked)
- Behavioral spies on signals.preferences calls
- End-to-end dispatch test with real preferences DB
- 10+ mutations from start
- Bug-shape audit (no Bug-3-class issues)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.live_style import (
    LiveStyleEnforcement,
    _is_camel_case, _is_snake_case,
    _detect_naming_violations, _detect_quote_violations,
    _detect_indent_violations, _detect_violations,
    _extract_after_block,
)


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_post_event(
    *,
    tool_name: str = "Edit",
    target: Path | None = None,
    project_root: Path | None = None,
    proposed_diff: str | None = None,
) -> HookEvent:
    proj = project_root or Path("/p")
    return HookEvent(
        event_type=EventType.POST_TOOL_USE,
        project_root=proj,
        tool_name=tool_name,
        target_file=target or (proj / "foo.py"),
        proposed_diff=proposed_diff,
    )


class _FakeSignals:
    """Honors-args fake — preferences() returns canned data."""

    def __init__(
        self,
        *,
        prefs: list[dict[str, Any]] | None = None,
    ):
        self._prefs = prefs or []
        self.preferences_calls: list[str] = []

    def preferences(self, category: str = "") -> list[dict[str, Any]]:
        self.preferences_calls.append(category)
        if category:
            return [p for p in self._prefs if p.get("category") == category]
        return list(self._prefs)


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    monkeypatch.setattr(
        "mcp_server.paths.get_global_home", lambda: cv_data,
    )
    return project


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "CODEVIRA_LIVE_STYLE_MODE",
        "CODEVIRA_LIVE_STYLE_MIN_FREQ",
    ):
        monkeypatch.delenv(k, raising=False)


# =====================================================================
# 8 acceptance scenarios
# =====================================================================


class TestAcceptanceScenarios:

    def test_1_non_post_tool_use_event_allowed(self):
        policy = LiveStyleEnforcement()
        for et in (EventType.PRE_TOOL_USE, EventType.SESSION_START,
                   EventType.USER_PROMPT_SUBMIT, EventType.STOP):
            event = HookEvent(
                event_type=et, project_root=Path("/p"),
                tool_name="Edit", target_file=Path("/p/x.py"),
            )
            verdict = policy.evaluate(event, _FakeSignals())
            assert verdict.is_allowing()

    def test_2_post_tool_use_non_edit_allowed(self):
        policy = LiveStyleEnforcement()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            event = _make_post_event(tool_name=tool)
            verdict = policy.evaluate(event, _FakeSignals())
            assert verdict.is_allowing()

    def test_3_no_preferences_recorded_allow(self):
        policy = LiveStyleEnforcement()
        event = _make_post_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nold\n--- after\ndef fetchUser(): pass\n",
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=[]))
        assert verdict.is_allowing()

    def test_4_camel_case_in_snake_case_project_warns(self):
        policy = LiveStyleEnforcement()
        target = Path("/p/api.py")
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "example": "def fetch_user_id():",
            "source": "manual",
        }]
        diff = (
            "--- before\n"
            "def existing(): pass\n"
            "--- after\n"
            "def existing(): pass\n"
            "\n"
            "def fetchUserMetadata(userId):\n"
            "    return userId\n"
        )
        event = _make_post_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.action == "warn"
        assert "fetchUserMetadata" in (verdict.message or "")
        assert "snake_case" in (verdict.message or "")
        assert verdict.metadata["violation_count"] >= 1

    def test_5_snake_case_in_snake_case_project_allows(self):
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "example": "",
            "source": "manual",
        }]
        diff = (
            "--- before\n"
            "old\n"
            "--- after\n"
            "def fetch_user_metadata(user_id):\n"
            "    return user_id\n"
        )
        event = _make_post_event(target=Path("/p/x.py"), proposed_diff=diff)
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()

    def test_6_quote_style_violation_warns(self):
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "quotes", "signal": "double-quotes",
            "frequency": 28, "example": '"hello"',
            "source": "manual",
        }]
        diff = (
            "--- before\n"
            "x = 1\n"
            "--- after\n"
            "x = 1\n"
            "msg = 'use double quotes here'\n"
            "name = 'another single-quoted string'\n"
        )
        event = _make_post_event(target=Path("/p/x.py"), proposed_diff=diff)
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.action == "warn"
        assert "quotes" in (verdict.message or "").lower()
        assert verdict.metadata["violation_count"] >= 2

    def test_7_off_mode_disables_policy(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MODE", "off")
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "source": "manual",
        }]
        diff = (
            "--- before\nx\n--- after\ndef fetchUser(): pass\n"
        )
        event = _make_post_event(target=Path("/p/x.py"), proposed_diff=diff)
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()

    def test_8_evaluate_under_10ms_p95(self):
        """Perf budget: p95 under 10 ms across 1000 trials.

        The actual mean is sub-millisecond (~0.15 ms). 10 ms tolerates
        GC pauses while still catching order-of-magnitude regressions.
        Use n=1000 for stability — small n (≤ 100) is too noisy for
        p95 because a single GC pause skews it.
        """
        import statistics, time
        policy = LiveStyleEnforcement()
        prefs = [
            {"category": "naming", "signal": "snake_case",
             "frequency": 42, "source": "manual"},
            {"category": "quotes", "signal": "double-quotes",
             "frequency": 28, "source": "manual"},
            {"category": "indent", "signal": "spaces",
             "frequency": 100, "source": "manual"},
        ]
        diff = (
            "--- before\nold\n--- after\n"
            "def fetch_user(): return 1\n" * 20
        )
        event = _make_post_event(
            target=Path("/p/x.py"), proposed_diff=diff,
        )
        durations = []
        for _ in range(1000):
            t = time.perf_counter()
            policy.evaluate(event, _FakeSignals(prefs=prefs))
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[949]
        # Median is the stable signal — sanity-check it's well under
        # the bound. p95 has more variance.
        p50 = statistics.median(durations)
        assert p50 < 5.0, f"p50 = {p50:.3f} ms (sub-ms expected)"
        assert p95 < 10.0, f"p95 = {p95:.3f} ms (10 ms target)"


# =====================================================================
# Behavioral gates
# =====================================================================


class TestBehavioralGates:

    def test_non_post_tool_use_does_not_call_preferences(self):
        """event_type gate: preferences() is NOT called on non-POST events."""
        policy = LiveStyleEnforcement()
        spy = _FakeSignals(prefs=[])
        for et in (EventType.PRE_TOOL_USE, EventType.SESSION_START,
                   EventType.USER_PROMPT_SUBMIT, EventType.STOP):
            event = HookEvent(
                event_type=et, project_root=Path("/p"),
                tool_name="Edit", target_file=Path("/p/x.py"),
            )
            policy.evaluate(event, spy)
        assert spy.preferences_calls == []

    def test_non_edit_post_does_not_call_preferences(self):
        policy = LiveStyleEnforcement()
        spy = _FakeSignals(prefs=[])
        for tool in ("Read", "Bash", "Glob", "Grep"):
            event = _make_post_event(tool_name=tool)
            policy.evaluate(event, spy)
        assert spy.preferences_calls == []

    def test_target_none_does_not_call_preferences(self):
        policy = LiveStyleEnforcement()
        spy = _FakeSignals(prefs=[])
        event = HookEvent(
            event_type=EventType.POST_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",
            target_file=None,
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        verdict = policy.evaluate(event, spy)
        assert verdict.is_allowing()
        assert spy.preferences_calls == []

    def test_off_mode_skips_preferences_call(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MODE", "off")
        policy = LiveStyleEnforcement()
        spy = _FakeSignals(prefs=[])
        event = _make_post_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        policy.evaluate(event, spy)
        assert spy.preferences_calls == []

    def test_signals_none_does_not_crash(self):
        policy = LiveStyleEnforcement()
        event = _make_post_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    def test_priority_value_stable(self):
        """Hero 7 priority=20: lower than block-class heroes, near
        the bottom (only Token Budget at 10 is lower)."""
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
        from mcp_server.engine.policies.anti_regression import AntiRegression
        from mcp_server.engine.policies.token_budget import TokenBudgetPersist
        for higher in (DecisionLock, AntiRegression, BlastRadiusVeto):
            assert LiveStyleEnforcement.priority < higher.priority
        # Above only Token Budget (telemetry is the lowest priority)
        assert LiveStyleEnforcement.priority > TokenBudgetPersist.priority

    def test_invalid_mode_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MODE", "garbage")
        policy = LiveStyleEnforcement()
        config = policy._config()
        assert config["mode"] == "warn"

    def test_min_frequency_cap_clamped(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MIN_FREQ", "-5")
        assert LiveStyleEnforcement()._config()["min_frequency"] == 3
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MIN_FREQ", "100000")
        assert LiveStyleEnforcement()._config()["min_frequency"] == 3
        monkeypatch.setenv("CODEVIRA_LIVE_STYLE_MIN_FREQ", "10")
        assert LiveStyleEnforcement()._config()["min_frequency"] == 10

    def test_low_frequency_preferences_skipped(self):
        """Preferences with frequency < min_freq are filtered out
        before any detector runs. Behavioral assertion: when only
        low-freq prefs exist, no warn fires.
        """
        policy = LiveStyleEnforcement()
        prefs = [
            {"category": "naming", "signal": "snake_case",
             "frequency": 1, "source": "manual"},  # below default min_freq=3
        ]
        diff = "--- before\nx\n--- after\ndef fetchUser(): pass\n"
        event = _make_post_event(
            target=Path("/p/x.py"), proposed_diff=diff,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()


# =====================================================================
# Detector unit tests
# =====================================================================


class TestDetectorPredicates:

    def test_is_camel_case(self):
        # True positives
        for s in ("fetchUser", "getUserId", "myCamelCaseFunc"):
            assert _is_camel_case(s), f"{s!r} should be camelCase"
        # True negatives (snake_case, lowercase, PascalCase)
        for s in ("fetch_user", "fetch", "FetchUser", "FUNC", ""):
            assert not _is_camel_case(s), f"{s!r} should NOT be camelCase"

    def test_is_snake_case(self):
        for s in ("fetch_user", "get_user_id", "my_func"):
            assert _is_snake_case(s), f"{s!r} should be snake_case"
        for s in ("fetchUser", "fetch", "Fetch_User", "_private", "fetch_USER"):
            assert not _is_snake_case(s), f"{s!r} should NOT be snake_case"


class TestDetectors:

    def test_naming_detector_flags_camel_in_snake_project(self):
        after = (
            "def fetchUserMetadata(userId):\n"
            "    return userId\n"
            "\n"
            "def get_user_name():\n"
            "    pass\n"
        )
        violations = _detect_naming_violations(after, ".py", "snake_case")
        # Only fetchUserMetadata should be flagged
        names = {v["snippet"] for v in violations}
        assert "fetchUserMetadata" in names
        assert "get_user_name" not in names

    def test_naming_detector_unrecognized_signal_skips(self):
        after = "def fetchFoo(): pass\n"
        violations = _detect_naming_violations(after, ".py", "kebab-case")
        assert violations == []

    def test_naming_detector_unsupported_language_skips(self):
        after = "def fetchFoo(): pass\n"
        violations = _detect_naming_violations(after, ".rb", "snake_case")
        assert violations == []

    def test_naming_detector_skips_class_pascal_case(self):
        after = (
            "class MyService:\n"
            "    def get_user(self): pass\n"
        )
        violations = _detect_naming_violations(after, ".py", "snake_case")
        # MyService is PascalCase (class) — convention is to skip classes
        names = {v["snippet"] for v in violations}
        assert "MyService" not in names

    def test_naming_detector_skips_underscore_prefix(self):
        after = "def _fetchUser(): pass\n"
        violations = _detect_naming_violations(after, ".py", "snake_case")
        # Leading underscore = private; skip enforcement
        assert violations == []

    def test_quote_detector_flags_single_in_double_project(self):
        after = (
            "x = 1\n"
            "msg = 'hello'\n"
            "name = 'world'\n"
        )
        violations = _detect_quote_violations(after, ".py", "double-quotes")
        # Both 'hello' and 'world' lines should be flagged
        assert len(violations) >= 2

    def test_quote_detector_normalizes_signal_synonyms(self):
        after = "msg = 'hello'\n"
        for sig in ("double-quotes", "double_quotes", "double", "DOUBLE"):
            v = _detect_quote_violations(after, ".py", sig)
            assert len(v) == 1, f"signal {sig!r} should detect violation"

    def test_indent_detector_flags_tabs_in_spaces_project(self):
        after = (
            "def f():\n"
            "\tprint('tab indent')\n"
            "\treturn 1\n"
        )
        violations = _detect_indent_violations(after, ".py", "spaces")
        assert len(violations) >= 2

    def test_extract_after_block_basic(self):
        diff = (
            "--- before\n"
            "old code\n"
            "--- after\n"
            "new code\n"
        )
        assert _extract_after_block(diff) == "new code\n"

    def test_extract_after_block_huge_diff_returns_empty(self):
        big = "x" * 200_000
        diff = f"--- before\nold\n--- after\n{big}\n"
        # Diff exceeds _MAX_DIFF_BYTES → returns empty
        assert _extract_after_block(diff) == ""

    def test_extract_after_block_empty_or_none_returns_empty(self):
        """Truly empty inputs return empty. Bug-4 fix: raw content
        without markers is now treated as Write-tool content (the
        whole input IS the after-block)."""
        for empty in (None, ""):
            assert _extract_after_block(empty) == ""

    def test_extract_after_block_write_format_returns_full_content(self):
        """Bug 4 (Week-9 integration QA): Claude Code's Write hook
        passes raw file content as proposed_diff with NO markers.
        The original parser returned '' for this shape, silently
        no-op'ing Hero 7 on every Write event. Now: shape #2 returns
        the whole input as the after-block."""
        # Write tool: full content, no Edit-style markers
        write_content = "def fetchUserMetadata(userId):\n    return userId\n"
        out = _extract_after_block(write_content)
        assert out == write_content, (
            "Write-tool content (no markers) must be treated as raw "
            "after-block (Bug-4 regression test)"
        )

        # Half-Edit format (only --- before, no --- after) is treated as
        # raw content. This is permissive but safe — at worst Hero 7
        # scans the marker text itself, which contains no identifiers
        # or string literals our detectors care about.
        half = "--- before\nold\n"
        assert _extract_after_block(half) == half


# =====================================================================
# End-to-end through dispatch() with real preferences DB
# =====================================================================


class TestEngineDispatch:

    def test_hero_7_fires_through_dispatch(self, isolated_project: Path):
        """Real preferences DB + dispatch() → warn on a real violation."""
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies, dispatch,
        )
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        from mcp_server.paths import get_data_dir
        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        # Plant a real preference: snake_case naming, frequency=42
        g.conn.execute(
            "INSERT INTO preferences (category, signal, example, frequency, "
            "source) VALUES (?, ?, ?, ?, ?)",
            ("naming", "snake_case", "def fetch_user():", 42, "manual"),
        )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        diff = (
            "--- before\n"
            "old\n"
            "--- after\n"
            "def fetchUserMetadata(userId):\n"
            "    return userId\n"
        )
        event = HookEvent(
            event_type=EventType.POST_TOOL_USE,
            project_root=isolated_project,
            tool_name="Edit",
            target_file=isolated_project / "api.py",
            proposed_diff=diff,
        )
        verdict = dispatch(event)
        assert verdict.action == "warn", (
            f"Hero 7 must fire warn through dispatch with real preferences DB; "
            f"got {verdict.action}"
        )
        assert "snake_case" in (verdict.message or "")
        reset_policies()


# =====================================================================
# Registration
# =====================================================================


class TestRegistration:

    def test_register_default_policies_includes_hero_7(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "live_style_enforcement" in names

    def test_idempotent_with_six_heroes(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        for n in (
            "anti_regression", "blast_radius_veto", "decision_lock",
            "cross_session_consistency", "token_budget_persist",
            "live_style_enforcement",
        ):
            assert names.count(n) == 1


# =====================================================================
# Edge cases
# =====================================================================


class TestEdgeCases:

    def test_unsupported_extension_no_violations(self):
        """Markdown / JSON files don't trigger naming detectors."""
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "source": "manual",
        }]
        diff = (
            "--- before\nold\n--- after\ndef fetchUser(): pass\n"
        )
        event = _make_post_event(
            target=Path("/p/README.md"),  # markdown — no Python rules
            proposed_diff=diff,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()

    def test_proposed_diff_none_allows(self):
        """No diff = nothing to check."""
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "source": "manual",
        }]
        event = _make_post_event(
            target=Path("/p/x.py"),
            proposed_diff=None,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()

    def test_empty_after_block_allows(self):
        """Edit that deletes all content has empty after block."""
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "source": "manual",
        }]
        diff = "--- before\nold code\n--- after\n"
        event = _make_post_event(
            target=Path("/p/x.py"), proposed_diff=diff,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing()

    def test_unrecognized_category_no_violations(self):
        """Preferences with categories Hero 7 doesn't know about
        (e.g. 'docstring_style', 'import_order') must produce zero
        violations. Catches mutations to the unrecognized-category
        fallback that would inject false positives.
        """
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "docstring_style",  # unrecognized
            "signal": "google",
            "frequency": 42,
            "source": "manual",
        }]
        diff = (
            "--- before\nold\n--- after\n"
            "def fetchUser(): pass\n"
        )
        event = _make_post_event(
            target=Path("/p/x.py"), proposed_diff=diff,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        assert verdict.is_allowing(), (
            f"unrecognized category should produce zero violations; "
            f"got {verdict.action}"
        )

    def test_huge_diff_skipped(self):
        """Diffs over _MAX_DIFF_BYTES are skipped (extract_after returns '')."""
        policy = LiveStyleEnforcement()
        prefs = [{
            "category": "naming", "signal": "snake_case",
            "frequency": 42, "source": "manual",
        }]
        big_after = "def fetchUser(): pass\n" * 10000
        diff = f"--- before\nold\n--- after\n{big_after}"
        event = _make_post_event(
            target=Path("/p/x.py"), proposed_diff=diff,
        )
        verdict = policy.evaluate(event, _FakeSignals(prefs=prefs))
        # Skipped → allow
        assert verdict.is_allowing()
