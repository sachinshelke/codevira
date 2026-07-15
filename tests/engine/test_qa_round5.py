"""Round-5 QA: regression tests for schema mismatches between my assumed
Claude Code hook protocol and the actual one.

R5 came from a fresh angle: actually consulting Claude Code's hook
documentation (https://code.claude.com/docs/en/hooks) and comparing
it field-by-field to what the wiring layer emits.

Findings:
  R5 #1 (CRITICAL) — `additionalContext` MUST be nested under
                     `hookSpecificOutput` with `hookEventName`. Top-level
                     placement is silently ignored. Hero 5 (Cross-Session
                     Consistency) and Hero 9 (Proactive Intent) depend on
                     inject working. Without this fix, both heroes ship
                     broken — no error, just no AI behavior change.
  R5 #2 — Block path should also include `hookSpecificOutput.permissionDecision`
          for modern Claude Code (and write reason to stderr per protocol).
  R5 #3 — Warn path used `message` field (doesn't exist in schema). Should
          be `systemMessage`.
  R5 #4 — PostToolUse input uses `tool_result`, not `tool_response`.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mcp_server.engine.events import EventType
from mcp_server.engine import (
    Policy,
    PolicyVerdict,
    register_policy,
    reset_policies,
)
from mcp_server.engine.wiring import claude_code_hooks


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_policies()
    monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)
    # These tests exercise hook MECHANICS (block/inject/warn schema), which run
    # once the v3.7.0 opt-in gate has allowed the project. Enable auto_adopt so
    # the gate is transparent here; the gate itself is covered by
    # tests/test_opt_in.py::TestHookHandleOptInGuard.
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    yield
    reset_policies()


def _run_handler(event_name: str, raw_input: dict, monkeypatch) -> tuple[int, str, str]:
    """Helper: stub stdin/stdout/stderr; return (exit_code, stdout, stderr)."""
    stdin_buf = io.StringIO(json.dumps(raw_input))
    stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin_buf)
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_buf)
    rc = claude_code_hooks.handle(event_name)
    return rc, stdout_buf.getvalue(), stderr_buf.getvalue()


# =====================================================================
# R5 #1 (CRITICAL): inject path uses hookSpecificOutput.additionalContext
# =====================================================================


class TestInjectSchemaConformance:
    """Hero 5 / Hero 9 inject context via PolicyVerdict.inject(...).
    The wiring must emit it under `hookSpecificOutput.additionalContext`
    with required `hookEventName` — top-level is silently ignored by
    Claude Code."""

    def test_inject_payload_under_hookSpecificOutput(self, tmp_path, monkeypatch):
        class Injector(Policy):
            name = "injector"
            handles = (EventType.SESSION_START,)

            def evaluate(self, event):
                return PolicyVerdict.inject("here is recent context")

        register_policy(Injector())
        proj = tmp_path / "proj"
        proj.mkdir()

        rc, stdout, _ = _run_handler(
            "SessionStart",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "source": "startup",
                "model": "claude-sonnet-4-6",
            },
            monkeypatch,
        )
        assert rc == 0
        payload = json.loads(stdout)
        # The critical assertion: additionalContext must be NESTED.
        assert (
            "hookSpecificOutput" in payload
        ), f"inject must use hookSpecificOutput; got payload: {payload}"
        hso = payload["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        assert "here is recent context" in hso["additionalContext"]
        # Must NOT have top-level additionalContext (legacy / wrong placement)
        assert (
            "additionalContext" not in payload
        ), "additionalContext at top level is silently ignored — must be nested"

    def test_inject_for_each_event_type_uses_correct_hookEventName(
        self, tmp_path, monkeypatch
    ):
        class InjectorAll(Policy):
            name = "injector_all"
            handles = (
                EventType.SESSION_START,
                EventType.PRE_TOOL_USE,
                EventType.POST_TOOL_USE,
                EventType.USER_PROMPT_SUBMIT,
                EventType.STOP,
            )

            def evaluate(self, event):
                return PolicyVerdict.inject(f"ctx for {event.event_type.value}")

        register_policy(InjectorAll())
        proj = tmp_path / "proj"
        proj.mkdir()

        cases = [
            ("SessionStart", {"source": "startup", "model": "x"}),
            ("PreToolUse", {"tool_name": "Edit", "tool_input": {}}),
            (
                "PostToolUse",
                {"tool_name": "Edit", "tool_input": {}, "tool_result": "ok"},
            ),
            ("UserPromptSubmit", {"prompt": "do a thing"}),
            ("Stop", {"stop_reason": "end_turn"}),
        ]
        for event_name, extra in cases:
            base = {"session_id": "s1", "cwd": str(proj), **extra}
            rc, stdout, _ = _run_handler(event_name, base, monkeypatch)
            assert rc == 0
            payload = json.loads(stdout)
            assert (
                payload["hookSpecificOutput"]["hookEventName"] == event_name
            ), f"Event {event_name} payload: {payload}"


# =====================================================================
# R5 #2: block path includes hookSpecificOutput.permissionDecision
# =====================================================================


class TestBlockSchemaConformance:
    """PreToolUse and UserPromptSubmit blocks should include the modern
    permissionDecision schema in addition to the legacy stopReason."""

    def test_pretooluse_block_has_permissionDecision_deny(self, tmp_path, monkeypatch):
        class Blocker(Policy):
            name = "blocker"
            handles = (EventType.PRE_TOOL_USE,)

            def evaluate(self, event):
                return PolicyVerdict.block("nope, not allowed")

        register_policy(Blocker())
        proj = tmp_path / "proj"
        proj.mkdir()
        rc, stdout, stderr = _run_handler(
            "PreToolUse",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(proj / "x.py")},
            },
            monkeypatch,
        )
        assert rc == 2
        payload = json.loads(stdout)
        # Legacy fields still present
        assert payload["continue"] is False
        assert "nope, not allowed" in payload["stopReason"]
        # Modern fields also present
        hso = payload.get("hookSpecificOutput", {})
        assert hso.get("hookEventName") == "PreToolUse"
        assert hso.get("permissionDecision") == "deny"
        assert "nope, not allowed" in hso.get("permissionDecisionReason", "")

    def test_block_writes_reason_to_stderr(self, tmp_path, monkeypatch):
        """Per protocol, exit-2 writes are surfaced via stderr to user/Claude."""

        class Blocker(Policy):
            name = "blocker"
            handles = (EventType.PRE_TOOL_USE,)

            def evaluate(self, event):
                return PolicyVerdict.block("specific reason text 12345")

        register_policy(Blocker())
        proj = tmp_path / "proj"
        proj.mkdir()
        rc, stdout, stderr = _run_handler(
            "PreToolUse",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(proj / "x.py")},
            },
            monkeypatch,
        )
        assert rc == 2
        assert "specific reason text 12345" in stderr


# =====================================================================
# R5 #3: warn path uses systemMessage field
# =====================================================================


class TestWarnSchemaConformance:
    """Warn output must use `systemMessage` (camelCase, schema-correct),
    not `message` (which I had before — non-existent field)."""

    def test_warn_uses_systemMessage(self, tmp_path, monkeypatch):
        class Warner(Policy):
            name = "warner"
            handles = (EventType.PRE_TOOL_USE,)

            def evaluate(self, event):
                return PolicyVerdict.warn("careful")

        register_policy(Warner())
        proj = tmp_path / "proj"
        proj.mkdir()
        rc, stdout, _ = _run_handler(
            "PreToolUse",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {},
            },
            monkeypatch,
        )
        assert rc == 0
        payload = json.loads(stdout)
        assert "systemMessage" in payload
        assert "careful" in payload["systemMessage"]
        # And not the wrong field
        assert "message" not in payload


# =====================================================================
# R5 #4: PostToolUse input reads tool_result
# =====================================================================


class TestPostToolUseInputSchema:
    """PostToolUse input uses `tool_result` (current Claude Code schema)."""

    def test_tool_result_field_read(self, tmp_path, monkeypatch):
        captured: dict = {}

        class Inspector(Policy):
            name = "inspector"
            handles = (EventType.POST_TOOL_USE,)

            def evaluate(self, event):
                captured["tool_output"] = event.tool_output
                return PolicyVerdict.allow()

        register_policy(Inspector())
        proj = tmp_path / "proj"
        proj.mkdir()
        _run_handler(
            "PostToolUse",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {},
                "tool_result": {"output": "edited successfully"},
            },
            monkeypatch,
        )
        # The wiring should have read tool_result and stored it on the
        # event. (HookEvent.tool_output gets dict-shaped values.)
        assert captured["tool_output"] is not None
        assert captured["tool_output"].get("output") == "edited successfully"

    def test_legacy_tool_response_still_works(self, tmp_path, monkeypatch):
        """Older Claude Code versions sent `tool_response` — still tolerated."""
        captured: dict = {}

        class Inspector(Policy):
            name = "inspector_legacy"
            handles = (EventType.POST_TOOL_USE,)

            def evaluate(self, event):
                captured["tool_output"] = event.tool_output
                return PolicyVerdict.allow()

        register_policy(Inspector())
        proj = tmp_path / "proj"
        proj.mkdir()
        _run_handler(
            "PostToolUse",
            {
                "session_id": "s1",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {},
                "tool_response": {"output": "legacy field"},
            },
            monkeypatch,
        )
        assert captured["tool_output"] is not None
        assert captured["tool_output"].get("output") == "legacy field"
