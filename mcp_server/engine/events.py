"""
events.py — the immutable event types the engine dispatches to policies.

Every interception point (Claude Code lifecycle hook, MCP tool dispatch, etc.)
normalizes its input into a HookEvent and hands it to engine.dispatch().
Policies never mutate events; they only read them and return PolicyVerdicts.

Event types (see docs/heroes/00-engine.md "Hook event types"):
  - PRE_TOOL_USE        — fires before any AI tool/Edit/Write/etc. runs
  - POST_TOOL_USE       — fires after tool runs
  - SESSION_START       — new AI session begins
  - USER_PROMPT_SUBMIT  — user sends a prompt to the AI
  - STOP                — AI session ends

The dataclass is frozen so policies can't accidentally mutate it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class EventType(str, Enum):
    """Identifies which lifecycle moment a HookEvent represents."""

    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    STOP = "stop"

    def __str__(self) -> str:  # pragma: no cover — only used in error messages
        return self.value


@dataclass(frozen=True)
class HookEvent:
    """A single interception point, immutable.

    The fields a particular event carries depend on its ``event_type`` —
    a ``PRE_TOOL_USE`` event has ``tool_name`` and ``tool_input``;
    a ``SESSION_START`` event might carry only ``project_root`` and
    ``ai_tool``. Fields that don't apply are left at their default ``None``
    or empty values.

    Policies must never mutate this object (frozen=True enforces it). To
    pass new state forward, return it via the ``metadata`` of a
    PolicyVerdict.

    Attributes:
        event_type: which lifecycle event this is.
        project_root: absolute path of the project the AI is working in.
            Always populated; this is how the engine routes signals.
        ai_tool: identifier of the AI tool that triggered this event
            (``"claude-code"``, ``"cursor"``, ``"windsurf"``, …). Best-effort;
            may be ``"unknown"`` if the wiring layer can't identify it.
        session_id: stable identifier for the current AI session; lets
            policies correlate events across the same conversation.
        tool_name: name of the AI tool being invoked (for PRE/POST_TOOL_USE).
            Examples: ``"Edit"``, ``"Write"``, ``"MultiEdit"``, ``"Bash"``,
            ``"Read"``. Empty for non-tool events.
        tool_input: the raw input the AI passed to the tool. Shape depends
            on the tool. Empty dict for non-tool events.
        tool_output: only populated for POST_TOOL_USE; the result the
            tool produced (or an error indicator).
        target_file: convenience: if the tool touches one specific file,
            this is its absolute path. Computed by the wiring layer.
            Many policies key off this.
        proposed_diff: for Edit/Write events, the text the AI wants to
            apply. Optional — wiring layer fills it when available.
        prompt_text: for USER_PROMPT_SUBMIT events, the user's prompt
            that triggered this turn.
        timestamp: epoch seconds when the event was created. Used for
            performance tracing and chronological ordering.
        raw: escape hatch for policies that need wiring-specific fields
            we haven't promoted to first-class attributes yet. Wiring
            adapters dump their original input here.
    """

    event_type: EventType
    project_root: Path
    ai_tool: str = "unknown"
    session_id: str | None = None

    # Tool-specific
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: dict[str, Any] | None = None
    target_file: Path | None = None
    proposed_diff: str | None = None

    # Prompt-specific
    prompt_text: str | None = None

    # Bookkeeping
    timestamp: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------
    # Convenience accessors (no mutation)
    # ---------------------------------------------------------------

    def is_edit(self) -> bool:
        """True for tool calls that modify code on disk.

        Most "block before edit" policies (Decision Lock, Anti-Regression,
        Blast-Radius, Scope Contract) fan out to the same tool names —
        centralize here so they all agree on the set.
        """
        return self.event_type == EventType.PRE_TOOL_USE and self.tool_name in {
            "Edit",
            "Write",
            "MultiEdit",
            "NotebookEdit",
        }

    def is_read(self) -> bool:
        """True for non-mutating file reads."""
        return self.event_type == EventType.PRE_TOOL_USE and self.tool_name in {
            "Read",
            "Glob",
            "Grep",
        }
