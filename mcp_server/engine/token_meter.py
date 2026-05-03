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


def end_session(session_id: str, *, project_root: "Path | None" = None) -> dict[str, Any] | None:
    """Drop the meter for a session. Returns its final summary.

    Wiring layer calls this on the ``stop`` hook event. The summary is
    also persisted to ``<data_dir>/logs/token_budget.jsonl`` (one JSON
    line per session) so Hero 6 (Token Budget Live View) and the
    `codevira budget history` CLI can read historical accounting.

    Args:
        session_id: which session to drop
        project_root: where to persist. If None, the persist step is
            skipped (in-memory only) — used by tests that don't want
            disk side effects.

    Returns:
        Summary dict (same as ``meter.summary()``), or ``None`` if no
        meter for that session_id existed.
    """
    global _current_session_id
    with _meters_lock:
        meter = _meters.pop(session_id, None)
        if _current_session_id == session_id:
            _current_session_id = None
        if meter is None:
            return None
        summary = meter.summary()

    # Persist outside the lock — disk I/O shouldn't hold module-level state.
    # Failures in persist NEVER affect the in-memory session lifecycle —
    # the meter is already removed; we just lose the historical record.
    if project_root is not None:
        try:
            _persist_session_summary(project_root, summary)
        except Exception:  # noqa: BLE001 — best-effort persistence
            pass

    return summary


def _persist_session_summary(project_root: "Path", summary: dict[str, Any]) -> None:
    """Append one JSONL record to ``<data_dir>/logs/token_budget.jsonl``.

    File format: one JSON object per line, schema:
      {
        "session_id": "...",
        "ended_at": <epoch seconds>,
        "injected_total": N,
        "used_total": N,
        "efficiency": 0.0-1.0,
        "top_wasted_sources": [{"source": "...", "wasted": N, "injected": N}, ...]
      }

    Hero 6 reads this for `codevira budget history`.

    Concurrency contract (Week-2 R4 design review):
      • Single writer per project. Codevira spins up one MCP server per
        AI session; multiple sessions on the same project would race
        the partial-line guard's read-then-write. v2.0 doesn't support
        this; if it ever does we move to a tempfile + atomic-rename
        write strategy instead of append.
      • Record size < PIPE_BUF (4 KiB). The schema is fixed and
        ``top_wasted_sources`` is capped to 5 entries by ``summary()``,
        so a typical record is ~500 bytes — well under the 4-KiB POSIX
        atomic-write boundary. We rely on this for crash recovery
        (a partial line on disk is always exactly one truncated record,
        never two interleaved records).

    Failure modes:
      • Disk full → write() raises OSError. Caller wraps this in
        try/except (see ``end_session``); the meter is already removed
        from in-memory state, so the session is just lost from the log.
      • Partial line from a prior crash → guarded by sniffing the last
        byte of the file and emitting a separator newline if needed.
        See R3 finding M17.1 + the regression test in
        ``tests/engine/test_week2_edge_cases.py::TestTokenLogCrashRecovery``.
    """
    import json
    import time
    from pathlib import Path as _Path

    from mcp_server.paths import _sanitize_path_key, get_global_home

    pr = _Path(project_root).resolve()
    key = _sanitize_path_key(pr)
    data_dir = get_global_home() / "projects" / key
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "token_budget.jsonl"

    record = {
        "session_id": summary.get("session_id"),
        "ended_at": time.time(),
        "injected_total": summary.get("injected_total", 0),
        "used_total": summary.get("used_total", 0),
        "efficiency": summary.get("efficiency", 0.0),
        "top_wasted_sources": summary.get("top_wasted_sources", []),
    }

    # Append-only; one JSON object per line, terminated by newline.
    # R3 crash-recovery finding: if a previous process died mid-write the
    # file may not end with a newline. Without a guard, the next append
    # concatenates to the partial record and BOTH lines become unreadable.
    # We sniff the last byte and emit a separator newline if needed —
    # cost: one extra read+write per persist on a healthy file (~ns).
    line = json.dumps(record, ensure_ascii=False) + "\n"
    payload = line.encode("utf-8")
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        size = 0
    needs_separator = False
    if size > 0:
        try:
            with open(log_path, "rb") as f:
                f.seek(max(0, size - 1))
                last = f.read(1)
                if last and last != b"\n":
                    needs_separator = True
        except OSError:
            # If we can't read, fall through and write anyway — the worst
            # case is we lose this one record on next read, not worse than
            # not writing it.
            pass
    with open(log_path, "ab") as f:
        if needs_separator:
            f.write(b"\n")
        f.write(payload)


# Hard cap on bytes we'll read out of token_budget.jsonl per call.
# 16 MiB is enough for ~80,000 sessions at ~200 bytes/record. Beyond
# that we serve only the tail and stop — no OOM if the log grows
# pathologically large (caught in Week-2 Tier-1 QA).
_HISTORY_TAIL_BYTES_CAP = 16 * 1024 * 1024


def read_session_history(
    project_root: "Path", *, limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the most recent N session summaries from token_budget.jsonl.

    Returns newest-first. Used by Hero 6's `codevira budget history` CLI.
    Returns empty list if the log doesn't exist or is unreadable.

    Memory-bounded: reads at most ``_HISTORY_TAIL_BYTES_CAP`` bytes from
    the end of the file regardless of total log size. Older records past
    that boundary are not returned.
    """
    import json
    import os as _os
    from pathlib import Path as _Path

    from mcp_server.paths import _sanitize_path_key, get_global_home

    pr = _Path(project_root).resolve()
    key = _sanitize_path_key(pr)
    log_path = get_global_home() / "projects" / key / "logs" / "token_budget.jsonl"

    if not log_path.exists():
        return []

    # Clamp limit defensively — same shape as the previous implementation.
    capped_limit = int(max(1, min(limit, 10_000)))

    try:
        size = log_path.stat().st_size
        # Seek to a tail window that's at most the cap. If the file is
        # smaller than the cap, just read it all.
        offset = max(0, size - _HISTORY_TAIL_BYTES_CAP)
        with open(log_path, "rb") as f:
            if offset:
                f.seek(offset)
                # Drop the partial line at the start of our window.
                f.readline()
            tail_bytes = f.read()
    except OSError:
        return []

    try:
        text = tail_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — defensive
        return []

    lines = text.splitlines()
    tail = lines[-capped_limit:]
    out: list[dict[str, Any]] = []
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip malformed line, keep scanning
    return out


def reset_meters() -> None:
    """Tests only — clear all session state."""
    global _current_session_id
    with _meters_lock:
        _meters.clear()
        _current_session_id = None
