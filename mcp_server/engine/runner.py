"""
runner.py — engine dispatch + verdict combination.

Public entry points for the engine:

    register_policy(policy)   — at import time, plug a Policy in
    dispatch(event)           — at hook fire time, run all relevant
                                 policies and return combined verdict
    registered_policies()     — for `codevira doctor` and tests
    reset_policies()          — for tests; never call from production

The engine philosophy in this file:

  - **Never break a hook.** A bad policy must not crash the whole hook.
    Each evaluate() runs inside a try/except; a failure logs to crash_logger
    and returns allow.

  - **Fail open.** If the engine itself crashes (signal collection blew
    up, etc.) we return allow. Better to miss a block than to break the
    user's workflow.

  - **Performance budget.** dispatch() is on the hot path of every hook.
    Lazy signal loading + per-event signal caching keeps p95 < 50 ms with
    5 policies registered.

  - **Verdict combination rules.** Block wins (first by priority).
    Otherwise warn/inject concatenate; otherwise allow. See
    docs/heroes/00-engine.md "Verdict combination rules".

  - **Engine kill switch.** Setting CODEVIRA_ENGINE=0 in the env disables
    every policy at the dispatcher; the engine returns allow without
    invoking anything. Lets users escape if a buggy policy ships.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Module-level policy registry. One per process. Registration happens at
# import time when each hero's module is loaded.
# ----------------------------------------------------------------------
_POLICIES: list[Policy] = []


def register_policy(policy: Policy) -> None:
    """Register a Policy with the engine. Idempotent.

    Re-registering a policy with the same ``name`` replaces the old one
    in place — useful in tests, harmless in production.

    Raises ValueError if the policy has no ``name``.
    """
    if not policy.name:
        raise ValueError(
            f"Policy {policy.__class__.__name__} must set a non-empty `name`"
        )
    # Replace existing entry with same name, otherwise append.
    for i, p in enumerate(_POLICIES):
        if p.name == policy.name:
            _POLICIES[i] = policy
            return
    _POLICIES.append(policy)


def registered_policies() -> list[Policy]:
    """Return the current list of registered policies (read-only copy)."""
    return list(_POLICIES)


def reset_policies() -> None:
    """Clear the registry. Tests only — production never calls this."""
    _POLICIES.clear()


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------


def dispatch(event: HookEvent) -> PolicyVerdict:
    """Run all relevant policies on ``event`` and return a combined verdict.

    This is the single entry point invoked by the wiring layer
    (Claude Code hook scripts, MCP call_tool dispatch, tests).

    Algorithm:

      1. Honor CODEVIRA_ENGINE=0 escape hatch.
      2. Filter registered policies to those whose ``handles`` includes
         this event's type.
      3. Sort by priority (descending).
      4. Build a SignalContext shared across all policies (lazy + cached).
      5. Run each policy's ``evaluate(event)``:
         - First ``block`` wins; subsequent policies still run for telemetry
           but their verdicts don't change the final outcome.
         - All ``warn`` accumulate.
         - All ``inject`` accumulate.
      6. Combine into a single PolicyVerdict.

    Always returns a PolicyVerdict — never raises.
    """
    # 1. Engine kill switch
    if os.environ.get("CODEVIRA_ENGINE", "1") == "0":
        return PolicyVerdict.allow(metadata={"engine_disabled": True})

    # 2 + 3: filter by event type, sort by priority
    eligible = [
        p for p in _POLICIES if event.event_type in set(p.handles)
    ]
    eligible.sort(key=lambda p: p.priority, reverse=True)

    if not eligible:
        # No policy claims this event type — fast path.
        return PolicyVerdict.allow()

    # 4. Build the signal context once, share across all policies.
    signals = SignalContext(project_root=event.project_root)

    # 5. Run policies. We ATTACH `signals` to the event via a side channel
    #    rather than mutating the (frozen) HookEvent. Policies access via
    #    ``event.signals`` indirectly; for simplicity we set an attribute
    #    on a per-call wrapper.
    #
    #    Implementation detail: we use object.__setattr__ since HookEvent
    #    is frozen. This is the one allowed mutation — and it's a synthetic
    #    attribute for accessor convenience, not a state-carrying field.
    object.__setattr__(event, "signals", signals)

    blocks: list[PolicyVerdict] = []
    warns: list[PolicyVerdict] = []
    injects: list[PolicyVerdict] = []

    for policy in eligible:
        verdict = _safe_evaluate(policy, event)
        # Auto-fill the policy name on the verdict so downstream code
        # (logs, doctor, hook-script JSON) can attribute decisions.
        if verdict.policy is None:
            object.__setattr__(verdict, "policy", policy.name)

        if verdict.action == "block":
            blocks.append(verdict)
        elif verdict.action == "warn":
            warns.append(verdict)
        elif verdict.action == "inject":
            injects.append(verdict)
        # "allow" contributes nothing to the combined output

    # 6. Combine
    return _combine(blocks, warns, injects)


def _safe_evaluate(policy: Policy, event: HookEvent) -> PolicyVerdict:
    """Run a policy's evaluate inside a guard.

    Failures get logged to the crash_logger (for `codevira report`) but
    never propagate. The policy is treated as if it returned `allow`.
    """
    started = time.perf_counter()
    try:
        verdict = policy.evaluate(event)
        if not isinstance(verdict, PolicyVerdict):
            logger.warning(
                "Policy %s returned non-PolicyVerdict %r; treating as allow",
                policy.name, type(verdict).__name__,
            )
            return PolicyVerdict.allow(metadata={"_policy_error": "bad_return_type"})
        return verdict
    except Exception as e:  # noqa: BLE001
        # Log to crash_logger so users can find this in `codevira report`.
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context=f"engine.dispatch policy={policy.name} event={event.event_type}")
        except Exception:  # pragma: no cover — crash_logger itself failing
            pass
        logger.warning(
            "Policy %s raised %s: %s — treating as allow",
            policy.name, type(e).__name__, e,
        )
        return PolicyVerdict.allow(metadata={"_policy_error": str(e)})
    finally:
        elapsed = (time.perf_counter() - started) * 1000  # ms
        if elapsed > 100:
            # Per-policy slow-evaluate warning. Engine SLA is 50ms p95
            # across ALL policies; one policy spending >100ms is suspicious.
            logger.warning(
                "Slow policy %s: %0.1f ms on %s",
                policy.name, elapsed, event.event_type,
            )


def _combine(
    blocks: list[PolicyVerdict],
    warns: list[PolicyVerdict],
    injects: list[PolicyVerdict],
) -> PolicyVerdict:
    """Combine per-policy verdicts into the engine's single output.

    Rules (also documented in docs/heroes/00-engine.md):
      - Any ``block`` wins. First (highest priority) block's message is
        used; subsequent blocks' policy names are recorded in metadata
        for telemetry / `codevira doctor`.
      - No block + any ``warn`` → action is ``warn``, messages joined.
      - No block + no warn + any ``inject`` → action is ``inject``,
        contexts joined.
      - Otherwise ``allow``.
    """
    if blocks:
        primary = blocks[0]
        meta = dict(primary.metadata)
        if len(blocks) > 1:
            meta["other_blocking_policies"] = [b.policy for b in blocks[1:]]
        return PolicyVerdict(
            action="block",
            message=primary.message,
            policy=primary.policy,
            metadata=meta,
        )
    if warns:
        joined = "\n".join(filter(None, (w.message for w in warns)))
        meta = {"warning_policies": [w.policy for w in warns]}
        return PolicyVerdict(action="warn", message=joined, policy=None, metadata=meta)
    if injects:
        joined_ctx = "\n\n".join(filter(None, (i.inject_context for i in injects)))
        joined_msg = "\n".join(filter(None, (i.message for i in injects)))
        meta = {"inject_policies": [i.policy for i in injects]}
        return PolicyVerdict(
            action="inject",
            inject_context=joined_ctx,
            message=joined_msg or None,
            policy=None,
            metadata=meta,
        )
    return PolicyVerdict.allow()
