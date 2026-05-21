"""
mcp_server.engine.policies — built-in policy plugins.

Each Hero registers one (or more) ``Policy`` subclasses here. The engine
auto-discovers them via ``register_default_policies()`` (see
``mcp_server/engine/__init__.py``).

Heroes that have shipped:
  - Hero 4: blast_radius.BlastRadiusVeto (Week 4)
  - Hero 1: decision_lock.DecisionLock (Week 5)
  - Hero 5: relevance_inject.RelevanceInject (v2.2.0 — replaces
            cross_session.CrossSessionConsistency removed in v2.2.0)
  - Hero 6: token_budget.TokenBudgetPersist (Week 7)
  - Hero 2: anti_regression.AntiRegression (Week 8)
  - Hero 7: live_style.LiveStyleEnforcement (Week 9)
  - Hero 10: ai_promotion.AIPromotionScore (Week 10)
  - Hero 9: intent_inference.ProactiveIntentInference (Week 11)
  - Hero 3: scope_contract.ProactiveScopeContractLock (Week 12)

Heroes still scaffolded but not implemented:
  - Hero 8: decision_replay.* (Week 13)
"""

from __future__ import annotations

from mcp_server.engine.policies.ai_promotion import AIPromotionScore
from mcp_server.engine.policies.anti_regression import AntiRegression
from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
from mcp_server.engine.policies.decision_lock import DecisionLock
from mcp_server.engine.policies.intent_inference import ProactiveIntentInference
from mcp_server.engine.policies.live_style import LiveStyleEnforcement
from mcp_server.engine.policies.relevance_inject import RelevanceInject
from mcp_server.engine.policies.scope_contract import ProactiveScopeContractLock
from mcp_server.engine.policies.token_budget import TokenBudgetPersist

__all__ = [
    "AIPromotionScore",
    "AntiRegression",
    "BlastRadiusVeto",
    "DecisionLock",
    "LiveStyleEnforcement",
    "ProactiveIntentInference",
    "ProactiveScopeContractLock",
    "RelevanceInject",
    "TokenBudgetPersist",
]
