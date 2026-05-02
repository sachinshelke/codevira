"""
mcp_dispatch.py — adapter for the existing MCP server's call_tool handler.

Codevira already routes AI tool calls through ``mcp_server.server:call_tool``.
The engine wants to evaluate policies on EVERY such call. This adapter
exposes two thin functions for ``call_tool`` to wrap around its dispatch:

    pre_call(tool_name, arguments) -> PolicyVerdict
        Build a PRE_TOOL_USE HookEvent, run engine.dispatch, return the
        combined verdict. ``call_tool`` checks ``verdict.action == "block"``
        and short-circuits with the block message; otherwise proceeds.

    post_call(tool_name, arguments, output) -> None
        Build a POST_TOOL_USE HookEvent, run engine.dispatch (verdict is
        usually allow but this is where Hero 6 logs token usage and Hero 7
        runs style checks).

These functions are deliberately safe-by-default: any error inside means
"allow / no-op." The call_tool path must NEVER break because of engine
trouble.

This adapter handles MCP tool calls. For Claude Code lifecycle hooks,
see ``claude_code_hooks.py`` instead.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies import PolicyVerdict
from mcp_server.engine.runner import dispatch


def pre_call(tool_name: str, arguments: dict[str, Any]) -> PolicyVerdict:
    """Run the engine's PRE_TOOL_USE hook for an MCP tool call.

    Returns the combined verdict. Caller (call_tool) is responsible for
    interpreting:
      - ``action == "block"``: short-circuit the tool dispatch and return
        the verdict.message to the AI as the tool result.
      - ``action == "warn"``: include the warn message alongside normal
        tool output.
      - ``action == "inject"``: include verdict.inject_context with the
        normal tool output.
      - ``action == "allow"``: dispatch normally.

    Engine bugs return ``allow`` with metadata explaining why.
    """
    try:
        event = _build_pre_event(tool_name, arguments)
    except Exception:  # noqa: BLE001
        return PolicyVerdict.allow(metadata={"_wiring_error": "build_event_failed"})

    try:
        return dispatch(event)
    except Exception:  # noqa: BLE001
        return PolicyVerdict.allow(metadata={"_wiring_error": "dispatch_failed"})


def post_call(
    tool_name: str,
    arguments: dict[str, Any],
    output: Any = None,
) -> PolicyVerdict:
    """Run the engine's POST_TOOL_USE hook for an MCP tool call.

    Most post-call verdicts are ``allow`` — this hook fires for telemetry
    (token meter, style check, AI-promotion-score updates). The verdict
    is returned for callers that want to surface ``warn`` messages, but
    can be ignored.
    """
    try:
        event = _build_post_event(tool_name, arguments, output)
    except Exception:  # noqa: BLE001
        return PolicyVerdict.allow(metadata={"_wiring_error": "build_event_failed"})

    try:
        return dispatch(event)
    except Exception:  # noqa: BLE001
        return PolicyVerdict.allow(metadata={"_wiring_error": "dispatch_failed"})


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _build_pre_event(tool_name: str, arguments: dict[str, Any]) -> HookEvent:
    """Construct a PRE_TOOL_USE HookEvent from MCP-style call_tool args."""
    from mcp_server.paths import get_project_root

    project_root = get_project_root()

    target_file: Path | None = None
    # Codevira tools that touch a specific file expose ``file_path`` in
    # their arguments — e.g. get_node, get_impact, update_node, get_code,
    # get_signature. Use that for target_file inference.
    candidate = arguments.get("file_path") or arguments.get("path")
    if isinstance(candidate, str) and candidate:
        try:
            target_file = (project_root / candidate).resolve()
        except OSError:
            target_file = None

    return HookEvent(
        event_type=EventType.PRE_TOOL_USE,
        project_root=project_root,
        ai_tool="mcp",  # caller doesn't always know — could be claude/cursor/etc.
        session_id=None,
        tool_name=tool_name,
        tool_input=dict(arguments),
        target_file=target_file,
        timestamp=time.time(),
        raw={"source": "mcp_dispatch"},
    )


def _build_post_event(
    tool_name: str,
    arguments: dict[str, Any],
    output: Any,
) -> HookEvent:
    """Construct a POST_TOOL_USE HookEvent."""
    from mcp_server.paths import get_project_root

    project_root = get_project_root()

    # Output may be anything — list[TextContent], dict, str. Coerce to a
    # dict shape for policies (we don't want them to deal with N variants).
    output_dict: dict[str, Any]
    if isinstance(output, dict):
        output_dict = output
    elif output is None:
        output_dict = {"value": None}
    else:
        # For str / list / other — wrap in a value field.
        output_dict = {"value": output}

    return HookEvent(
        event_type=EventType.POST_TOOL_USE,
        project_root=project_root,
        ai_tool="mcp",
        session_id=None,
        tool_name=tool_name,
        tool_input=dict(arguments),
        tool_output=output_dict,
        timestamp=time.time(),
        raw={"source": "mcp_dispatch"},
    )
