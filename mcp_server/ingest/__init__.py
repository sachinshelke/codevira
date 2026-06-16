"""Read-only session-transcript ingest — E2 (Phase 20).

Scans local AI-IDE session logs (Claude Code / Codex / Gemini), reconstructs
the tool-call stream with cheap heuristics (NO LLM), and produces sanitized,
token-bounded :class:`~mcp_server.ingest.models.SessionDigest` signals that
feed the EXISTING reflect/induce pipeline.

Hard invariants:

* **Read-only.** Nothing here writes to a session log or the project.
* **Candidates only.** The digest feeds ``reflect``/``induce`` which produce
  *candidates* a human confirms before anything is committed — this path
  NEVER auto-creates a decision/skill (preserves deliberate-capture).
* **Sanitized.** Every retained excerpt is scrubbed of secrets and capped.
* **Defensive.** A parser that can't read a format returns ``None``; one
  IDE's broken log never breaks the scan.
"""

from __future__ import annotations

from mcp_server.ingest.models import CorrectionTurn, SessionDigest, ToolEvent
from mcp_server.ingest.scan import scan_sessions, to_reflection_signals

__all__ = [
    "scan_sessions",
    "to_reflection_signals",
    "SessionDigest",
    "ToolEvent",
    "CorrectionTurn",
]
