"""Normalized session-transcript models — E2 (Phase 20).

Every per-tool parser (Claude Code / Codex / Gemini) reduces its native
log format to these tool-agnostic shapes. The reflect/induce pipeline only
ever sees the normalized form, so adding a new IDE means writing one parser
that emits a :class:`SessionDigest` — nothing downstream changes.

A digest is a SIGNAL summary, not a transcript replay: it carries the count
of tool calls, the handful of FAILED calls, and the handful of user
CORRECTIONS — the moments worth reflecting on. Raw prompt/code text is never
retained; only short, sanitized excerpts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolEvent:
    """One tool call that FAILED (successful calls are only counted, not kept)."""

    tool: str  # tool name, e.g. "Edit", "Bash", "mcp__codevira__record_decision"
    error_excerpt: str = ""  # short, sanitized error text
    seq: int = 0  # position in the session's tool-call stream

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "error_excerpt": self.error_excerpt, "seq": self.seq}


@dataclass(frozen=True)
class CorrectionTurn:
    """A user turn heuristically flagged as correcting the assistant."""

    excerpt: str = ""  # short, sanitized user text
    after_tool: str = ""  # the tool the user appears to be correcting, if known
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "excerpt": self.excerpt,
            "after_tool": self.after_tool,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class SessionDigest:
    """Tool-agnostic summary of one AI-IDE session worth reflecting on."""

    source: str  # "claude_code" | "codex" | "gemini"
    session_id: str
    path: str  # absolute path of the source log (provenance, never its content)
    started_at: str | None  # ISO timestamp of the first event, if known
    n_tool_calls: int
    n_failures: int
    n_corrections: int
    failures: tuple[ToolEvent, ...]  # capped sample
    corrections: tuple[CorrectionTurn, ...]  # capped sample

    @property
    def is_interesting(self) -> bool:
        """True if there is anything worth reflecting on (a failure or a
        correction). Clean sessions are dropped before they reach the LLM."""
        return bool(self.failures or self.corrections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "session_id": self.session_id,
            "path": self.path,
            "started_at": self.started_at,
            "n_tool_calls": self.n_tool_calls,
            "n_failures": self.n_failures,
            "n_corrections": self.n_corrections,
            "failures": [f.to_dict() for f in self.failures],
            "corrections": [c.to_dict() for c in self.corrections],
        }
