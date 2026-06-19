"""Read-side relevance eval — E3 (Phase 21).

Measures codevira's actual leverage (D00005N): does the read surface
surface the RIGHT memory with low noise? Self-maintaining (cases derived
from real ``.codevira/`` memory) and intelligent (LLM-as-judge for
relevance, offline + opt-in). Non-gating — a quality signal, not a gate.
"""

from __future__ import annotations

from mcp_server.eval.report import append_trend, format_report, run_eval

__all__ = ["run_eval", "format_report", "append_trend"]
