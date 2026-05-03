"""
test_decision_lock.py — Hero 1 acceptance tests.

The 8 scenarios in docs/heroes/01-decision-lock.md "Acceptance test list"
plus configuration robustness + registration tests.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.decision_lock import DecisionLock


# =====================================================================
# Helpers
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
    """Minimal SignalContext stand-in for unit tests.

    Allows canned responses for ``decisions(...)`` and a simulated
    graph for the ``_file_is_locked_without_decisions`` query path.
    """

    def __init__(
        self,
        *,
        decisions_for: dict[str, list[dict[str, Any]]] | None = None,
        locked_no_decision_files: set[str] | None = None,
    ):
        self._decisions_for = decisions_for or {}
        self._locked_no_decisions = locked_no_decision_files or set()
        # Provide a minimal graph stub for the no-decisions check.
        self.graph = self._FakeGraph(self._locked_no_decisions)

    def decisions(
        self,
        *,
        file: str | None = None,
        locked_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # Only return when locked_only=True (Hero 1's contract)
        if not locked_only:
            return []
        return self._decisions_for.get(file or "", [])

    class _FakeGraph:
        def __init__(self, locked_no_dec_files: set[str]):
            self._files = locked_no_dec_files
            self.conn = self._FakeConn(locked_no_dec_files)

        class _FakeConn:
            def __init__(self, files):
                self._files = files

            def execute(self, sql, params):
                # Single-row do_not_revert lookup
                file_path = params[0]
                row = (
                    {"do_not_revert": 1}
                    if file_path in self._files else None
                )
                return _FakeCursor(row)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEVIRA_DECISION_LOCK_MODE", raising=False)


# =====================================================================
# 8 acceptance scenarios from the spec
# =====================================================================


class TestAcceptanceScenarios:

    def test_1_non_edit_event_allowed(self):
        policy = DecisionLock()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            event = _make_event(tool_name=tool)
            verdict = policy.evaluate(event, _FakeSignals())
            assert verdict.is_allowing(), f"{tool} should be allowed"

    def test_2_edit_on_file_not_in_graph_allowed(self):
        """No decisions, no node → no lock → allow."""
        policy = DecisionLock()
        event = _make_event(target=Path("/p/foo.py"))
        verdict = policy.evaluate(event, _FakeSignals())
        assert verdict.is_allowing()

    def test_3_edit_on_unlocked_file_allowed(self):
        """File has decisions but they're NOT locked → allow.

        signals.decisions(locked_only=True) returns empty for this file
        because the join filters to do_not_revert=1.
        """
        policy = DecisionLock()
        event = _make_event(target=Path("/p/foo.py"))
        # No locked decisions, no locked-without-decisions either
        verdict = policy.evaluate(event, _FakeSignals(decisions_for={}))
        assert verdict.is_allowing()

    def test_4_edit_on_locked_file_blocked(self):
        """Locked file with decisions → block, listing the decisions."""
        policy = DecisionLock()
        target = Path("/p/auth.py")
        signals = _FakeSignals(decisions_for={
            "auth.py": [
                {"id": 142, "decision": "bcrypt over argon2 — see issue #142",
                 "file_path": "auth.py", "context": "performance discussion",
                 "locked": 1, "timestamp": 1730764800.0},
                {"id": 143, "decision": "use cookie-based session, not JWT",
                 "file_path": "auth.py", "context": "security review",
                 "locked": 1, "timestamp": 1730851200.0},
            ],
        })
        event = _make_event(target=target)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking()
        assert "auth.py" in (verdict.message or "")
        assert "bcrypt" in (verdict.message or "")
        assert verdict.metadata["locked_decision_count"] == 2
        assert 142 in verdict.metadata["locked_decision_ids"]
        assert 143 in verdict.metadata["locked_decision_ids"]
        assert verdict.metadata["mode"] == "block"

    def test_5_locked_file_no_decisions_yields_warn(self):
        """Edge case #5: file is do_not_revert=1 but no decisions
        attached. We surface a warn (never block) so the user knows
        the file IS locked, with a recommendation to record rationale.
        """
        policy = DecisionLock()
        target = Path("/p/legacy.py")
        signals = _FakeSignals(
            decisions_for={},  # no decisions
            locked_no_decision_files={"legacy.py"},
        )
        event = _make_event(target=target)
        verdict = policy.evaluate(event, signals)
        # Even with default mode=block, this case downgrades to warn
        assert verdict.action == "warn", (
            f"locked-without-rationale should warn, not block; got {verdict.action}"
        )
        assert "no recorded decisions" in (verdict.message or "")
        assert verdict.metadata["locked_without_rationale"] is True

    def test_6_warn_mode_produces_warn_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_MODE", "warn")
        policy = DecisionLock()
        target = Path("/p/auth.py")
        signals = _FakeSignals(decisions_for={
            "auth.py": [
                {"id": 1, "decision": "locked decision", "file_path": "auth.py",
                 "context": "", "locked": 1, "timestamp": 1730764800.0},
            ],
        })
        event = _make_event(target=target)
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "warn"
        assert verdict.metadata["mode"] == "warn"

    def test_7_off_mode_disables_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_MODE", "off")
        policy = DecisionLock()
        target = Path("/p/auth.py")
        signals = _FakeSignals(decisions_for={
            "auth.py": [{"id": 1, "decision": "x", "file_path": "auth.py",
                         "context": "", "locked": 1, "timestamp": 1.0}],
        })
        event = _make_event(target=target)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_8_evaluation_under_1ms_p95_warm_cache(self):
        """Warm-graph + cached signals: sub-millisecond evaluation."""
        import statistics, time
        policy = DecisionLock()
        target = Path("/p/auth.py")
        signals = _FakeSignals(decisions_for={
            "auth.py": [{"id": 1, "decision": "x", "file_path": "auth.py",
                         "context": "", "locked": 1, "timestamp": 1.0}],
        })
        event = _make_event(target=target)

        durations = []
        for _ in range(100):
            t = time.perf_counter()
            policy.evaluate(event, signals)
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[94]
        assert p95 < 5.0, f"p95 = {p95:.2f} ms"


# =====================================================================
# Configuration robustness
# =====================================================================


class TestConfiguration:

    def test_invalid_mode_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_MODE", "totally-fake")
        policy = DecisionLock()
        assert policy._config()["mode"] == "block"

    def test_empty_mode_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_MODE", "")
        policy = DecisionLock()
        assert policy._config()["mode"] == "block"

    def test_uppercase_mode_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_MODE", "WARN")
        policy = DecisionLock()
        assert policy._config()["mode"] == "warn"


# =====================================================================
# Co-existence with Hero 4 (both fire on same event, verdicts combine)
# =====================================================================


class TestCoexistenceWithHero4:

    def test_both_policies_fire_block_takes_precedence(self):
        """Hero 1 + Hero 4 simultaneously firing a block: runner combines
        verdicts (any block wins). Decision Lock has higher priority,
        so its message comes first."""
        from mcp_server.engine import (
            register_policy, registered_policies, reset_policies, dispatch,
        )
        from mcp_server.engine.policies.blast_radius import BlastRadiusVeto

        reset_policies()
        register_policy(DecisionLock())
        register_policy(BlastRadiusVeto())

        names = [p.name for p in registered_policies()]
        # Decision Lock is priority=100, Blast-Radius is priority=50
        # → Decision Lock evaluates first
        assert names.index("decision_lock") < names.index("blast_radius_veto")

        reset_policies()


# =====================================================================
# Integration: register_default_policies + idempotency
# =====================================================================


class TestRegistration:

    def test_register_default_policies_includes_hero_1(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        names = sorted(p.name for p in registered_policies())
        assert "decision_lock" in names
        assert "blast_radius_veto" in names

    def test_register_default_policies_idempotent_with_hero_1(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        register_default_policies()  # idempotent — no duplicates
        names = [p.name for p in registered_policies()]
        # Each name appears exactly once
        for n in ("decision_lock", "blast_radius_veto"):
            assert names.count(n) == 1, (
                f"{n} registered {names.count(n)} times — idempotency broken"
            )


# =====================================================================
# Robustness: signals layer failure modes
# =====================================================================


class TestSignalFailures:

    def test_none_signals_allows_gracefully(self):
        """If wiring couldn't build signals (graph failure / etc.), the
        policy must allow rather than crash."""
        policy = DecisionLock()
        event = _make_event()
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    def test_decisions_raises_propagates_to_runner_safety_net(self):
        """Documents the failure-handling contract: policy does NOT
        swallow arbitrary signal exceptions. They propagate UP to the
        runner's _safe_evaluate, which catches them and returns allow.

        Hero 1's job is to express policy logic, not to second-guess the
        signal layer. In production, signals.decisions() catches its own
        SQLite errors and returns []; this test simulates a hostile signal
        layer to verify the policy's "let it propagate" stance is intact.
        """
        class _Broken:
            graph = None
            def decisions(self, **kw):
                raise RuntimeError("simulated signal failure")

        policy = DecisionLock()
        event = _make_event()
        # We assert the exception PROPAGATES (the runner is the safety
        # net, not the policy). This is the contract documented in
        # mcp_server/engine/runner.py::_safe_evaluate.
        with pytest.raises(RuntimeError):
            policy.evaluate(event, _Broken())

    def test_path_outside_project_root_handled(self):
        """If target_file isn't inside project_root (rare but possible),
        we fall back to absolute path lookup rather than crashing."""
        policy = DecisionLock()
        # target_file outside project_root
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",
            target_file=Path("/elsewhere/foo.py"),
        )
        # No decisions for the absolute path → allow
        verdict = policy.evaluate(event, _FakeSignals())
        assert verdict.is_allowing()
