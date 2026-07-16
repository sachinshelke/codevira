"""
antigravity_hooks.py — adapter for Google Antigravity's file-based hooks.json.

Antigravity (v1.1.x+) runs hook commands at lifecycle events and — unlike
Claude Code, which signals via exit code — reads the hook's decision from a
JSON object on STDOUT. Contract extracted from the shipped Antigravity
language_server binary (D00011O):

  STDIN  (PreToolUse): {"toolCall": {"name": "...", "args": {...}},
                        "workspacePaths": ["/abs/ws"], "conversationId": ...,
                        "transcriptPath": ..., "modelName": ...}
  STDOUT (PreToolUse): {"decision": "allow"|"deny"|"ask"|"force_ask",
                        "reason": "..."}     # deny = hard block before the tool runs

This adapter reuses the SAME enforcement engine as Claude Code: it translates
Antigravity's payload into the Claude-shaped raw dict and calls
``claude_code_hooks._build_event`` + ``engine.runner.dispatch``, so a
``do_not_revert`` decision is enforced identically in both IDEs. Only the
input parsing and the output formatting differ.

Fail-open by construction: any parse/lookup uncertainty yields ``allow``. We
emit ``deny`` ONLY when the engine positively returns a ``block`` verdict.

Public API:
    handle(event_type: str) -> int
        Reads Antigravity's stdin JSON, runs the engine, writes the
        Antigravity-protocol JSON to stdout, returns an exit code (0 always;
        Antigravity reads the decision from stdout, not the exit code).
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Antigravity's file-mutating tool names (from the language_server binary) mapped
# onto the Claude tool names the shared engine's target_file extraction keys on.
# We match generously (the exact edit-step name is one of the 3 unknowns the
# live-capture pins) and fail-open if a real name isn't in this map.
_ANTIGRAVITY_TOOL_MAP: dict[str, str] = {
    "write_to_file": "Write",
    "create_file": "Write",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "edit_file": "Edit",
    "run_command": "Bash",
}

# Candidate arg keys that carry the edited file's path across Antigravity tool
# versions (exact casing is one of the live-capture unknowns — try them all).
_PATH_ARG_KEYS: tuple[str, ...] = (
    "TargetFile",
    "targetFile",
    "target_file",
    "file_path",
    "filePath",
    "FilePath",
    "path",
    "Path",
    "absolute_path",
    "AbsolutePath",
    "filename",
    "FileName",
)


def handle(event_type: str) -> int:
    """Process one Antigravity hooks.json invocation. Returns an exit code.

    Antigravity reads the decision from the STDOUT JSON, so the exit code is
    informational; we always return 0 to avoid double-signalling a block.
    """
    # Only PreToolUse enforces; other events (PostToolUse/Stop/…) allow for now.
    if event_type != "PreToolUse":
        _write({"decision": "allow"})
        return 0

    raw = _read_stdin()
    if raw is None:
        _write({"decision": "allow"})
        return 0

    try:
        translated, cc_event = _translate(raw)
    except Exception:  # noqa: BLE001 — fail open
        _write({"decision": "allow"})
        return 0

    # Reuse the Claude Code adapter's event builder + the shared engine.
    try:
        from mcp_server.engine.runner import dispatch
        from mcp_server.engine.wiring.claude_code_hooks import _build_event

        event = _build_event(cc_event, translated)
    except Exception:  # noqa: BLE001 — bad payload / invalid root → allow
        _write({"decision": "allow"})
        return 0

    # Opt-in gate: stay fully inert for a project the user never `codevira
    # init`-ed (mirrors the Claude Code hook path). Fail-open.
    try:
        from mcp_server.opt_in import activation_allowed

        if not activation_allowed(event.project_root):
            _write({"decision": "allow"})
            return 0
    except Exception:  # noqa: BLE001
        pass

    try:
        verdict = dispatch(event)
    except Exception:  # noqa: BLE001 — engine bug must never block the user
        _write({"decision": "allow"})
        return 0

    # Memory fan-out (best-effort, after the verdict — never affects it).
    try:
        from mcp_server.engine.memory_fanout import dispatch as _fanout

        _fanout(event)
    except Exception:  # noqa: BLE001
        pass

    _write(_verdict_to_response(verdict))
    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _read_stdin() -> dict[str, Any] | None:
    """Return the parsed stdin JSON object, or None on empty/bad input."""
    try:
        if sys.stdin.isatty():
            return None
        buf = sys.stdin.read()
    except OSError:
        return None
    if not buf.strip():
        return None
    try:
        data = json.loads(buf)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _translate(raw: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """Map Antigravity's PreToolUse payload → the Claude-shaped raw dict.

    Returns (translated_raw, EventType.PRE_TOOL_USE). Raises on a structurally
    unusable payload (caller fails open).
    """
    from mcp_server.engine.events import EventType

    tool_call = raw.get("toolCall") or {}
    if not isinstance(tool_call, dict):
        tool_call = {}
    agy_name = str(tool_call.get("name") or "")
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    # Map to a Claude tool name the shared engine understands; unknown edit
    # tools fall back to the raw name (engine simply won't extract a target
    # file, which is safe — it just can't run file-scoped guards).
    cc_tool_name = _ANTIGRAVITY_TOOL_MAP.get(agy_name, agy_name)

    # Normalize the file-path arg into the keys _build_event looks for.
    tool_input = dict(args)
    if "file_path" not in tool_input:
        for k in _PATH_ARG_KEYS:
            if args.get(k):
                tool_input["file_path"] = args[k]
                break

    # Working directory: Antigravity gives workspacePaths (array); take the
    # first. Falls back to any singular field, else cwd via _build_event.
    ws = raw.get("workspacePaths")
    cwd = None
    if isinstance(ws, list) and ws:
        cwd = ws[0]
    elif isinstance(ws, str):
        cwd = ws
    cwd = cwd or raw.get("workspacePath") or raw.get("cwd")

    translated: dict[str, Any] = {
        "tool_name": cc_tool_name,
        "tool_input": tool_input,
        "_antigravity_tool": agy_name,
    }
    if cwd:
        translated["cwd"] = cwd
    return translated, EventType.PRE_TOOL_USE


def _verdict_to_response(verdict: Any) -> dict[str, Any]:
    """Translate an engine PolicyVerdict into Antigravity's decision JSON.

    ``block`` → ``deny`` (hard-stops the tool before it runs). ``warn`` /
    ``inject`` surface a reason but still allow. ``allow`` → allow.
    """
    action = getattr(verdict, "action", "allow")
    message = getattr(verdict, "message", "") or ""
    policy = getattr(verdict, "policy", "") or ""

    if action == "block":
        reason = message or "Codevira policy blocked this action."
        if policy:
            reason = f"[codevira:{policy}] {reason}"
        return {"decision": "deny", "reason": reason}

    if action in ("warn", "inject"):
        reason = message or getattr(verdict, "inject_context", "") or ""
        if reason and policy:
            reason = f"[codevira] {reason}"
        # Allow, but pass the context along as the reason (shown to the agent).
        resp: dict[str, Any] = {"decision": "allow"}
        if reason:
            resp["reason"] = reason
        return resp

    return {"decision": "allow"}


def _write(payload: dict[str, Any]) -> None:
    try:
        sys.stdout.write(json.dumps(payload))
        sys.stdout.write("\n")
        sys.stdout.flush()
    except OSError:
        pass
