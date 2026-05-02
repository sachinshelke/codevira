"""Tests for mcp_server.engine.policies.

Covers PolicyVerdict construction helpers and Policy base-class contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies import Policy, PolicyVerdict


class TestPolicyVerdictConstructors:
    def test_allow_default(self):
        v = PolicyVerdict.allow()
        assert v.action == "allow"
        assert v.message is None
        assert v.metadata == {}

    def test_allow_with_metadata(self):
        v = PolicyVerdict.allow(metadata={"k": "v"})
        assert v.metadata == {"k": "v"}

    def test_warn(self):
        v = PolicyVerdict.warn("careful here")
        assert v.action == "warn"
        assert v.message == "careful here"

    def test_block(self):
        v = PolicyVerdict.block("nope")
        assert v.action == "block"
        assert v.message == "nope"
        assert v.is_blocking()

    def test_inject(self):
        v = PolicyVerdict.inject("here is context", message="for AI")
        assert v.action == "inject"
        assert v.inject_context == "here is context"
        assert v.message == "for AI"

    def test_predicates(self):
        assert PolicyVerdict.allow().is_allowing()
        assert PolicyVerdict.block("x").is_blocking()
        assert not PolicyVerdict.warn("x").is_blocking()
        assert not PolicyVerdict.inject("c").is_allowing()


class TestPolicyBase:
    def test_default_evaluate_allows(self):
        class P(Policy):
            name = "p1"
            handles = (EventType.PRE_TOOL_USE,)

        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
        )
        verdict = P().evaluate(event)
        assert verdict.action == "allow"

    def test_subclass_can_override(self):
        class Blocker(Policy):
            name = "blocker"
            handles = (EventType.PRE_TOOL_USE,)

            def evaluate(self, event):
                return PolicyVerdict.block("nope")

        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
        )
        v = Blocker().evaluate(event)
        assert v.is_blocking()

    def test_config_schema_default_empty(self):
        class P(Policy):
            name = "p"
            handles = ()
        assert P().config_schema() == {}
