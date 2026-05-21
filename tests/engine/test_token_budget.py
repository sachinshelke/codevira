"""
test_token_budget.py — Hero 6 acceptance + behavioral + mutation tests.

Applying Tier-0 pre-flight discipline (Lessons #15-#17 from the Week-5
retrospective) FROM THE START:
  1. Tests use real ``token_budget.jsonl`` files via ``tmp_path`` —
     no mocked persistence layer that could hide schema bugs.
  2. Behavioral spies on ``end_session`` for gate verification.
  3. End-to-end test through ``dispatch()`` against a real session
     meter + persisted JSONL.
  4. CLI tested via subprocess so the entire arg-parse → cmd_budget →
     read_session_history chain is exercised.
  5. 10+ mutations from start, not 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.token_budget import TokenBudgetPersist


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_stop_event(
    *,
    session_id: str | None = "test-session",
    project_root: Path,
) -> HookEvent:
    return HookEvent(
        event_type=EventType.STOP,
        project_root=project_root,
        session_id=session_id,
    )


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp_path-based project with codevira data dir under a fake home.

    The CLI subprocess uses ``Path.home() / '.codevira'`` to find the
    data dir. To make in-process writes (via the monkey-patched
    ``get_global_home``) land at the SAME location the subprocess
    will read from, ``get_global_home`` must return
    ``fake_home / '.codevira'`` — matching production layout.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # The codevira data dir is ~/.codevira/projects/... in production.
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
    monkeypatch.delenv("CODEVIRA_TOKEN_BUDGET_MODE", raising=False)


# =====================================================================
# Acceptance scenarios from the spec
# =====================================================================


class TestAcceptanceScenarios:
    def test_1_stop_without_session_id_allows(self, isolated_project: Path):
        policy = TokenBudgetPersist()
        event = _make_stop_event(session_id=None, project_root=isolated_project)
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        # No persistence happened
        assert (
            verdict.metadata.get("persisted") is None
            or verdict.metadata.get("persisted") is False
        )

    def test_2_stop_with_active_meter_persists(self, isolated_project: Path):
        """Real-DB integration: create a meter, fire Stop, verify
        the JSONL file got a line written."""
        from mcp_server.engine.token_meter import (
            get_or_create_session_meter,
            reset_meters,
        )

        reset_meters()
        m = get_or_create_session_meter("session-x")
        m.record_injected(500, source="get_node")
        m.record_used(200, source="get_node")

        policy = TokenBudgetPersist()
        event = _make_stop_event(session_id="session-x", project_root=isolated_project)
        verdict = policy.evaluate(event, None)

        assert verdict.is_allowing()
        assert verdict.metadata.get("persisted") is True
        assert verdict.metadata["session_id"] == "session-x"
        assert verdict.metadata["injected_total"] == 500
        assert verdict.metadata["used_total"] == 200

        # The JSONL file exists and contains the session record
        from mcp_server.paths import _sanitize_path_key, get_global_home

        log_path = (
            get_global_home()
            / "projects"
            / _sanitize_path_key(isolated_project.resolve())
            / "logs"
            / "token_budget.jsonl"
        )
        assert log_path.exists(), f"JSONL not written at {log_path}"
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["session_id"] == "session-x"
        assert record["injected_total"] == 500
        assert record["used_total"] == 200

    def test_3_persist_failure_doesnt_crash(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If end_session raises (disk full, perm denied, etc.), the
        policy returns allow with error metadata — never propagates."""

        def _raising_end_session(*args, **kwargs):
            raise OSError("simulated disk full")

        monkeypatch.setattr(
            "mcp_server.engine.token_meter.end_session",
            _raising_end_session,
        )

        policy = TokenBudgetPersist()
        event = _make_stop_event(project_root=isolated_project)
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert verdict.metadata.get("persisted") is False
        assert "error" in verdict.metadata

    def test_4_non_stop_event_passes_through(self, isolated_project: Path):
        policy = TokenBudgetPersist()
        for et in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.SESSION_START,
            EventType.USER_PROMPT_SUBMIT,
        ):
            event = HookEvent(
                event_type=et,
                project_root=isolated_project,
                session_id="x",
            )
            verdict = policy.evaluate(event, None)
            assert verdict.is_allowing()
            # No persist metadata on pass-through
            assert "persisted" not in verdict.metadata

    # v2.2.0+: test_5_budget_cli_no_sessions_yet removed (budget CLI deleted in surface-cut audit).

    # v2.2.0+: test_6_budget_cli_after_session_persists removed (budget CLI deleted in surface-cut audit).

    # v2.2.0+: test_7_budget_history_last_n removed (budget CLI deleted in surface-cut audit).

    def test_8_evaluate_under_5ms_p99(self, isolated_project: Path):
        import time

        policy = TokenBudgetPersist()
        # No session_id → fast path
        event = _make_stop_event(session_id=None, project_root=isolated_project)
        durations = []
        for _ in range(100):
            t = time.perf_counter()
            policy.evaluate(event, None)
            durations.append((time.perf_counter() - t) * 1000)
        p99 = sorted(durations)[98]
        assert p99 < 5.0, f"p99 = {p99:.3f} ms"


# =====================================================================
# Behavioral gates (Tier-0 pre-flight: spy on end_session calls)
# =====================================================================


class TestBehavioralGates:
    """Behavioral spies catch gates that output-only assertions miss.
    Per Lesson #15-17 from the Week-5 retrospective."""

    def test_event_type_gate_skips_end_session(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Non-STOP events must NOT call end_session()."""
        calls: list[tuple] = []

        def spy_end_session(session_id, *, project_root=None):
            calls.append((session_id, project_root))
            return None

        monkeypatch.setattr(
            "mcp_server.engine.token_meter.end_session",
            spy_end_session,
        )

        policy = TokenBudgetPersist()
        for et in (
            EventType.PRE_TOOL_USE,
            EventType.SESSION_START,
            EventType.USER_PROMPT_SUBMIT,
            EventType.POST_TOOL_USE,
        ):
            event = HookEvent(
                event_type=et,
                project_root=isolated_project,
                session_id="x",
            )
            policy.evaluate(event, None)

        assert calls == [], (
            f"event_type gate degraded: end_session called on non-STOP "
            f"events: {calls}"
        )

    def test_session_id_none_gate_skips_end_session(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """STOP event with session_id=None must NOT call end_session."""
        calls: list[tuple] = []

        def spy_end_session(session_id, *, project_root=None):
            calls.append((session_id, project_root))
            return None

        monkeypatch.setattr(
            "mcp_server.engine.token_meter.end_session",
            spy_end_session,
        )

        policy = TokenBudgetPersist()
        event = _make_stop_event(session_id=None, project_root=isolated_project)
        policy.evaluate(event, None)

        assert (
            calls == []
        ), f"session_id None gate degraded: end_session called: {calls}"

    def test_off_mode_skips_end_session(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_TOKEN_BUDGET_MODE", "off")
        calls: list[tuple] = []

        def spy_end_session(session_id, *, project_root=None):
            calls.append((session_id, project_root))
            return None

        monkeypatch.setattr(
            "mcp_server.engine.token_meter.end_session",
            spy_end_session,
        )

        policy = TokenBudgetPersist()
        event = _make_stop_event(project_root=isolated_project)
        policy.evaluate(event, None)
        assert calls == [], f"mode=off gate degraded: end_session called: {calls}"

    def test_invalid_mode_does_not_disable_policy(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Garbage env var (e.g. 'totally-fake') must fall back to
        'persist' default. Otherwise `mode != "off"` would be True and
        the policy would still run, but the actual operation depends
        on the unvalidated mode_raw passing through.

        Set garbage env, fire a STOP event with an active meter,
        verify persistence still happens (because validation falls
        back to default 'persist' instead of trusting the garbage).
        """
        monkeypatch.setenv("CODEVIRA_TOKEN_BUDGET_MODE", "totally-fake")

        from mcp_server.engine.token_meter import (
            get_or_create_session_meter,
            reset_meters,
        )

        reset_meters()
        m = get_or_create_session_meter("invalid-mode-test")
        m.record_injected(100)

        policy = TokenBudgetPersist()
        config = policy._config()
        # Validation kicks in: garbage falls back to default 'persist'
        assert config["mode"] == "persist", (
            f"garbage CODEVIRA_TOKEN_BUDGET_MODE not validated; "
            f"got {config['mode']!r}"
        )

        event = _make_stop_event(
            session_id="invalid-mode-test",
            project_root=isolated_project,
        )
        verdict = policy.evaluate(event, None)
        # Persistence happens because mode falls back to 'persist'
        assert verdict.metadata.get("persisted") is True

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_enabled_by_default_false_skips_registration(self):
        """Bug 3 (Week-7 retrospective): the ``enabled_by_default``
        field on Policy was declared but never checked. Setting it
        False on a default-registered hero had no effect — the hero
        was always registered. Now the registration helper honors
        the flag.

        Test: simulate a hero with enabled_by_default=False and
        verify it's NOT auto-registered.
        """
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        # We can't easily mutate Hero 6's class flag at test time
        # without breaking other tests. Instead, verify the
        # register_default_policies contract via a temporary class.
        from mcp_server.engine.policies.token_budget import TokenBudgetPersist

        reset_policies()
        # Save the original flag and flip it
        original = TokenBudgetPersist.enabled_by_default
        try:
            TokenBudgetPersist.enabled_by_default = False
            register_default_policies()
            names = {p.name for p in registered_policies()}
            assert "token_budget_persist" not in names, (
                "enabled_by_default=False not honored; policy was " "still registered"
            )
            # Other heroes still register
            assert "decision_lock" in names
            assert "blast_radius_veto" in names
        finally:
            TokenBudgetPersist.enabled_by_default = original
            reset_policies()

    @pytest.mark.skip(
        reason="v2.2.0: cross_session module deleted (replaced by relevance_inject)"
    )
    def test_priority_value_stable(self):
        """Hero 6 priority=10. Below all other heroes (1, 4, 5).
        Stop-event ordering matters if other STOP heroes exist later;
        we want telemetry to run AFTER any business-logic STOP policies.
        """
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
        from mcp_server.engine.policies.cross_session import CrossSessionConsistency

        for other in (DecisionLock, BlastRadiusVeto, CrossSessionConsistency):
            assert TokenBudgetPersist.priority < other.priority, (
                f"Hero 6 priority must be lowest; "
                f"{other.__name__}.priority = {other.priority}"
            )


# =====================================================================
# End-to-end through dispatch() (Lesson #15-#16)
# =====================================================================


class TestEngineDispatch:
    """Hero 6 must fire correctly via the runner's dispatch() path,
    not just direct evaluate() calls. Catches the same class of bug
    Week-5 retrospective fixed (signal-passing) — except this hero
    doesn't take signals, so the relevant test is "does dispatch
    actually invoke this policy on STOP events?"
    """

    def test_hero_6_fires_through_dispatch(
        self,
        isolated_project: Path,
    ):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.token_meter import (
            get_or_create_session_meter,
            reset_meters,
        )

        reset_policies()
        register_default_policies()
        # Policy is registered
        names = {p.name for p in registered_policies()}
        assert "token_budget_persist" in names

        # Set up a session with measured tokens
        reset_meters()
        m = get_or_create_session_meter("dispatch-test")
        m.record_injected(1500)
        m.record_used(750)

        event = _make_stop_event(
            session_id="dispatch-test",
            project_root=isolated_project,
        )
        verdict = dispatch(event)
        # Verdict is allow + metadata indicates persistence happened
        assert verdict.is_allowing()
        # The combined verdict comes from _combine; metadata may or
        # may not preserve our policy's metadata. Check the side-effect
        # (the JSONL file was written).
        from mcp_server.paths import _sanitize_path_key, get_global_home

        log_path = (
            get_global_home()
            / "projects"
            / _sanitize_path_key(isolated_project.resolve())
            / "logs"
            / "token_budget.jsonl"
        )
        assert log_path.exists()
        record = json.loads(log_path.read_text().splitlines()[-1])
        assert record["session_id"] == "dispatch-test"
        assert record["injected_total"] == 1500
        assert record["used_total"] == 750
        reset_policies()


# =====================================================================
# Edge cases (Lesson #15: real-data scenarios, not just happy path)
# =====================================================================


class TestEdgeCases:
    # v2.2.0+: test_corrupt_jsonl_doesnt_crash_cli removed (budget CLI deleted in surface-cut audit).

    def test_session_with_zero_tokens_persists_cleanly(
        self,
        isolated_project: Path,
    ):
        """A session that ran but never recorded tokens (e.g. the AI
        only Read files, no Edit/Write) still persists with totals=0.
        Verifies record_injected/record_used aren't required."""
        from mcp_server.engine.token_meter import (
            get_or_create_session_meter,
            reset_meters,
        )

        reset_meters()
        # Create the meter but don't record anything
        get_or_create_session_meter("zero-token-session")

        policy = TokenBudgetPersist()
        event = _make_stop_event(
            session_id="zero-token-session",
            project_root=isolated_project,
        )
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert verdict.metadata.get("persisted") is True
        assert verdict.metadata["injected_total"] == 0
        assert verdict.metadata["used_total"] == 0

    # v2.2.0+: test_clamp_last_to_valid_range removed (budget CLI deleted in surface-cut audit).
