"""
claude_code_hooks.py — adapter for Claude Code lifecycle hook scripts.

Claude Code invokes hook scripts (one per event type) and pipes JSON to
their stdin describing the event. The script is expected to:

  - Print JSON to stdout: ``{"continue": true|false, "stopReason": "...", ...}``
  - Exit 0 to allow, non-zero to block (for PreToolUse).

This adapter lets a generic hook script be just::

    #!/usr/bin/env bash
    exec codevira engine handle <event-type>

…and the heavy lifting (reading stdin, building HookEvent, calling
engine.dispatch, formatting the response) all happens inside this Python
module.

Public API:

    handle(event_type: str) -> int
        Reads JSON from sys.stdin, runs the engine, writes Claude-Code-
        protocol response to sys.stdout, returns the suggested exit code.

The exit code maps to Claude Code's hook semantics:
    0   → allow / continue
    2   → blocked (Claude Code shows the message and prevents the tool)

Other exit codes are treated as errors by Claude Code; we never use them.

Reference: https://code.claude.com/docs/en/hooks
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies import PolicyVerdict
from mcp_server.engine.runner import dispatch


# Map Claude Code's hook event names to our EventType enum.
# Claude Code's docs list 12 events; we handle the 5 we care about.
_CC_EVENT_MAP: dict[str, EventType] = {
    "PreToolUse": EventType.PRE_TOOL_USE,
    "PostToolUse": EventType.POST_TOOL_USE,
    "SessionStart": EventType.SESSION_START,
    "UserPromptSubmit": EventType.USER_PROMPT_SUBMIT,
    "Stop": EventType.STOP,
}


def handle(event_type_str: str) -> int:
    """Process a Claude Code hook invocation. Returns the suggested exit code.

    This is the SOLE entry point Claude Code's hook scripts call. It:

      1. Reads JSON from stdin (Claude Code's hook input).
      2. Maps it into a HookEvent.
      3. Calls engine.dispatch.
      4. Writes the protocol-correct JSON response to stdout.
      5. Returns 0 (allow) or 2 (block).

    Errors are caught and converted to ``allow`` — we NEVER block the
    user's workflow because of an engine bug.
    """
    cc_event = _CC_EVENT_MAP.get(event_type_str)
    if cc_event is None:
        # Unknown event name — Claude Code may have added one we don't
        # handle. Allow silently.
        _write_response({"continue": True})
        return 0

    # 1. Read stdin (Claude Code pipes JSON; if stdin is empty we still
    #    proceed so tests/dry-runs work).
    raw_input: dict[str, Any] = {}
    try:
        if not sys.stdin.isatty():
            buf = sys.stdin.read()
            if buf.strip():
                raw_input = json.loads(buf)
    except (json.JSONDecodeError, OSError):
        # Bad input — allow and move on. We do NOT log to crash_logger
        # here because the hook may fire before crash_logger is set up.
        _write_response({"continue": True})
        return 0

    # 2. Build HookEvent from the raw payload.
    try:
        event = _build_event(cc_event, raw_input)
    except Exception:  # noqa: BLE001 — fail open
        _write_response({"continue": True})
        return 0

    # 3. Dispatch — this never raises; it always returns a verdict.
    verdict = dispatch(event)

    # 4 + 5. Translate verdict to Claude Code response.
    return _emit(verdict, event)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _build_event(cc_event: EventType, raw: dict[str, Any]) -> HookEvent:
    """Translate Claude Code's raw hook input into a HookEvent.

    Claude Code's hook input schema (from
    https://code.claude.com/docs/en/hooks ) typically includes:

      - cwd: project working directory
      - tool_name (PreToolUse / PostToolUse)
      - tool_input (PreToolUse / PostToolUse)
      - tool_response (PostToolUse)
      - prompt (UserPromptSubmit)
      - session_id

    Field shapes vary slightly by hook event; this helper picks the
    relevant ones and lets the rest fall into ``raw`` for policies that
    care.
    """
    # cwd is the project the AI is operating on. Claude Code always sends it.
    cwd_str = raw.get("cwd") or raw.get("workspace_dir") or str(Path.cwd())
    project_root = Path(cwd_str).resolve()

    tool_name = raw.get("tool_name", "") or ""
    tool_input = raw.get("tool_input", {}) or {}
    tool_output = raw.get("tool_response") or raw.get("tool_output")

    # Extract a target_file from tool_input when the tool name suggests one.
    target_file: Path | None = None
    if tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit", "Read"}:
        candidate = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        if candidate:
            try:
                target_file = Path(candidate).resolve()
            except OSError:
                target_file = None

    # Best-effort proposed_diff for Write/Edit. Edit gives old_string/new_string;
    # Write gives content. We don't synthesize unified diffs here — policies
    # that need them can do so. We just pass enough text for heuristic checks.
    proposed_diff: str | None = None
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old or new:
            proposed_diff = f"--- before\n{old}\n--- after\n{new}\n"
    elif tool_name == "Write":
        content = tool_input.get("content")
        if isinstance(content, str):
            proposed_diff = content

    prompt_text: str | None = None
    if cc_event == EventType.USER_PROMPT_SUBMIT:
        prompt_text = raw.get("prompt") or raw.get("user_prompt")

    return HookEvent(
        event_type=cc_event,
        project_root=project_root,
        ai_tool="claude-code",
        session_id=raw.get("session_id"),
        tool_name=tool_name,
        tool_input=tool_input if isinstance(tool_input, dict) else {},
        tool_output=tool_output if isinstance(tool_output, dict) else None,
        target_file=target_file,
        proposed_diff=proposed_diff,
        prompt_text=prompt_text,
        timestamp=time.time(),
        raw=raw,
    )


def _emit(verdict: PolicyVerdict, event: HookEvent) -> int:
    """Translate verdict to Claude Code's hook protocol on stdout. Returns exit code."""
    if verdict.action == "block":
        # Claude Code blocks the tool and shows ``stopReason`` to the user.
        msg = verdict.message or "Codevira policy blocked this action."
        if verdict.policy:
            msg = f"[codevira:{verdict.policy}] {msg}"
        _write_response({"continue": False, "stopReason": msg})
        return 2

    if verdict.action == "inject":
        # Claude Code includes the hook's stdout (when continue=True) as
        # additional context for the next AI turn — so we write the
        # injected context as the response payload.
        ctx = verdict.inject_context or ""
        if verdict.policy:
            ctx = f"[codevira:{verdict.policy}]\n{ctx}"
        _write_response({"continue": True, "additionalContext": ctx})
        return 0

    if verdict.action == "warn":
        # Warn = continue but show a non-blocking message in the user's
        # session log.
        msg = verdict.message or ""
        if verdict.policy:
            msg = f"[codevira] {msg}"
        _write_response({"continue": True, "message": msg})
        return 0

    # Allow — silent success.
    _write_response({"continue": True})
    return 0


def _write_response(payload: dict[str, Any]) -> None:
    """Emit JSON on stdout. Errors are swallowed so a misbehaving stdout
    stream doesn't escalate into a workflow break."""
    try:
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
    except OSError:
        pass
