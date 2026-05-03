"""
mcp_server.engine.policies — built-in policy plugins.

Each Hero registers one (or more) ``Policy`` subclasses here. The engine
auto-discovers them via ``register_default_policies()`` (see
``mcp_server/engine/__init__.py``).

Heroes that have shipped:
  - Hero 4: blast_radius.BlastRadiusVeto

Heroes still scaffolded but not implemented:
  - Hero 1: decision_lock.* (Week 5)
  - Hero 5: cross_session.* (Week 6)
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

__all__ = ["BlastRadiusVeto"]
