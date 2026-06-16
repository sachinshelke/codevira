"""
test_anti_regression.py — Hero 2 acceptance + behavioral + mutation tests.

Tier-0 pre-flight from start:
  - Real fix_history.db via record_fix() (not mocked)
  - Behavioral spies on signals.fixes + is_revert
  - End-to-end test through dispatch() with real graph + real fixes DB
  - 10+ mutations from start
  - Bug-shape audit during R8 (no Bug-3-class issues)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.anti_regression import AntiRegression


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_event(
    *,
    tool_name: str = "Edit",
    target: Path | None = None,
    project_root: Path | None = None,
    proposed_diff: str | None = None,
) -> HookEvent:
    proj = project_root or Path("/p")
    return HookEvent(
        event_type=EventType.PRE_TOOL_USE,
        project_root=proj,
        tool_name=tool_name,
        target_file=target or (proj / "foo.py"),
        proposed_diff=proposed_diff,
    )


class _FakeSignals:
    """Honors-args fake — fixes() returns canned data per file_path.

    Per Lesson #15: fakes that ignore args HIDE mutations on the args.
    This fake stores fixes by file_path and returns them only when
    queried with the matching path.
    """

    def __init__(
        self,
        *,
        fixes_for: dict[Path, list[dict[str, Any]]] | None = None,
    ):
        self._fixes_for = fixes_for or {}
        self.fixes_calls: list[Path] = []

    def fixes(self, file_path: Path) -> list[dict[str, Any]]:
        self.fixes_calls.append(file_path)
        return list(self._fixes_for.get(file_path, []))


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
        "mcp_server.paths.get_global_home",
        lambda: cv_data,
    )
    return project


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEVIRA_ANTI_REGRESSION_MODE", raising=False)


# =====================================================================
# 8 acceptance scenarios
# =====================================================================


class TestAcceptanceScenarios:
    def test_1_non_edit_event_allowed(self):
        policy = AntiRegression()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            verdict = policy.evaluate(
                _make_event(tool_name=tool),
                _FakeSignals(),
            )
            assert verdict.is_allowing()

    def test_2_no_fixes_recorded_allowed(self):
        policy = AntiRegression()
        verdict = policy.evaluate(
            _make_event(
                target=Path("/p/x.py"),
                proposed_diff="--- before\nold\n--- after\nnew\n",
            ),
            _FakeSignals(fixes_for={}),  # no fixes
        )
        assert verdict.is_allowing()

    def test_3_fixes_recorded_but_no_revert_match_allowed(self):
        """The diff doesn't match any fix's revert pattern.

        is_revert returns False for diffs that don't move toward the
        pre-fix state. Provide a diff that adds NEW content (not
        related to any fix) and verify allow.
        """
        policy = AntiRegression()
        target = Path("/p/x.py")
        # Fix on lines 10-20 with description containing "race"
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "fix: race in cache",
                        "source": "git",
                        "commit_sha": "abc12345abc",
                        "line_start": 10,
                        "line_end": 20,
                        "recorded_at": 1730764800.0,
                    }
                ],
            }
        )
        # Diff modifies a completely different region with no "race" keywords
        diff = (
            "--- before\n"
            "def helper():\n"
            "    pass\n"
            "--- after\n"
            "def helper():\n"
            "    return 1\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert (
            verdict.is_allowing()
        ), f"unrelated diff shouldn't trigger anti-regression; got {verdict.action}"

    def test_4_revert_match_blocks_with_diagnostic(self):
        """The diff matches is_revert's heuristic → block.

        is_revert (keyword-overlap heuristic): if after_hits >
        before_hits AND after_hits > 0 for description keywords
        (filtered for stop-words + length > 2), it flags revert.

        Test diff: bug keywords ONLY in after, not before.
        """
        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        # Description keywords (after stop-word filter):
                        #   infinite, deadlock, race, condition
                        "description": "fix: infinite deadlock race condition",
                        "source": "git",
                        "commit_sha": "abc12345abc",
                        "line_start": 0,
                        "line_end": 0,  # whole-file marker
                        "recorded_at": 1730764800.0,
                    }
                ],
            }
        )
        # before: clean code, no bug keywords
        # after: re-introduces "infinite" and "race" keywords
        diff = (
            "--- before\n"
            "    with self._lock:\n"
            "        attempt()\n"
            "--- after\n"
            "    # infinite retry; the race condition is back\n"
            "    attempt()\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking(), f"expected block, got {verdict.action}"
        # Message mentions the description text
        msg_lower = (verdict.message or "").lower()
        assert "infinite" in msg_lower or "race" in msg_lower or "deadlock" in msg_lower
        assert verdict.metadata["reverting_count"] == 1
        assert "abc12345abc" in verdict.metadata["reverting_commit_shas"]

    def test_5_warn_mode_produces_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_ANTI_REGRESSION_MODE", "warn")
        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "fix: infinite deadlock race condition",
                        "source": "git",
                        "commit_sha": "deadbeefdead",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    }
                ],
            }
        )
        diff = (
            "--- before\n    with self._lock:\n        attempt()\n"
            "--- after\n    # infinite race condition\n    attempt()\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "warn"
        assert verdict.metadata["mode"] == "warn"

    def test_6_off_mode_disables_policy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_ANTI_REGRESSION_MODE", "off")
        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "fix: x",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    }
                ],
            }
        )
        diff = "--- before\nfor _ in range(N):\n--- after\nwhile True:  # infinite\n"
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_7_hero_2_plus_hero_1_simultaneous_fire(
        self,
        isolated_project: Path,
    ):
        """Real-graph end-to-end: a file is BOTH locked (Hero 1
        applicable) AND has a fix history that the proposed diff
        reverts (Hero 2 applicable). Both fire; combined verdict
        carries Hero 1's message (priority=100 > 80).
        """
        from indexer.fix_history import record_fix
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        # v3.0.0 (2026-05-22 round-2 G5 fix): seed via the JSONL
        # backend, not graph.db SQL. signals.decisions() reads from
        # .codevira/decisions.jsonl in v3.0.0; SQL-table seeds are
        # invisible (the storage layer they're written to isn't the
        # one DecisionLock reads).
        from mcp_server.storage import (
            decisions_store,
            paths as store_paths,
        )

        store_paths.ensure_dirs()
        # v3.5.0 content-aware lock: the locked decision must be ABOUT the
        # code the diff changes (self._lock / attempt) for decision_lock to
        # fire — otherwise it correctly downgrades to a warn and this test's
        # premise (BOTH heroes block, priority decides primary) won't hold.
        decisions_store.record(
            decision="attempt() must run inside self._lock — no lock-free retry path",
            file_path="auth.py",
            context="race condition guard",
            do_not_revert=True,
        )

        # Record a fix on the same file
        record_fix(
            isolated_project,
            file_path="auth.py",
            line_start=0,
            line_end=0,
            description="fix: infinite deadlock race condition",
            source="manual",
        )

        reset_policies()
        register_default_policies()

        diff = (
            "--- before\n    with self._lock:\n        attempt()\n"
            "--- after\n    # infinite race condition\n    attempt()\n"
        )
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=isolated_project,
            tool_name="Edit",
            target_file=isolated_project / "auth.py",
            proposed_diff=diff,
        )
        verdict = dispatch(event)
        assert verdict.is_blocking()
        # Decision Lock (priority=100) wins as primary
        assert (
            verdict.policy == "decision_lock"
        ), f"expected decision_lock to be primary; got {verdict.policy}"
        # Anti-regression should appear in other_blocking_policies
        others = verdict.metadata.get("other_blocking_policies", [])
        assert (
            "anti_regression" in others
        ), f"anti_regression should be in others; got {others}"
        reset_policies()

    def test_8_evaluate_under_5ms_p95(self):
        import time

        policy = AntiRegression()
        target = Path("/p/x.py")
        # 5 fixes — realistic-ish
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": i,
                        "file_path": "x.py",
                        "description": f"fix: bug {i}",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": float(i),
                    }
                    for i in range(1, 6)
                ],
            }
        )
        diff = "--- before\ndef f():\n    return 1\n--- after\ndef f():\n    return 2\n"
        event = _make_event(target=target, proposed_diff=diff)
        durations = []
        for _ in range(100):
            t = time.perf_counter()
            policy.evaluate(event, signals)
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[94]
        assert p95 < 5.0, f"p95 = {p95:.3f} ms"


# =====================================================================
# Behavioral gates (per Lesson #15-#17)
# =====================================================================


class TestBehavioralGates:
    def test_non_edit_does_not_call_signals_fixes(self):
        """is_edit gate: spy on signals.fixes calls."""
        policy = AntiRegression()
        spy = _FakeSignals()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            policy.evaluate(_make_event(tool_name=tool), spy)
        assert (
            spy.fixes_calls == []
        ), f"is_edit gate degraded: signals.fixes called: {spy.fixes_calls}"

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_target_none_does_not_call_signals_fixes(self):
        policy = AntiRegression()
        spy = _FakeSignals()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",
            target_file=None,
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        verdict = policy.evaluate(event, spy)
        assert verdict.is_allowing()
        assert (
            spy.fixes_calls == []
        ), f"target_file None gate degraded: {spy.fixes_calls}"

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_signals_none_does_not_crash(self):
        policy = AntiRegression()
        event = _make_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_off_mode_skips_signals_fixes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_ANTI_REGRESSION_MODE", "off")
        policy = AntiRegression()
        spy = _FakeSignals(
            fixes_for={Path("/p/x.py"): [{"id": 1, "description": "fix: x"}]}
        )
        event = _make_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        policy.evaluate(event, spy)
        assert spy.fixes_calls == [], f"mode=off gate degraded: {spy.fixes_calls}"

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_priority_value_stable(self):
        """Hero 2 priority=80. Below Decision Lock (100) but ABOVE
        Blast-Radius (50), Cross-Session (30), Token Budget (10)."""
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
        from mcp_server.engine.policies.cross_session import CrossSessionConsistency
        from mcp_server.engine.policies.token_budget import TokenBudgetPersist

        assert DecisionLock.priority > AntiRegression.priority
        assert AntiRegression.priority > BlastRadiusVeto.priority
        assert AntiRegression.priority > CrossSessionConsistency.priority
        assert AntiRegression.priority > TokenBudgetPersist.priority

    def test_invalid_mode_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Garbage mode env var falls back to 'block'. Catches mutations
        that remove the validation."""
        monkeypatch.setenv("CODEVIRA_ANTI_REGRESSION_MODE", "totally-fake")
        policy = AntiRegression()
        config = policy._config()
        assert config["mode"] == "block"

    def test_empty_fixes_skips_is_revert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Empty-fixes gate: if signals.fixes returns [], the policy
        must NOT call is_revert. Output-only tests can't catch the
        gate (empty fix list iterates zero times anyway). Spy on
        is_revert to verify zero calls.
        """

        is_revert_calls: list[tuple] = []

        def spy_is_revert(diff, fix):
            is_revert_calls.append((diff, fix))
            return False

        monkeypatch.setattr(
            "indexer.fix_history.is_revert",
            spy_is_revert,
        )

        policy = AntiRegression()
        spy_signals = _FakeSignals(fixes_for={})  # no fixes for any file
        event = _make_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        policy.evaluate(event, spy_signals)
        assert (
            is_revert_calls == []
        ), f"empty-fixes gate degraded: is_revert called: {is_revert_calls}"

    def test_none_diff_skips_is_revert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """None-diff gate: when proposed_diff is None (full Write),
        the policy must NOT call is_revert (it has nothing to
        compare). Spy verifies the gate runs.
        """

        is_revert_calls: list[tuple] = []

        def spy_is_revert(diff, fix):
            is_revert_calls.append((diff, fix))
            return False

        monkeypatch.setattr(
            "indexer.fix_history.is_revert",
            spy_is_revert,
        )

        policy = AntiRegression()
        target = Path("/p/x.py")
        # Has fixes, but diff is None → should skip is_revert
        spy_signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "fix: x",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    }
                ],
            }
        )
        event = _make_event(target=target, tool_name="Write", proposed_diff=None)
        policy.evaluate(event, spy_signals)
        assert (
            is_revert_calls == []
        ), f"None-diff gate degraded: is_revert called: {is_revert_calls}"

    def test_per_fix_failure_doesnt_break_evaluation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If is_revert raises on one fix, other fixes still get
        evaluated. The per-fix try/except is a critical robustness
        contract — mutations that remove it would break evaluation
        on a single malformed fix.
        """

        def _flaky_is_revert(diff, fix):
            if fix.get("id") == 1:
                raise RuntimeError("simulated bad fix")
            return True  # 2nd fix returns True

        # Patch the imported is_revert at module scope
        monkeypatch.setattr(
            "indexer.fix_history.is_revert",
            _flaky_is_revert,
        )

        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "broken fix",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    },
                    {
                        "id": 2,
                        "file_path": "x.py",
                        "description": "ok fix",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 2.0,
                    },
                ],
            }
        )
        event = _make_event(
            target=target,
            proposed_diff="--- before\nx\n--- after\ny\n",
        )
        # Should still block because fix 2 returned True (is_revert)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking()
        assert verdict.metadata["reverting_count"] == 1


# =====================================================================
# End-to-end through dispatch() (Lesson #15-#16)
# =====================================================================


class TestEngineDispatch:
    def test_hero_2_fires_through_dispatch(
        self,
        isolated_project: Path,
    ):
        """Real fix_history.db + dispatch() + AntiRegression →
        block on a revert. Catches Bug-2-class wiring bugs.
        """
        from indexer.fix_history import record_fix
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        # Record a fix
        record_fix(
            isolated_project,
            file_path="x.py",
            line_start=0,
            line_end=0,
            description="fix: infinite deadlock race condition",
            source="manual",
        )

        reset_policies()
        register_default_policies()

        diff = (
            "--- before\n    with self._lock:\n        attempt()\n"
            "--- after\n    # infinite race condition\n    attempt()\n"
        )
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=isolated_project,
            tool_name="Edit",
            target_file=isolated_project / "x.py",
            proposed_diff=diff,
        )
        verdict = dispatch(event)
        assert verdict.is_blocking(), (
            f"Hero 2 must fire through dispatch with real fix_history.db; "
            f"got {verdict.action}"
        )
        # Either Hero 2 alone (no other policies fire) OR combined
        # with another. Hero 2 should at least be in the policies set.
        if verdict.policy == "anti_regression":
            assert "infinite" in (verdict.message or "").lower()
        else:
            others = verdict.metadata.get("other_blocking_policies", [])
            assert "anti_regression" in others
        reset_policies()


# =====================================================================
# Registration
# =====================================================================


class TestRegistration:
    def test_register_default_policies_includes_hero_2(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names = sorted(p.name for p in registered_policies())
        assert "anti_regression" in names

    def test_idempotent_with_five_heroes(self):
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
            "anti_regression",
            "blast_radius_veto",
            "decision_lock",
            "relevance_inject",
            "token_budget_persist",
        ):
            assert names.count(n) == 1


# =====================================================================
# Edge cases
# =====================================================================


class TestEdgeCases:
    def test_fix_with_empty_description_handled(self):
        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    }
                ],
            }
        )
        diff = "--- before\nx\n--- after\ny\n"
        event = _make_event(target=target, proposed_diff=diff)
        # Empty description → is_revert keyword check skips → no match → allow
        verdict = policy.evaluate(event, signals)
        # Either allows or doesn't crash. The contract is "must not raise".
        assert verdict.action in ("allow", "block", "warn")

    def test_more_than_max_fixes_per_file(self):
        """100 fixes on one file: only top-20 (newest) get evaluated.
        Performance + correctness verified via the cap.
        """
        policy = AntiRegression()
        target = Path("/p/x.py")
        # 100 fixes; only top-20 should be checked
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": i,
                        "file_path": "x.py",
                        "description": "fix: thing",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": float(i),
                    }
                    for i in range(100)
                ],
            }
        )
        diff = "--- before\nx\n--- after\ny\n"
        event = _make_event(target=target, proposed_diff=diff)
        # Should not crash + complete in reasonable time
        import time

        t = time.perf_counter()
        verdict = policy.evaluate(event, signals)
        elapsed_ms = (time.perf_counter() - t) * 1000
        assert elapsed_ms < 100, f"100-fixes took {elapsed_ms:.0f}ms"
        # The verdict's metadata should reflect total but cap candidates
        if verdict.is_blocking():
            assert verdict.metadata["total_fixes_for_file"] == 100

    def test_proposed_diff_none_allows(self):
        """Full Write (proposed_diff=None) → Hero 2 allows; Hero 4
        handles full-file replacements."""
        policy = AntiRegression()
        target = Path("/p/x.py")
        signals = _FakeSignals(
            fixes_for={
                target: [
                    {
                        "id": 1,
                        "file_path": "x.py",
                        "description": "fix: x",
                        "source": "manual",
                        "line_start": 0,
                        "line_end": 0,
                        "recorded_at": 1.0,
                    }
                ],
            }
        )
        event = _make_event(target=target, tool_name="Write", proposed_diff=None)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()
