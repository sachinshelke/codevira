"""
codevira engine — the shared infrastructure for all v2.0 hero policies.

Heroes 1-10 each register one (or more) Policy objects. The engine intercepts
AI tool calls (via Claude Code lifecycle hooks and MCP tool dispatch),
collects signals, runs registered policies, and returns a combined verdict
that the hook layer translates into block / warn / inject / allow.

Public API:

    from mcp_server.engine import (
        Policy, PolicyVerdict, HookEvent, EventType,
        register_policy, dispatch,
    )

The engine is invisible to users; heroes are the visible outcome.

See docs/heroes/00-engine.md for the full design.
"""
from __future__ import annotations

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.runner import dispatch, register_policy, registered_policies, reset_policies


def register_default_policies() -> None:
    """Register every Hero policy that ships enabled-by-default.

    Called from the engine's lifecycle hook entry (`mcp_server.cli`'s
    `engine handle ...` subcommand) and from the MCP server startup.

    Two contracts:
      1. Idempotent — running twice does NOT register duplicates.
      2. **``Policy.enabled_by_default = False`` opt-out is honored.**
         Bug 3 (Week-7 retrospective): the flag was previously declared
         on the base class but never checked. Setting it to False had
         zero effect; the demo_policy worked around this via
         ``maybe_register()`` with manual env-var gating. Now the
         registration helper itself respects the flag.
    """
    from mcp_server.engine.policies.ai_promotion import AIPromotionScore
    from mcp_server.engine.policies.anti_regression import AntiRegression
    from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
    from mcp_server.engine.policies.cross_session import CrossSessionConsistency
    from mcp_server.engine.policies.decision_lock import DecisionLock
    from mcp_server.engine.policies.intent_inference import ProactiveIntentInference
    from mcp_server.engine.policies.live_style import LiveStyleEnforcement
    from mcp_server.engine.policies.token_budget import TokenBudgetPersist

    for policy_cls in (
        BlastRadiusVeto,             # Hero 4 (Week 4)
        DecisionLock,                # Hero 1 (Week 5)
        CrossSessionConsistency,     # Hero 5 (Week 6)
        TokenBudgetPersist,          # Hero 6 (Week 7)
        AntiRegression,              # Hero 2 (Week 8)
        LiveStyleEnforcement,        # Hero 7 (Week 9)
        AIPromotionScore,            # Hero 10 (Week 10)
        ProactiveIntentInference,    # Hero 9 (Week 11)
    ):
        if not policy_cls.enabled_by_default:
            continue  # opt-in only — caller registers manually
        if any(p.name == policy_cls.name for p in registered_policies()):
            continue  # already registered (idempotent)
        register_policy(policy_cls())

__all__ = [
    # Event types
    "EventType",
    "HookEvent",
    # Policy contract
    "Policy",
    "PolicyVerdict",
    # Runtime
    "dispatch",
    "register_policy",
    "register_default_policies",
    "registered_policies",
    "reset_policies",
]

# Engine version — increment when the policy plugin contract changes.
__engine_version__ = "0.1.0"
