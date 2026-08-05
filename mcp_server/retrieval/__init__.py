"""
retrieval — shared ranking primitives (4.0 Step 6).

Codevira grew four independent scoring implementations. Two were removed
in the 4.0 subtraction (``eval/composite.py`` with the weight tuner, and
the reflections path). Three remain, on three different scales:

  * ``decisions_store.search``            raw FTS5 BM25 — unbounded, negative
  * ``relevance_inject._score_candidates`` unnormalized sum × outcome weight
  * ``skills_store.search``               composite in [0, 1]

That divergence is not cosmetic. ``learned_weights.py`` documented a
scorer drifting from the one it mirrored, and a raw BM25 figure cannot be
compared across two queries, let alone across two surfaces.

This module holds the primitives all three should agree on. It is
DELIBERATELY primitives-only, not a unified scorer:

``relevance_inject`` consumes ``decisions_store.search`` as its FTS input
and then applies its own rank falloff. Collapsing the two would
double-count the tag and file signals at a rank position that itself
moved. Worse, the cross-tool wedge that D000010 protects currently
survives on an EXACT floating-point tie — a single-FTS-match decision
scores 0.100 against a 0.100 floor compared with ``<``. Any arithmetic
change there is a coin flip on a load-bearing behaviour, and the honest
gate for it is the labelled action-keyed corpus that does not exist yet.

So: share the definitions now, unify the arithmetic when there is a
measurement that can tell success from regression.
"""

from __future__ import annotations

from mcp_server.retrieval.score import (
    outcome_weight,
    rank_norm,
    recency_decay,
    tag_jaccard,
)

__all__ = ["rank_norm", "tag_jaccard", "recency_decay", "outcome_weight"]
