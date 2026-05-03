"""Engine sprint integration test — proves wiring end-to-end.

The acceptance criterion from docs/heroes/00-engine.md:

  "One demo policy (a simple block if file ends with .py.bak) registers
  and works end-to-end through both Claude Code wiring AND MCP dispatch
  wiring."

This file exercises both paths.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server.engine.demo_policy import BackupExtensionGuard, maybe_register
from mcp_server.engine.runner import dispatch, register_policy, reset_policies
from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.wiring import claude_code_hooks, mcp_dispatch


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_policies()
    monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)
    yield
    reset_policies()


# ----------------------------------------------------------------------
# Direct dispatch path — register demo policy, fire event, expect block
# ----------------------------------------------------------------------

class TestDemoPolicyDirect:
    def test_blocks_py_bak_edit(self):
        register_policy(BackupExtensionGuard())
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Edit",
            target_file=Path("/proj/src/foo.py.bak"),
            tool_input={"file_path": "src/foo.py.bak"},
        )
        verdict = dispatch(event)
        assert verdict.is_blocking()
        assert "py.bak" in verdict.message.lower() or "backup" in verdict.message.lower()
        assert verdict.policy == "demo_backup_guard"

    def test_allows_normal_py_edit(self):
        register_policy(BackupExtensionGuard())
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Edit",
            target_file=Path("/proj/src/foo.py"),
            tool_input={"file_path": "src/foo.py"},
        )
        verdict = dispatch(event)
        assert verdict.is_allowing()

    def test_allows_read_of_py_bak(self):
        register_policy(BackupExtensionGuard())
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/proj"),
            tool_name="Read",
            target_file=Path("/proj/src/foo.py.bak"),
        )
        # Read is not an edit — policy allows.
        verdict = dispatch(event)
        assert verdict.is_allowing()


# ----------------------------------------------------------------------
# Claude Code hook wiring path — feed JSON on stdin, expect proper response
# ----------------------------------------------------------------------

class TestClaudeCodeHookWiring:
    def _run_handler(self, event_name, raw_input, monkeypatch):
        """Helper: stub stdin with raw_input as JSON; capture stdout."""
        register_policy(BackupExtensionGuard())
        # Pretend stdin is a pipe (not a TTY) so the handler reads it.
        stdin_buf = io.StringIO(json.dumps(raw_input))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        rc = claude_code_hooks.handle(event_name)
        return rc, stdout_buf.getvalue()

    def test_pre_tool_use_blocks_py_bak(self, tmp_path, monkeypatch):
        # Use a real project subdirectory rather than `/tmp` itself —
        # R4 QA added `is_invalid_project_root` to the wiring layer,
        # which correctly refuses `/tmp` (system dir).
        proj = tmp_path / "proj"
        proj.mkdir()
        target = proj / "foo.py.bak"
        target.touch()
        raw = {
            "session_id": "s1",
            "cwd": str(proj),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "x", "new_string": "y",
            },
        }
        rc, out = self._run_handler("PreToolUse", raw, monkeypatch)
        assert rc == 2  # Claude Code semantics: 2 = blocked
        payload = json.loads(out)
        assert payload["continue"] is False
        assert "py.bak" in payload["stopReason"].lower() or "backup" in payload["stopReason"].lower()

    def test_pre_tool_use_allows_normal_edit(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        target = proj / "foo.py"
        target.touch()
        raw = {
            "session_id": "s1",
            "cwd": str(proj),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "x", "new_string": "y",
            },
        }
        rc, out = self._run_handler("PreToolUse", raw, monkeypatch)
        assert rc == 0
        payload = json.loads(out)
        assert payload["continue"] is True

    def test_unknown_event_name_allows(self, monkeypatch):
        # Claude Code may add new event types we don't handle; we must allow.
        register_policy(BackupExtensionGuard())
        stdin_buf = io.StringIO("{}")
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        rc = claude_code_hooks.handle("SomeFutureEvent")
        assert rc == 0
        payload = json.loads(stdout_buf.getvalue())
        assert payload["continue"] is True

    def test_bad_json_input_allows(self, monkeypatch):
        stdin_buf = io.StringIO("not json")
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        rc = claude_code_hooks.handle("PreToolUse")
        # Bad input must not block.
        assert rc == 0


# ----------------------------------------------------------------------
# MCP dispatch wiring path — pre_call/post_call adapter functions
# ----------------------------------------------------------------------

class TestMCPDispatchWiring:
    def test_pre_call_returns_block_for_py_bak(self, tmp_path, monkeypatch):
        # Make get_project_root resolve to a real dir (the wiring uses it).
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: tmp_path
        )
        register_policy(BackupExtensionGuard())
        # Demo policy keys off target_file derived from tool args.
        verdict = mcp_dispatch.pre_call(
            tool_name="Edit",
            arguments={"file_path": "foo.py.bak", "content": "anything"},
        )
        assert verdict.is_blocking()

    def test_pre_call_allows_normal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: tmp_path
        )
        register_policy(BackupExtensionGuard())
        verdict = mcp_dispatch.pre_call(
            tool_name="Edit",
            arguments={"file_path": "foo.py"},
        )
        assert verdict.is_allowing()

    def test_post_call_safe_with_no_policies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: tmp_path
        )
        verdict = mcp_dispatch.post_call(
            tool_name="get_node",
            arguments={"file_path": "src/foo.py"},
            output={"value": "ok"},
        )
        assert verdict.is_allowing()

    def test_pre_call_swallows_engine_errors(self, tmp_path, monkeypatch):
        # Even if dispatch raises, the wiring layer must return allow —
        # MCP call_tool must NEVER break because of engine bugs.
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: tmp_path
        )
        # Force dispatch to raise:
        monkeypatch.setattr(
            "mcp_server.engine.wiring.mcp_dispatch.dispatch",
            lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        verdict = mcp_dispatch.pre_call("Edit", {"file_path": "x.py"})
        assert verdict.is_allowing()


# ----------------------------------------------------------------------
# Maybe-register helper (env-var-gated demo policy activation)
# ----------------------------------------------------------------------

class TestMaybeRegister:
    def test_off_by_default(self, monkeypatch):
        from mcp_server.engine.runner import registered_policies
        monkeypatch.delenv("CODEVIRA_DEMO_POLICY", raising=False)
        maybe_register()
        assert "demo_backup_guard" not in [p.name for p in registered_policies()]

    def test_on_when_env_var_set(self, monkeypatch):
        from mcp_server.engine.runner import registered_policies
        monkeypatch.setenv("CODEVIRA_DEMO_POLICY", "1")
        maybe_register()
        assert "demo_backup_guard" in [p.name for p in registered_policies()]
