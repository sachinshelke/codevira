"""Tests for mcp_server.engine.events.

Covers HookEvent construction, immutability, and the convenience predicates
(is_edit, is_read).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent


class TestHookEventBasics:
    def test_construct_pre_tool_use(self):
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Edit",
            tool_input={"file_path": "src/foo.py"},
        )
        assert event.event_type == EventType.PRE_TOOL_USE
        assert event.project_root == Path("/proj")
        assert event.tool_name == "Edit"

    def test_event_is_frozen(self):
        event = HookEvent(
            event_type=EventType.SESSION_START,
            project_root=Path("/proj"),
        )
        with pytest.raises(FrozenInstanceError):
            event.tool_name = "Edit"  # type: ignore[misc]

    def test_default_field_values(self):
        event = HookEvent(
            event_type=EventType.STOP,
            project_root=Path("/proj"),
        )
        assert event.ai_tool == "unknown"
        assert event.session_id is None
        assert event.tool_name == ""
        assert event.tool_input == {}
        assert event.target_file is None
        assert event.proposed_diff is None


class TestEventTypePredicates:
    def test_is_edit_true_for_edit_tools(self):
        for tool in ["Edit", "Write", "MultiEdit", "NotebookEdit"]:
            event = HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=Path("/proj"),
                tool_name=tool,
            )
            assert event.is_edit() is True, f"{tool} should be edit"

    def test_is_edit_false_for_post_tool_use(self):
        event = HookEvent(
            event_type=EventType.POST_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Edit",
        )
        assert event.is_edit() is False  # post != pre

    def test_is_edit_false_for_read(self):
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Read",
        )
        assert event.is_edit() is False

    def test_is_read_true_for_read_tools(self):
        for tool in ["Read", "Glob", "Grep"]:
            event = HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=Path("/proj"),
                tool_name=tool,
            )
            assert event.is_read() is True

    def test_event_type_is_string_enum(self):
        # EventType inherits from str so it serializes naturally to JSON.
        assert EventType.PRE_TOOL_USE == "pre_tool_use"
        assert str(EventType.SESSION_START) == "session_start"
