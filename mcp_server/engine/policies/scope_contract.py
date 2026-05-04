"""
scope_contract.py — Hero 3: Proactive Scope Contract Lock policy.

Two-event policy: builds a per-session scope contract on
USER_PROMPT_SUBMIT and enforces it on PRE_TOOL_USE.

The first hero to handle two event types in one policy. Engine's
dispatch loop already supports this (``handles`` is a tuple).

**Off by default**. Opt-in via ``CODEVIRA_SCOPE_LOCK_MODE=warn`` (advisory)
or ``=block`` (enforcing). Highest-risk hero in the master plan: relies
on intent inference (which can mis-classify) AND blocks Edits (which
can frustrate users on false positives). The default is ``off`` so
v2.0-alpha ships with this hero silent — users explicitly turn it on
when they want scope discipline.

Reuses Hero 9's regex intent classifier + file-mention extractor (both
already shipped + tested). New code is the contract-building logic +
enforcement check.

See ``docs/heroes/03-scope-contract.md`` for the full spec, edge cases,
and risks.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.intent_classifier import (
    classify_intent, extract_file_mentions,
    INTENT_FIX_BUG, INTENT_ADD_FEATURE, INTENT_REFACTOR, INTENT_EXPLAIN,
    INTENT_TEST, INTENT_DOCS, INTENT_OTHER,
)
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.scope_contract import (
    ScopeContract, set_session_contract, get_session_contract,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Defaults + bounds
# ---------------------------------------------------------------------

_DEFAULT_MODE = "off"  # opt-in by default
_MODES = ("off", "warn", "block")

#: Min prompt length to bother building a contract. Same threshold as
#: Hero 5 / Hero 9 (short prompts rarely have meaningful scope).
_MIN_PROMPT_CHARS = 10

#: Cap on file mentions extracted (matches Hero 9's default).
_DEFAULT_MAX_FILES = 5

#: Intents that DON'T get a scope contract built — let the AI work freely.
_NO_BUILD_INTENTS: frozenset[str] = frozenset({
    INTENT_TEST, INTENT_DOCS, INTENT_OTHER, INTENT_EXPLAIN,
})

#: Default LOC delta caps per intent (informational; not enforced in v2.0-alpha).
_LOC_BUDGET_BY_INTENT: dict[str, int] = {
    INTENT_FIX_BUG: 50,
    INTENT_REFACTOR: 200,
    INTENT_ADD_FEATURE: 500,
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _resolve_in_project_files(
    file_mentions: list[str],
    project_root: Path,
) -> frozenset[str]:
    """Resolve each mention against project_root and KEEP only those
    inside it. Returns a frozenset of PROJECT-RELATIVE path strings.

    Bug-5 lesson (Week-11 deep re-audit): user-controlled file paths
    must be containment-checked. A prompt like "fix '../../etc/passwd.py'"
    must NOT end up in the allowed_files set — otherwise the AI could
    use scope-lock as a confused-deputy to "validate" out-of-project
    edits.

    Also defends against macOS /tmp → /private/tmp symlink mismatch
    by resolving project_root once before the loop (same fix Hero 9 has).
    """
    out: set[str] = set()
    try:
        resolved_root = project_root.resolve()
    except (OSError, ValueError):
        resolved_root = project_root

    for mention in file_mentions:
        try:
            abs_path = (project_root / mention).resolve()
        except (OSError, ValueError):
            continue
        try:
            rel = abs_path.relative_to(resolved_root)
        except ValueError:
            # Out of project — drop (Bug-5 defense).
            logger.debug(
                "scope_contract: dropping out-of-project mention %r "
                "(resolved %s outside %s)",
                mention, abs_path, resolved_root,
            )
            continue
        out.add(str(rel))
    return frozenset(out)


def _target_in_scope(
    target_file: Path,
    project_root: Path,
    allowed_files: frozenset[str],
) -> bool:
    """Check whether the proposed Edit's target_file is in the contract.

    Comparison is via project-relative path (same shape as allowed_files
    stores).

    Defensive:
      - Empty allowed_files = no contract narrowing → always True
        (caller's enforce phase short-circuits earlier, but this is
        belt-and-suspenders)
      - target_file outside project_root: ValueError from relative_to
        → return False (block; this shouldn't happen because the wiring
        layer strips out-of-project target_files, but defense-in-depth)
    """
    if not allowed_files:
        return True
    try:
        resolved_root = project_root.resolve()
    except (OSError, ValueError):
        resolved_root = project_root
    try:
        rel = str(target_file.resolve().relative_to(resolved_root))
    except (OSError, ValueError):
        return False
    return rel in allowed_files


# ---------------------------------------------------------------------
# Block / warn message formatter
# ---------------------------------------------------------------------


def _format_block_message(
    *,
    target_file: Path,
    project_root: Path,
    contract: ScopeContract,
) -> str:
    """Build the block / warn message. Per Lesson #19, this MUST contain
    the offending file AND the original prompt — header-only messages
    fail the user's "tell me WHY" expectation.
    """
    try:
        rel = str(target_file.resolve().relative_to(project_root.resolve()))
    except (OSError, ValueError):
        rel = str(target_file)

    # Format allowed-files list (capped to keep message readable)
    allowed_list = sorted(contract.allowed_files)
    if len(allowed_list) > 5:
        allowed_str = ", ".join(allowed_list[:5]) + f", +{len(allowed_list) - 5} more"
    else:
        allowed_str = ", ".join(allowed_list) if allowed_list else "(none)"

    # Truncate prompt if huge
    prompt = contract.original_prompt
    if len(prompt) > 200:
        prompt = prompt[:197] + "…"

    return (
        f"🔒 Scope-lock veto on {rel}.\n"
        f"This Edit is outside the scope inferred from your prompt.\n\n"
        f"Original prompt: {prompt!r}\n"
        f"Inferred intent: {contract.original_intent}\n"
        f"Allowed files: {allowed_str}\n\n"
        f"To proceed:\n"
        f"  1. Submit a follow-up prompt that includes {rel}, OR\n"
        f"  2. Set CODEVIRA_SCOPE_LOCK_MODE=warn for advisory-only, OR\n"
        f"  3. Set CODEVIRA_SCOPE_LOCK_MODE=off to disable scope-lock."
    )


# ---------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------


class ProactiveScopeContractLock(Policy):
    """Build a per-session scope contract from the user's prompt;
    enforce it on subsequent Edit/Write calls.

    Verdict shapes:
      - allow  : event-type filter, mode=off, no scope to enforce, in-scope edit
      - warn   : out-of-scope edit when mode=warn
      - block  : out-of-scope edit when mode=block

    Off by default — users opt in per project.
    """

    name = "scope_contract_lock"
    handles = (EventType.USER_PROMPT_SUBMIT, EventType.PRE_TOOL_USE)
    enabled_by_default = True  # registered, but mode=off means silent
    priority = 90  # below Decision Lock (100) but above most others

    # ------- config (env-driven) -------

    def _config(self) -> dict[str, Any]:
        mode_raw = (
            os.environ.get("CODEVIRA_SCOPE_LOCK_MODE", _DEFAULT_MODE) or ""
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {"mode": mode}

    def describe(self) -> dict[str, Any]:
        cfg = self._config()
        return {
            "name": self.name,
            "priority": self.priority,
            "handles": [str(h) for h in self.handles],
            "enabled_by_default": self.enabled_by_default,
            **cfg,
            "config": {
                "mode": {
                    "values": list(_MODES),
                    "default": _DEFAULT_MODE,
                    "env": "CODEVIRA_SCOPE_LOCK_MODE",
                    "description": "off / warn / block (off-by-default)",
                },
                "max_age_seconds": {
                    "default": 3600,
                    "env": "CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS",
                    "description": "Contract TTL (1 minute to 1 day)",
                },
            },
        }

    # ------- main entry point -------

    def evaluate(
        self,
        event: HookEvent,
        signals=None,
    ) -> PolicyVerdict:
        # Stage 0: event-type filter
        if event.event_type not in (
            EventType.USER_PROMPT_SUBMIT,
            EventType.PRE_TOOL_USE,
        ):
            return PolicyVerdict.allow()

        # Stage 1: mode gate (off-by-default)
        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: dispatch by event type
        if event.event_type == EventType.USER_PROMPT_SUBMIT:
            return self._handle_prompt(event, config)
        return self._handle_edit(event, config)

    # ------- build phase: USER_PROMPT_SUBMIT -------

    def _handle_prompt(
        self, event: HookEvent, config: dict[str, Any],
    ) -> PolicyVerdict:
        # No session_id → no storage → silent allow
        session_id = event.session_id
        if not session_id:
            return PolicyVerdict.allow()

        # Empty / short prompt → no scope to build
        prompt = (event.prompt_text or "").strip()
        if len(prompt) < _MIN_PROMPT_CHARS:
            return PolicyVerdict.allow()

        # Classify intent. Skip "open-ended" intents.
        intent = classify_intent(prompt)
        if intent in _NO_BUILD_INTENTS:
            return PolicyVerdict.allow()

        # Extract file mentions, defended against path-traversal (Bug-5).
        mentions = extract_file_mentions(prompt, max_files=_DEFAULT_MAX_FILES)
        allowed = _resolve_in_project_files(mentions, event.project_root)
        if not allowed:
            # No concrete in-project files → no contract worth enforcing.
            return PolicyVerdict.allow()

        contract = ScopeContract(
            session_id=session_id,
            allowed_files=allowed,
            allowed_change_types=(intent,),
            max_loc_delta=_LOC_BUDGET_BY_INTENT.get(intent, 0),
            original_intent=intent,
            original_prompt=prompt,
            created_at=time.time(),
        )
        set_session_contract(session_id, contract)
        return PolicyVerdict.allow(metadata={
            "scope_built": True,
            "intent": intent,
            "allowed_files_count": len(allowed),
        })

    # ------- enforce phase: PRE_TOOL_USE -------

    def _handle_edit(
        self, event: HookEvent, config: dict[str, Any],
    ) -> PolicyVerdict:
        # Bug-7 lesson: use is_edit() — covers Edit/Write/MultiEdit/NotebookEdit
        # (and is gated to PRE_TOOL_USE in the same call).
        if not event.is_edit():
            return PolicyVerdict.allow()

        if event.target_file is None:
            return PolicyVerdict.allow()

        if not event.session_id:
            return PolicyVerdict.allow()

        contract = get_session_contract(event.session_id)
        if contract is None:
            return PolicyVerdict.allow()

        if not contract.allowed_files:
            # Belt-and-suspenders: empty contract = no narrowing
            return PolicyVerdict.allow()

        if _target_in_scope(
            event.target_file, event.project_root, contract.allowed_files,
        ):
            return PolicyVerdict.allow()

        # Out of scope — verdict per mode
        message = _format_block_message(
            target_file=event.target_file,
            project_root=event.project_root,
            contract=contract,
        )
        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "intent": contract.original_intent,
            "allowed_files": sorted(contract.allowed_files),
            "mode": config["mode"],
        }

        if config["mode"] == "block":
            return PolicyVerdict.block(message=message, metadata=metadata)
        # mode == "warn"
        return PolicyVerdict.warn(message=message, metadata=metadata)
