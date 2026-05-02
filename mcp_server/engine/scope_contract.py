"""
scope_contract.py — interface stub for Hero 3 (Scope Contract Lock).

Hero 3 is scheduled for v2.0 Week 12. We ship the interface stub now so:

  - SignalContext.scope_contract has a stable import target
  - Other policies can read the (currently always None) contract without
    coupling to Hero 3's not-yet-built implementation
  - Tests can mock current_contract() trivially

When Hero 3 lands it replaces this stub's body with real intent parsing
and per-session contract storage. The signature ``current_contract() ->
ScopeContract | None`` is the public API and won't change.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScopeContract:
    """Per-session contract describing what the AI is allowed to change.

    Hero 3 builds this from the user's prompt at UserPromptSubmit time;
    other policies (esp. Blast-Radius) may read it to refine decisions.

    Fields are deliberately permissive — Hero 3 will narrow them based on
    intent classification.

    Attributes:
        session_id: which session this contract belongs to
        allowed_files: glob patterns of files the AI may modify; empty
            means "no restriction"
        allowed_change_types: list of change kinds permitted ("fix",
            "refactor", "rename", "test", "docs"). Empty = unrestricted.
        max_loc_delta: maximum lines-changed before a violation; 0 = no
            cap.
        original_intent: the parsed intent from the user's prompt
            (free-text summary)
    """

    session_id: str = ""
    allowed_files: list[str] = field(default_factory=list)
    allowed_change_types: list[str] = field(default_factory=list)
    max_loc_delta: int = 0
    original_intent: str = ""


# ----------------------------------------------------------------------
# Process-wide "current contract" — set by Hero 3's UserPromptSubmit
# policy, read by anyone else who cares. None until Hero 3 lands.
# ----------------------------------------------------------------------

_current: ScopeContract | None = None


def current_contract() -> ScopeContract | None:
    """Return the active scope contract, or None if none set.

    Used by SignalContext.scope_contract. Returns None until Hero 3
    populates it.
    """
    return _current


def set_current_contract(contract: ScopeContract | None) -> None:
    """Hero 3 calls this on UserPromptSubmit. Tests use it too."""
    global _current
    _current = contract


def clear() -> None:
    """Tests only."""
    global _current
    _current = None
