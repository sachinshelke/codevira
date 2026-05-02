"""
token_meter.py — per-session token accounting.

Hero 6 (Token Budget Live View) is the visible consumer; other policies
may peek at current usage. This Week-1 version is the minimum needed for
the engine's signal API:

  - one ``TokenMeter`` per AI session, identified by session_id
  - ``record_injected(tokens, source)`` — wiring layer calls this when
    a tool response goes to the AI
  - ``record_used(tokens, source)`` — Hero 6 will call this in PostToolUse
    when AI references a prior tool's output
  - ``summary()`` — current numbers + breakdown by source
  - ``get_session_meter()`` — accessor used by SignalContext

Week 2 expands with: persistence to ``<data_dir>/logs/token_budget.jsonl``,
historical accounting, optimization hints. For now we keep state in-memory
so the wiring layer can be tested first.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenMeter:
    """In-memory token accounting for one AI session.

    Fields:
        session_id: stable identifier for this session.
        injected_total: sum of tokens added to AI context across all tool
            responses.
        used_total: sum of tokens the AI demonstrably referenced.
        injected_by_source: per-tool/source breakdown of injection.
        used_by_source: per-tool/source breakdown of usage.

    Usage is thread-safe — wiring layer may call from any thread.
    """

    session_id: str
    injected_total: int = 0
    used_total: int = 0
    injected_by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    used_by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_injected(self, tokens: int, source: str = "unknown") -> None:
        """Record tokens added to AI context. Thread-safe."""
        if tokens <= 0:
            return
        with self._lock:
            self.injected_total += tokens
            self.injected_by_source[source] += tokens

    def record_used(self, tokens: int, source: str = "unknown") -> None:
        """Record tokens the AI referenced. Thread-safe.

        ``source`` should match an earlier ``record_injected`` source
        when possible — that lets ``summary()`` compute per-source
        utilization (e.g. "get_node injected 200 tokens, AI used 150 → 75%").
        """
        if tokens <= 0:
            return
        with self._lock:
            self.used_total += tokens
            self.used_by_source[source] += tokens

    def summary(self) -> dict[str, Any]:
        """Return a snapshot of current accounting."""
        with self._lock:
            efficiency = (self.used_total / self.injected_total) if self.injected_total else 0.0
            # Top wasted sources = where injected was high relative to used.
            wasted = []
            for src, injected in self.injected_by_source.items():
                used = self.used_by_source.get(src, 0)
                wasted_amt = injected - used
                if wasted_amt > 0:
                    wasted.append({"source": src, "wasted": wasted_amt, "injected": injected})
            wasted.sort(key=lambda x: x["wasted"], reverse=True)
            return {
                "session_id": self.session_id,
                "injected_total": self.injected_total,
                "used_total": self.used_total,
                "efficiency": round(efficiency, 3),
                "top_wasted_sources": wasted[:5],
            }


# ----------------------------------------------------------------------
# Process-wide session meters keyed by session_id.
#
# Wiring layer manages the lifecycle: create on session_start, drop on
# stop. SignalContext.token_budget reads the "current" meter via
# ``get_session_meter()`` — which uses the most-recently-active session.
#
# Week 1 keeps this trivial. Week 2 adds persistence + cleanup of stale
# sessions.
# ----------------------------------------------------------------------

_meters: dict[str, TokenMeter] = {}
_current_session_id: str | None = None
_meters_lock = threading.Lock()


def get_or_create_session_meter(session_id: str) -> TokenMeter:
    """Return the meter for ``session_id``, creating one if needed.

    Sets the meter as the "current" session for ``get_session_meter``.
    """
    global _current_session_id
    with _meters_lock:
        meter = _meters.get(session_id)
        if meter is None:
            meter = TokenMeter(session_id=session_id)
            _meters[session_id] = meter
        _current_session_id = session_id
        return meter


def get_session_meter() -> TokenMeter | None:
    """Return the current session's meter, or ``None`` if no session active.

    Used by SignalContext.token_budget. Policies that need to read but not
    create state read this.
    """
    with _meters_lock:
        if _current_session_id is None:
            return None
        return _meters.get(_current_session_id)


def end_session(session_id: str) -> dict[str, Any] | None:
    """Drop the meter for a session. Returns its final summary.

    Wiring layer calls this on the ``stop`` hook event.
    """
    global _current_session_id
    with _meters_lock:
        meter = _meters.pop(session_id, None)
        if _current_session_id == session_id:
            _current_session_id = None
        if meter is None:
            return None
        return meter.summary()


def reset_meters() -> None:
    """Tests only — clear all session state."""
    global _current_session_id
    with _meters_lock:
        _meters.clear()
        _current_session_id = None
