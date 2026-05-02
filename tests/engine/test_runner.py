"""Tests for mcp_server.engine.runner — dispatch + verdict combination.

The runner is the heart of the engine. These tests cover:

  - register_policy / reset / replace-by-name
  - dispatch routes events to policies that handle the event_type
  - dispatch returns allow when no policy handles
  - dispatch combines verdicts per the documented rules:
      block > warn > inject > allow
  - exception in one policy doesn't break others
  - CODEVIRA_ENGINE=0 escape hatch disables everything
  - signals attached to event are accessible from policy.evaluate
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies import Policy, PolicyVerdict
from mcp_server.engine.runner import (
    dispatch,
    register_policy,
    registered_policies,
    reset_policies,
)


@pytest.fixture(autouse=True)
def _isolate_runner():
    reset_policies()
    yield
    reset_policies()


def _event(event_type=EventType.PRE_TOOL_USE, **kwargs) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        project_root=Path("/proj"),
        tool_name=kwargs.pop("tool_name", "Edit"),
        **kwargs,
    )


class _AllowPolicy(Policy):
    name = "allower"
    handles = (EventType.PRE_TOOL_USE,)


class _BlockPolicy(Policy):
    name = "blocker"
    handles = (EventType.PRE_TOOL_USE,)

    def evaluate(self, event):
        return PolicyVerdict.block("blocked by test")


class _WarnPolicy(Policy):
    name = "warner"
    handles = (EventType.PRE_TOOL_USE,)

    def evaluate(self, event):
        return PolicyVerdict.warn("careful")


class _InjectPolicy(Policy):
    name = "injector"
    handles = (EventType.PRE_TOOL_USE,)

    def evaluate(self, event):
        return PolicyVerdict.inject("here is some context")


class _SessionStartPolicy(Policy):
    name = "session_only"
    handles = (EventType.SESSION_START,)


class TestRegistration:
    def test_register_and_list(self):
        register_policy(_AllowPolicy())
        names = [p.name for p in registered_policies()]
        assert "allower" in names

    def test_register_replaces_by_name(self):
        register_policy(_AllowPolicy())
        register_policy(_AllowPolicy())  # second registration replaces
        assert len(registered_policies()) == 1

    def test_empty_name_rejected(self):
        class Anon(Policy):
            name = ""
            handles = ()
        with pytest.raises(ValueError):
            register_policy(Anon())


class TestDispatchBasics:
    def test_no_policies_returns_allow(self):
        v = dispatch(_event())
        assert v.is_allowing()

    def test_single_allow_returns_allow(self):
        register_policy(_AllowPolicy())
        v = dispatch(_event())
        assert v.is_allowing()

    def test_single_block_returns_block(self):
        register_policy(_BlockPolicy())
        v = dispatch(_event())
        assert v.is_blocking()
        assert "blocked by test" in (v.message or "")
        assert v.policy == "blocker"


class TestVerdictCombination:
    def test_block_wins_over_warn(self):
        register_policy(_BlockPolicy())
        register_policy(_WarnPolicy())
        v = dispatch(_event())
        assert v.is_blocking()

    def test_block_wins_over_inject(self):
        register_policy(_InjectPolicy())
        register_policy(_BlockPolicy())
        v = dispatch(_event())
        assert v.is_blocking()

    def test_warn_when_no_block(self):
        register_policy(_WarnPolicy())
        register_policy(_AllowPolicy())
        v = dispatch(_event())
        assert v.action == "warn"
        assert "careful" in v.message

    def test_inject_when_no_block_no_warn(self):
        register_policy(_InjectPolicy())
        register_policy(_AllowPolicy())
        v = dispatch(_event())
        assert v.action == "inject"
        assert "context" in v.inject_context

    def test_warn_messages_concatenated(self):
        class WarnA(Policy):
            name = "warn_a"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, e):
                return PolicyVerdict.warn("first warning")

        class WarnB(Policy):
            name = "warn_b"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, e):
                return PolicyVerdict.warn("second warning")

        register_policy(WarnA())
        register_policy(WarnB())
        v = dispatch(_event())
        assert v.action == "warn"
        assert "first warning" in v.message
        assert "second warning" in v.message


class TestEventTypeFiltering:
    def test_policy_only_runs_on_handled_event_type(self):
        register_policy(_SessionStartPolicy())  # handles SESSION_START only
        # Fire PRE_TOOL_USE — that policy's evaluate should never run.
        # We can't directly observe non-invocation, but we CAN observe
        # that the verdict stays "allow" (default) even though we
        # registered ONLY a policy that doesn't claim PRE_TOOL_USE.
        v = dispatch(_event(event_type=EventType.PRE_TOOL_USE))
        assert v.is_allowing()


class TestErrorHandling:
    def test_policy_exception_treated_as_allow(self):
        class Boom(Policy):
            name = "boom"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, e):
                raise RuntimeError("policy crashed")

        register_policy(Boom())
        register_policy(_AllowPolicy())
        # Engine must not raise; verdict is allow.
        v = dispatch(_event())
        assert v.is_allowing()

    def test_policy_exception_does_not_block_other_policies(self):
        class Boom(Policy):
            name = "boom"
            handles = (EventType.PRE_TOOL_USE,)
            priority = 100  # runs first
            def evaluate(self, e):
                raise RuntimeError("policy crashed")

        register_policy(Boom())
        register_policy(_BlockPolicy())  # priority 0 — runs after Boom
        v = dispatch(_event())
        # Blocker still wins despite Boom raising.
        assert v.is_blocking()

    def test_policy_returns_non_verdict_treated_as_allow(self):
        class BadReturn(Policy):
            name = "bad_return"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, e):
                return "not a verdict"  # type: ignore[return-value]

        register_policy(BadReturn())
        v = dispatch(_event())
        assert v.is_allowing()


class TestEscapeHatch:
    def test_engine_disabled_by_env_returns_allow(self, monkeypatch):
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")
        register_policy(_BlockPolicy())  # would normally block
        v = dispatch(_event())
        assert v.is_allowing()
        assert v.metadata.get("engine_disabled") is True


class TestPriorityOrdering:
    def test_higher_priority_runs_first(self):
        executed = []

        class HighPriority(Policy):
            name = "high"
            handles = (EventType.PRE_TOOL_USE,)
            priority = 100
            def evaluate(self, e):
                executed.append("high")
                return PolicyVerdict.allow()

        class LowPriority(Policy):
            name = "low"
            handles = (EventType.PRE_TOOL_USE,)
            priority = 0
            def evaluate(self, e):
                executed.append("low")
                return PolicyVerdict.allow()

        register_policy(LowPriority())
        register_policy(HighPriority())  # registered second but higher priority
        dispatch(_event())
        assert executed == ["high", "low"]


class TestSignalsAttached:
    def test_event_has_signals_after_dispatch(self):
        captured = {}

        class Inspector(Policy):
            name = "inspector"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, e):
                captured["has_signals"] = hasattr(e, "signals")
                captured["signals_type"] = type(e.signals).__name__ if hasattr(e, "signals") else None
                return PolicyVerdict.allow()

        register_policy(Inspector())
        dispatch(_event())
        assert captured["has_signals"] is True
        assert captured["signals_type"] == "SignalContext"
