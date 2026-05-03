"""
mcp_server.engine.policies — built-in policy plugins.

Each Hero registers one (or more) ``Policy`` subclasses here. The engine
auto-discovers them via ``register_default_policies()`` (see
``mcp_server/engine/__init__.py``).

Heroes that have shipped:
  - Hero 4: blast_radius.BlastRadiusVeto (Week 4)
  - Hero 1: decision_lock.DecisionLock (Week 5)
  - Hero 5: cross_session.CrossSessionConsistency (Week 6)

Heroes still scaffolded but not implemented:
  - Hero 6: token_budget.* (Week 7)
  - Hero 2: anti_regression.* (Week 8)
  - Hero 7: live_style.* (Week 9)
  - Hero 10: ai_promotion.* (Week 10)
  - Hero 9: intent_inference.* (Week 11)
  - Hero 3: scope_contract.* (Week 12)
  - Hero 8: decision_replay.* (Week 13)
"""
from __future__ import annotations

from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
from mcp_server.engine.policies.cross_session import CrossSessionConsistency
from mcp_server.engine.policies.decision_lock import DecisionLock

__all__ = ["BlastRadiusVeto", "CrossSessionConsistency", "DecisionLock"]
