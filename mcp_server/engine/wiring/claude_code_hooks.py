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
from mcp_server.engine.policy import PolicyVerdict
from mcp_server.engine.runner import dispatch
from mcp_server.engine.wiring._diff_envelope import synthesize_proposed_diff


# Map Claude Code's hook event names to our EventType enum.
# Claude Code's docs list 12 events; we handle the 5 we care about.
_CC_EVENT_MAP: dict[str, EventType] = {
    "PreToolUse": EventType.PRE_TOOL_USE,
    "PostToolUse": EventType.POST_TOOL_USE,
    "SessionStart": EventType.SESSION_START,
    "UserPromptSubmit": EventType.USER_PROMPT_SUBMIT,
    "Stop": EventType.STOP,
}

# Reverse map: our EventType → Claude Code's CamelCase name. Claude Code
# requires `hookSpecificOutput.hookEventName` to match the event being
# handled. (R5 QA finding: this is required, not optional.)
_CC_EVENT_NAME: dict[EventType, str] = {v: k for k, v in _CC_EVENT_MAP.items()}


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

    # 3.5. v3.1.0 M2 Phase 3: memory fan-out. Sequenced AFTER policy eval so
    # the verdict isn't affected by fan-out behavior. Fail-open.
    try:
        from mcp_server.engine.memory_fanout import dispatch as _fanout_dispatch

        _fanout_dispatch(event)
    except Exception:
        pass

    # 4 + 5. Translate verdict to Claude Code response.
    return _emit(verdict, event)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _build_event(cc_event: EventType, raw: dict[str, Any]) -> HookEvent:
    """Translate Claude Code's raw hook input into a HookEvent.

    Security note (Round-4 QA HIGH #1, #2):
      - ``project_root`` (cwd) is validated via ``is_invalid_project_root``;
        AI-controlled cwd pointing at $HOME / system dirs is rejected
        before any signal access happens. ValueError → caller's outer
        handler returns ``allow`` (fail-open).
      - ``target_file`` is path-traversal-defended: resolved paths that
        escape ``project_root`` are dropped (target_file = None) so
        policies can't end up reading/writing outside the project.

    Claude Code's hook input schema (verified in R5 QA against
    https://code.claude.com/docs/en/hooks):

      Common fields:
        - session_id, transcript_path, cwd, permission_mode,
          hook_event_name, agent_id (optional), agent_type (optional)

      Event-specific:
        - PreToolUse:        tool_name, tool_input, tool_use_id
        - PostToolUse:       tool_name, tool_input, tool_use_id,
                             tool_result, tool_result_type
        - SessionStart:      source, model
        - UserPromptSubmit:  prompt
        - Stop:              stop_reason

    The working-directory field is exactly ``cwd`` (not
    ``working_directory``). PostToolUse uses ``tool_result`` (not
    ``tool_response``) — older Claude Code versions used the latter,
    so we accept both.
    """
    # cwd is the project the AI is operating on. Claude Code always sends it.
    cwd_str = raw.get("cwd") or raw.get("workspace_dir") or str(Path.cwd())
    project_root = Path(cwd_str).resolve()

    # Round-4 HIGH #2: refuse $HOME / system dirs as project_root EVEN if
    # Claude Code sends them. v1.8.1's is_invalid_project_root guard is
    # the canonical check; the engine reuses it. ValueError propagates
    # to the caller, which fails open via _write_response({"continue": True}).
    from mcp_server.paths import is_invalid_project_root

    rejection = is_invalid_project_root(project_root)
    if rejection:
        # Don't treat this as "block" — that would be aggressive and the
        # user can't fix it from inside Claude Code. Raise ValueError so
        # the outer handler returns allow + logs.
        raise ValueError(
            f"engine: refusing event from invalid project_root: {rejection}"
        )

    tool_name = raw.get("tool_name", "") or ""
    tool_input = raw.get("tool_input", {}) or {}
    # Modern Claude Code uses `tool_result`; older `tool_response`
    # tolerated for backward compat. R5 QA finding: docs say tool_result.
    tool_output = (
        raw.get("tool_result") or raw.get("tool_response") or raw.get("tool_output")
    )

    # Extract a target_file from tool_input when the tool name suggests one.
    target_file: Path | None = None
    if tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit", "Read"}:
        candidate = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
        )
        if candidate:
            try:
                resolved = Path(candidate).resolve()
                # Round-4 HIGH #1: path-traversal containment. Reject
                # candidates that resolve outside project_root. Use
                # commonpath for robust comparison (str-prefix check
                # would false-match e.g. /tmp/proj vs /tmp/proj-other).
                try:
                    import os

                    common = Path(
                        os.path.commonpath([str(project_root), str(resolved)])
                    )
                    if common == project_root:
                        target_file = resolved
                    # else: target_file stays None (path traversal rejected)
                except ValueError:
                    # commonpath raises if paths are on different drives
                    # (Windows) or otherwise incomparable. Reject silently.
                    target_file = None
            except OSError:
                target_file = None

    # Synthesize the ``--- before / --- after`` envelope all the
    # additive-edit guards key on. Delegated to the shared
    # ``_diff_envelope`` helper so this entry point and ``mcp_dispatch``
    # produce an identical, parseable shape.
    #
    # Phase 9 false-positive fix: ``Write`` previously passed raw file
    # content (no envelope), so ``parse_diff`` returned ``(None, None)``,
    # the additive guards in decision_lock / blast_radius / anti_regression
    # were silently bypassed, and a purely-additive full-file Write to a
    # locked or high-fan-in file was hard-blocked as if destructive. The
    # helper now reads the current on-disk content as the ``before`` block
    # so ``Write`` carries an honest diff. (Bug 7 / Week-11: MultiEdit +
    # NotebookEdit support also lives in the helper now.)
    proposed_diff = synthesize_proposed_diff(tool_name, tool_input, target_file)

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
    """Translate verdict to Claude Code's hook protocol on stdout.

    R5 QA: aligned with Claude Code's actual schema. Three fixes since
    Round 1:
      - ``additionalContext`` MUST live under ``hookSpecificOutput`` with
        a required ``hookEventName`` field — top-level placement is
        silently ignored. (R5 inject-path fix.)
      - PreToolUse blocks should also include ``hookSpecificOutput.
        permissionDecision`` and write the reason to stderr per the
        modern protocol. (R5 block-path improvement.)
      - Warn output uses ``systemMessage`` (camelCase), not the
        non-existent ``message`` field. (R5 warn-path fix.)

    Returns exit code: 0 for allow/warn/inject, 2 for block.
    """
    cc_event_name = _CC_EVENT_NAME.get(event.event_type, "")

    if verdict.action == "block":
        # Claude Code blocks the tool. Two complementary mechanisms:
        # 1. Exit code 2 — universally honored across all Claude Code
        #    versions; legacy behavior.
        # 2. hookSpecificOutput.permissionDecision="deny" — modern
        #    protocol; surfaces the reason to the AI cleanly.
        msg = verdict.message or "Codevira policy blocked this action."
        if verdict.policy:
            msg = f"[codevira:{verdict.policy}] {msg}"
        payload: dict[str, Any] = {
            "continue": False,
            "stopReason": msg,
        }
        # Modern hookSpecificOutput for PreToolUse/UserPromptSubmit
        # event types that support permission semantics.
        if cc_event_name in {"PreToolUse", "UserPromptSubmit"}:
            payload["hookSpecificOutput"] = {
                "hookEventName": cc_event_name,
                "permissionDecision": "deny",
                "permissionDecisionReason": msg,
            }
        _write_response(payload)
        # Per the protocol, stderr is shown to the user/Claude on exit 2.
        # Mirror the message there so users see WHY in the Claude Code UI.
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except OSError:
            pass
        return 2

    if verdict.action == "inject":
        # R5 critical fix: inject context MUST be under hookSpecificOutput
        # with hookEventName, not at top level. Top-level was silently
        # ignored by Claude Code.
        ctx = verdict.inject_context or ""
        if verdict.policy:
            ctx = f"[codevira:{verdict.policy}]\n{ctx}"
        payload = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": cc_event_name,
                "additionalContext": ctx,
            },
        }
        _write_response(payload)
        return 0

    if verdict.action == "warn":
        # Warn = continue but surface a non-blocking message via
        # systemMessage (the schema-correct field name).
        msg = verdict.message or ""
        if verdict.policy:
            msg = f"[codevira] {msg}"
        _write_response({"continue": True, "systemMessage": msg})
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
