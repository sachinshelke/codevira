"""
mcp_server.engine.policies — built-in policy plugins.

Each Hero registers one (or more) ``Policy`` subclasses here. The engine
auto-discovers them via ``register_default_policies()`` (see
``mcp_server/engine/__init__.py``).

Heroes shipped in v2.2.0+ (after the 2026-05-22 surface-cut audit):
  - Hero 4: blast_radius.BlastRadiusVeto
  - Hero 1: decision_lock.DecisionLock (the unique enforcement wedge)
  - Hero 5: relevance_inject.RelevanceInject (v2.2.0 — replaces
            cross_session.CrossSessionConsistency)
  - Hero 6: token_budget.TokenBudgetPersist
  - Hero 2: anti_regression.AntiRegression
  - post_edit_refresh.PostEditGraphRefresh (v2.1.2 Item 4 — auto graph refresh)

Removed in v2.2.0+ (high-noise, low-value per audit):
  - Hero 7: live_style.LiveStyleEnforcement — consumed preferences/rules
            surface; both deleted as noise.
  - Hero 10: ai_promotion.AIPromotionScore — SessionStart ranking that
             produced noise; never validated in real sessions.
  - Hero 9: intent_inference.ProactiveIntentInference — guessing user
            intent → wrong half the time → user annoyance.
  - Hero 3: scope_contract.ProactiveScopeContractLock — never fires;
            complex; users don't trust it.

Heroes still scaffolded but not implemented:
  - Hero 8: decision_replay.* (browse surface, not a Policy class)
"""

from __future__ import annotations

from mcp_server.engine.policies.anti_regression import AntiRegression
from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
from mcp_server.engine.policies.decision_lock import DecisionLock
from mcp_server.engine.policies.prompt_capture import PromptCapture
from mcp_server.engine.policies.relevance_inject import RelevanceInject
from mcp_server.engine.policies.session_log_enforcer import SessionLogEnforcer
from mcp_server.engine.policies.token_budget import TokenBudgetPersist

__all__ = [
    "AntiRegression",
    "BlastRadiusVeto",
    "DecisionLock",
    "PromptCapture",
    "RelevanceInject",
    "SessionLogEnforcer",
    "TokenBudgetPersist",
]
