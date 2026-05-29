"""
memory_fanout.py — v3.1.0 M2 Phase 3: PostToolUse → working memory.

Auto-populates working memory from MCP tool calls so the agent gets a
free scratchpad without having to remember to call ``working_add()`` on
every Edit / Write / Bash.

# Why this lives next to the engine, not inside it

The engine evaluates policies (allow / warn / inject / block) on every
tool call. Memory fan-out is a pure *side-effect* step: it records
observations from successful tool calls but does not — and must not —
change the policy verdict. Bundling it into a policy would couple
those two concerns and make the verdict pipeline harder to reason
about. Instead, fan-out is a separate module that ``post_call`` in
``mcp_dispatch.py`` calls AFTER the engine dispatch completes. Same
event payload, different responsibility.

# In-process buffering (R3 mitigation)

Each ``dispatch()`` call appends to an in-process list. When the list
reaches ``_FLUSH_THRESHOLD`` events (default 20) — or on interpreter
shutdown via ``atexit`` — the buffer drains to ``working.jsonl`` in
one pass. This is the R3 risk mitigation from the v3.1.0 plan: a
20-file refactor produces ~40 PostToolUse events, and per-write
fsync would visibly slow each tool call. Buffering pushes the latency
to one batched flush.

If the MCP server is killed hard (SIGKILL), the unflushed buffer is
lost. Acceptable for the working-memory use case (observations are
of edits already on disk; the agent can re-derive them if needed).

# Triggers

  - ``Edit`` / ``Write`` / ``MultiEdit`` / ``NotebookEdit`` /
    ``update_node`` → observation ``"<tool>: touched <file_path>"``,
    importance 4.
  - ``Bash`` (non-trivial) → observation ``"Bash: <cmd[:80]>"``,
    importance 3. Trivial commands (``ls``, ``pwd``, ``cd``, ``echo``,
    ``cat``) are skipped to avoid noise.
  - Any tool whose output dict carries ``error`` → bump importance to 7
    (errors are high-salience signals worth surfacing in
    ``get_working_context``).
  - All other tools (read-only introspection, graph queries) → no
    observation. Read tools don't change state, so observing them
    floods the buffer without adding signal.

# Fail-open contract

Every step is wrapped in ``try / except``. A bug in fan-out must
never break the caller's tool dispatch. The verdict from the engine
is already committed by the time this runs; we only get to choose
whether or not to write an observation.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any

from mcp_server.engine.events import EventType, HookEvent

logger = logging.getLogger(__name__)


# In-process buffer + flush threshold. Module-level by design — the
# engine has no per-request state, and a per-process buffer is the
# right unit (one MCP server process serves many tool calls).
_BUFFER: list[dict[str, Any]] = []
_FLUSH_THRESHOLD = 20


# Tools whose calls produce a meaningful "touched <file_path>" observation.
_FILE_EDITING_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "update_node"}
)

# Bash first-words we deliberately skip. The agent runs these all day
# for navigation; observing each would flood working memory.
_TRIVIAL_BASH = frozenset({"ls", "pwd", "cd", "echo", "cat", "which", "type"})


# ──────────────────────────────────────────────────────────────────────
# Public dispatch
# ──────────────────────────────────────────────────────────────────────


def dispatch(event: HookEvent) -> None:
    """Side-effect: record a working-memory observation from an MCP tool call.

    Triggered only on ``POST_TOOL_USE`` events. Buffers the record;
    flushes once the buffer reaches ``_FLUSH_THRESHOLD``. Caller-side
    failure must never affect the engine verdict — every step here is
    fail-open.
    """
    if event.event_type != EventType.POST_TOOL_USE:
        return

    try:
        record = _build_observation(event)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("memory_fanout.dispatch: _build_observation failed: %s", exc)
        return
    if record is None:
        return

    _BUFFER.append(record)
    if len(_BUFFER) >= _FLUSH_THRESHOLD:
        flush()


def flush() -> None:
    """Drain the buffer into working.jsonl + activity.jsonl.

    Each buffered entry becomes one ``working_store.add()`` call,
    plus — for file-edit observations carrying ``_activity_file_path``
    metadata — one ``activity_store.add(kind="edit")`` row. Failures
    inside individual writes are logged and skipped so a single
    malformed entry can't poison the rest of the batch.
    """
    global _BUFFER
    if not _BUFFER:
        return

    # Take ownership of the buffer atomically so a re-entrant call
    # (e.g., from a hook during another flush) doesn't double-write.
    drained = _BUFFER
    _BUFFER = []

    try:
        from mcp_server.storage import working_store
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_fanout.flush: working_store import failed: %s", exc)
        return

    # activity_store import is best-effort — older installs without M4
    # still get the working observation written.
    try:
        from mcp_server.storage import activity_store
    except Exception:  # noqa: BLE001
        activity_store = None  # type: ignore[assignment]

    for rec in drained:
        try:
            working_store.add(
                content=rec["content"],
                kind=rec.get("kind", "observation"),
                importance=rec.get("importance", 4),
                links=rec.get("links") or [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory_fanout.flush: working add failed: %s", exc)

        # v3.1.0 M4: if the originating tool was a file edit, mirror the
        # observation as an activity row so spatial_heat / spatial_nearby
        # have a heat signal. _activity_file_path is set by
        # _build_observation below; never present on Bash records.
        if activity_store is not None and rec.get("_activity_file_path"):
            try:
                activity_store.add(
                    rec["_activity_file_path"],
                    kind=activity_store.KIND_EDIT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("memory_fanout.flush: activity add failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────
# Test/admin helpers
# ──────────────────────────────────────────────────────────────────────


def buffer_size() -> int:
    """Return the current buffer length. Useful for tests + telemetry."""
    return len(_BUFFER)


def reset_buffer() -> None:
    """Discard buffered entries without writing. TEST-ONLY: never use
    this in production code — it loses observations."""
    global _BUFFER
    _BUFFER = []


# ──────────────────────────────────────────────────────────────────────
# Observation builders
# ──────────────────────────────────────────────────────────────────────


def _build_observation(event: HookEvent) -> dict[str, Any] | None:
    """Translate a POST_TOOL_USE event into a working-memory record.

    Per-tool importance floor:
      - File edits (Edit/Write/MultiEdit/NotebookEdit/update_node): 4
      - Bash (non-trivial): 3  (lower so commands don't outrank edits)
    Errors bump the importance to 7 regardless of tool.

    Returns ``None`` if the tool isn't worth observing.
    """
    tool_name = event.tool_name or ""
    args = event.tool_input or {}
    output = event.tool_output or {}

    has_error = isinstance(output, dict) and bool(output.get("error"))

    if tool_name in _FILE_EDITING_TOOLS:
        file_path = args.get("file_path") or args.get("path") or "<unknown>"
        return {
            "content": f"{tool_name}: touched {file_path}",
            "kind": "observation",
            "importance": 7 if has_error else 4,
            # v3.1.0 M4: mirror this edit into activity.jsonl too. The
            # flusher detects this hidden field and writes an activity
            # row alongside the working observation; non-file tools
            # (e.g., Bash) omit it.
            "_activity_file_path": (
                str(file_path) if file_path and file_path != "<unknown>" else None
            ),
        }

    if tool_name == "Bash":
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return None
        first = cmd.split(None, 1)[0]
        if first in _TRIVIAL_BASH:
            return None
        summary = cmd if len(cmd) <= 80 else cmd[:77] + "..."
        return {
            "content": f"Bash: {summary}",
            "kind": "observation",
            "importance": 7 if has_error else 3,
        }

    # Read-only / introspection tools — no observation. We want
    # working memory dense with "did this" signal, not "looked at" noise.
    return None


# Ensure a clean interpreter shutdown still flushes buffered events.
atexit.register(flush)
