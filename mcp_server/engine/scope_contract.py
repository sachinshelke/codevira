"""
scope_contract.py — Hero 3's per-session scope storage.

Was a stub through Weeks 1-11; Week 12 ships the real implementation.

Public API (stable since v2.0-alpha):
  - ``ScopeContract`` dataclass — what the AI is allowed to change
  - ``current_contract() -> ScopeContract | None`` — global accessor used
    by ``SignalContext.scope_contract``. Returns the most-recently-set
    contract OR None if none active. Kept for backward compatibility
    with policies that read scope without knowing session_id.
  - ``set_session_contract(session_id, contract)`` — Hero 3 calls this
    on UserPromptSubmit
  - ``get_session_contract(session_id) -> ScopeContract | None`` —
    Hero 3 calls this on PreToolUse
  - ``clear_all() / clear()`` — tests only

Storage:
  Per-session contracts live in a process-module dict keyed by session_id.
  Each entry has a created_at timestamp; entries older than the TTL
  (default 3600 s) are evicted on read. Bounds memory and prevents
  stale contracts from misclassifying after a long break.

Bug-X-shape defenses (lessons #15-21 applied from start):
  - All public functions defend against None inputs (return None / no-op
    rather than KeyError)
  - TTL eviction is bounded by absolute time (not iteration count) so a
    long-running process can't accumulate stale contracts indefinitely
  - ``clear_all()`` is exposed for tests AND for ``codevira clean``
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeContract:
    """Per-session contract describing what the AI is allowed to change.

    Built by Hero 3 on UserPromptSubmit; read on PreToolUse and by other
    policies that want intent-aware behavior.

    Attributes:
        session_id: which session this contract belongs to. Empty string
            means "session-less", which Hero 3 treats as "do not store"
            (the build phase silently skips).
        allowed_files: PROJECT-RELATIVE paths the AI may modify. Set
            semantics (no duplicates). Empty = no contract / no
            enforcement (Hero 3's enforce phase short-circuits to allow).
        allowed_change_types: not enforced in v2.0-alpha. Reserved for
            v2.1 (e.g., {"fix", "refactor"}).
        max_loc_delta: maximum lines-changed before a violation. 0 = no
            cap. Informational in v2.0-alpha; reserved for v2.1.
        original_intent: the parsed intent from the user's prompt
            (free-text summary; e.g. ``"fix-bug"``).
        original_prompt: the user prompt that built this contract. Used
            by the enforce phase's block message so the AI sees WHY
            it's being blocked.
        created_at: epoch seconds when the contract was built. Used for
            TTL eviction.
    """

    session_id: str = ""
    allowed_files: frozenset[str] = field(default_factory=frozenset)
    allowed_change_types: tuple[str, ...] = ()
    max_loc_delta: int = 0
    original_intent: str = ""
    original_prompt: str = ""
    created_at: float = 0.0


# ---------------------------------------------------------------------
# TTL helpers
# ---------------------------------------------------------------------

_DEFAULT_MAX_AGE_SECONDS = 3600
_MAX_AGE_FLOOR = 60
_MAX_AGE_CEIL = 86400


def _max_age_seconds() -> int:
    """Read TTL from env, clamped to [60, 86400] (1 minute to 1 day)."""
    raw = os.environ.get("CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS")
    if not raw:
        return _DEFAULT_MAX_AGE_SECONDS
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE_SECONDS
    return max(_MAX_AGE_FLOOR, min(v, _MAX_AGE_CEIL))


# ---------------------------------------------------------------------
# Per-session storage
# ---------------------------------------------------------------------

# session_id → ScopeContract (with .created_at on the contract itself)
_session_contracts: dict[str, ScopeContract] = {}

# The most-recently-set contract — preserved for SignalContext.scope_contract,
# which doesn't have session_id context. Returns the latest scope across
# any session, which is "best effort" but useful for single-session use.
_current: ScopeContract | None = None


def _evict_expired(now: float | None = None) -> None:
    """Drop contracts older than the TTL.

    Called on every read (set/get) so eviction is bounded by call rate.
    Defensive: never raises; on clock skew (negative age) we simply
    don't evict.
    """
    if now is None:
        now = time.time()
    max_age = _max_age_seconds()
    cutoff = now - max_age
    if cutoff < 0:
        return  # implausible clock; skip
    expired = [sid for sid, c in _session_contracts.items() if c.created_at < cutoff]
    for sid in expired:
        _session_contracts.pop(sid, None)


def set_session_contract(session_id: str, contract: ScopeContract) -> None:
    """Store a contract for the given session_id. Hero 3 calls this on
    UserPromptSubmit. Replaces any existing contract for the same session.

    Defensive:
      - Empty session_id is a no-op (Hero 3 also short-circuits there).
      - None contract is a no-op.
    """
    if not session_id or contract is None:
        return
    _evict_expired()
    _session_contracts[session_id] = contract
    global _current
    _current = contract


def get_session_contract(session_id: str | None) -> ScopeContract | None:
    """Return the contract for ``session_id``, or None if missing/expired.

    Hero 3 calls this on PreToolUse. Defensive: None / empty input
    returns None (caller treats as "no contract → allow").
    """
    if not session_id:
        return None
    _evict_expired()
    return _session_contracts.get(session_id)


def current_contract() -> ScopeContract | None:
    """Return the most-recently-set contract across any session.

    Used by ``SignalContext.scope_contract``. Best-effort accessor for
    callers that don't have session_id context.
    """
    return _current


def set_current_contract(contract: ScopeContract | None) -> None:
    """Tests + Hero 3's UserPromptSubmit handler. Sets the global
    "current" pointer without keying by session_id. Combined with
    ``set_session_contract`` for the per-session storage."""
    global _current
    _current = contract


def clear_all() -> None:
    """Clear all per-session contracts AND the current pointer. Tests
    + ``codevira clean`` only — never call from production paths."""
    global _current
    _session_contracts.clear()
    _current = None


# Backward-compat alias from the v2.0 stub era.
clear = clear_all


# ---------------------------------------------------------------------
# Test-introspection helpers
# ---------------------------------------------------------------------


def _all_session_ids() -> list[str]:
    """Tests only: snapshot of currently-stored session_ids."""
    return list(_session_contracts.keys())


def _stored_count() -> int:
    """Tests only: number of contracts currently in storage."""
    return len(_session_contracts)
