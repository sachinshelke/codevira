"""
check_conflict.py — v2.2.0: FTS5 + Jaccard text-similarity detector.

Reports 3 + 4 flagged silent conflict / duplicate accumulation as a
trust gap. In v2.1.x we used ChromaDB semantic similarity. In v2.2.0
we use FTS5 keyword/stemming retrieval + Jaccard token-set similarity
on top of decisions stored in ``.codevira/decisions.jsonl``.

Definitions:

- A *duplicate* is a decision whose **symmetric Jaccard** similarity
  to ANY existing decision is ≥ DUP_THRESHOLD (default 0.60).
  Symmetric is the right shape for re-records: the two decisions are
  saying roughly the same thing in similar amounts of text.

- A *conflict* (v3.0.0 round-3 expansion) is a decision that either:
  (a) is a duplicate of a ``do_not_revert=True`` existing decision, OR
  (b) has **asymmetric overlap** with a ``do_not_revert=True``
      decision: at least CONFLICT_OVERLAP_THRESHOLD (0.60) of the
      smaller token set's tokens are shared, with at least
      CONFLICT_MIN_SHARED_TOKENS (3) shared tokens, AND symmetric
      Jaccard is below DUP_THRESHOLD (so we only flag the contradiction
      shape, not re-affirmations).

Why the asymmetric path: pure Jaccard misses the common contradiction
shape where a TERSE new decision ("Switch from pnpm to npm" — 4
content tokens) overlaps with a LONGER protected decision ("AgentStore
uses pnpm workspaces — DO NOT switch package manager" — 8 content
tokens). Their intersection is 3 tokens (agentstore, pnpm, switch);
Jaccard is 3/9 = 0.333 (below 0.60, miss); overlap coefficient is
3/min(4,8) = 0.75 (fires at 0.60). This is exactly the shape that
caught the AgentStore system test in
``scripts/system_test_agentstore.py::A9``.

The Jaccard-below-0.60 guard in the asymmetric path is the
re-affirmation filter: if the new decision is essentially saying the
same thing as the protected one (high symmetric similarity), it's a
duplicate, not a conflict — agents re-record protected decisions
defensively all the time; we shouldn't treat that as a contradiction.

Why FTS5 + Jaccard, not embeddings:

- Decisions are short text; FTS5 BM25 is fast + recall-strong for
  keyword overlap.
- Jaccard catches near-duplicates with shared vocabulary even when
  worded differently ("Use bcrypt for hashing" vs "Hash passwords
  with bcrypt").
- No torch/chromadb runtime cost; sub-50ms per check on 1000-decision
  corpus.

If semantic infra ever returns to codevira (it shouldn't in v2.2.x),
this module is the integration point — swap the Jaccard scorer with
a vector-cosine call and the surface contract stays identical.
"""

from __future__ import annotations

import re
from typing import Any


# Tunable thresholds. Duplicate path stays conservative; conflict path
# (against do_not_revert decisions only) adds the asymmetric-overlap
# detector with its own thresholds.
_DUP_THRESHOLD = 0.60  # symmetric Jaccard ≥ 0.60 → duplicate
_CONFLICT_OVERLAP_THRESHOLD = 0.60  # asymmetric overlap ≥ 0.60 → conflict
_CONFLICT_MIN_SHARED_TOKENS = 3  # floor on |A∩B| to avoid 1- or 2-token noise

# Stop-word list for tokenization (English + common code words).
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "from",
        "for",
        "by",
        "with",
        "as",
        "it",
        "this",
        "that",
        "these",
        "those",
        "we",
        "you",
        "i",
        "they",
        "should",
        "must",
        "may",
        "can",
        "will",
        "would",
        "do",
        "does",
        "did",
        "use",
        "using",
        "used",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip stop-words, return token set."""
    return {
        tok.lower()
        for tok in _TOKEN_RE.findall(text or "")
        if tok.lower() not in _STOPWORDS and len(tok) >= 3
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity: |A∩B| / |A∪B| ∈ [0, 1]."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    """Asymmetric overlap coefficient: |A∩B| / min(|A|, |B|) ∈ [0, 1].

    Catches the contradiction shape where a terse new decision shares
    most of its tokens with a longer protected decision. Pure Jaccard
    misses this because the protected decision's extra context tokens
    dilute the symmetric union.

    Example (v3.0.0 round-3 system-test finding):
      A = "AgentStore should switch from pnpm to npm" — 4 content tokens
      B = "AgentStore uses pnpm workspaces — DO NOT switch package manager"
          — 8 content tokens
      |A∩B| = 3 (agentstore, pnpm, switch)
      Jaccard = 3/9 = 0.333  (below DUP threshold — miss)
      Overlap = 3/min(4,8) = 0.75  (fires at CONFLICT_OVERLAP threshold)
    """
    if not a or not b:
        return 0.0
    inter = a & b
    return len(inter) / min(len(a), len(b))


def check_conflict(
    decision_text: str,
    file_path: str | None = None,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Check whether ``decision_text`` is a near-duplicate of, or
    contradicts, any existing decision in this project.

    Returns:
        {
          "status": "novel" | "duplicate" | "conflict" | "error",
          "conflicts": [{decision_id, similarity, do_not_revert,
                          summary, file_path, decision}, ...],
          "duplicates": [{decision_id, similarity, do_not_revert,
                          summary, file_path, decision}, ...],
          "threshold_used": float,
        }

    "Conflict" overrides "duplicate" — if any hit is do_not_revert=True
    the overall status is "conflict" regardless.
    """
    if not decision_text or not isinstance(decision_text, str):
        return {
            "status": "error",
            "error": "decision_text must be a non-empty string",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": None,
        }

    from mcp_server.storage import decisions_store

    # Use FTS5 to narrow the candidate pool (no need to Jaccard-score
    # every decision; only the top-K most-keyword-relevant). This keeps
    # the check fast on large projects.
    candidates = decisions_store.search(decision_text, limit=limit * 2)

    if not candidates:
        return {
            "status": "novel",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": _DUP_THRESHOLD,
            "thresholds": {
                "duplicate_jaccard": _DUP_THRESHOLD,
                "conflict_overlap": _CONFLICT_OVERLAP_THRESHOLD,
                "conflict_min_shared_tokens": _CONFLICT_MIN_SHARED_TOKENS,
            },
        }

    query_tokens = _tokenize(decision_text)
    conflicts: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []

    for cand in candidates:
        cand_text = cand.get("decision") or ""
        cand_tokens = _tokenize(cand_text)
        jaccard = _jaccard(query_tokens, cand_tokens)
        overlap = _overlap_coefficient(query_tokens, cand_tokens)
        shared = len(query_tokens & cand_tokens)
        is_protected = bool(cand.get("do_not_revert", False))

        # Duplicate path: high SYMMETRIC similarity (re-record shape).
        # Conflict path: against do_not_revert AND either symmetric
        # high similarity OR ASYMMETRIC overlap (the contradiction
        # shape — terse new decision shares core tokens with a longer
        # protected decision). The Jaccard-below-DUP guard on the
        # asymmetric branch is the re-affirmation filter.
        is_duplicate = jaccard >= _DUP_THRESHOLD
        is_asymmetric_conflict = (
            is_protected
            and overlap >= _CONFLICT_OVERLAP_THRESHOLD
            and shared >= _CONFLICT_MIN_SHARED_TOKENS
            and jaccard < _DUP_THRESHOLD
        )
        if not (is_duplicate or is_asymmetric_conflict):
            continue

        # Report the stronger of the two scores so the agent can see
        # both regimes (symmetric high vs asymmetric high).
        display_similarity = max(jaccard, overlap)
        entry = {
            "decision_id": cand.get("id"),
            "similarity": round(display_similarity, 3),
            "jaccard": round(jaccard, 3),
            "overlap_coefficient": round(overlap, 3),
            "shared_tokens": shared,
            "match_shape": "duplicate" if is_duplicate else "asymmetric-conflict",
            "do_not_revert": is_protected,
            "summary": (cand_text[:80] + "…") if len(cand_text) > 80 else cand_text,
            "file_path": cand.get("file_path"),
            "decision": cand_text,
        }
        if is_protected:
            conflicts.append(entry)
        else:
            duplicates.append(entry)

    # Cap at limit so a runaway false-positive set doesn't explode the response.
    conflicts = conflicts[:limit]
    duplicates = duplicates[:limit]

    if conflicts:
        status = "conflict"
    elif duplicates:
        status = "duplicate"
    else:
        status = "novel"

    return {
        "status": status,
        "conflicts": conflicts,
        "duplicates": duplicates,
        # ``threshold_used`` is kept for v2.x back-compat. New callers
        # should read ``thresholds`` (dict) which surfaces both regimes.
        "threshold_used": _DUP_THRESHOLD,
        "thresholds": {
            "duplicate_jaccard": _DUP_THRESHOLD,
            "conflict_overlap": _CONFLICT_OVERLAP_THRESHOLD,
            "conflict_min_shared_tokens": _CONFLICT_MIN_SHARED_TOKENS,
        },
    }
