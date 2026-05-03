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
    Idempotent: running it twice does NOT register duplicates.
    """
    # Hero 4 — Blast-Radius Veto. First shipping policy.
    from mcp_server.engine.policies.blast_radius import BlastRadiusVeto

    if not any(p.name == BlastRadiusVeto.name for p in registered_policies()):
        register_policy(BlastRadiusVeto())

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
