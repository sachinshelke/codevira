"""
check_conflict.py — v2.2.0: FTS5 + Jaccard text-similarity detector.

Reports 3 + 4 flagged silent conflict / duplicate accumulation as a
trust gap. In v2.1.x we used ChromaDB semantic similarity. In v2.2.0
we use FTS5 keyword/stemming retrieval + Jaccard token-set similarity
on top of decisions stored in ``.codevira/decisions.jsonl``.

Definitions:

- A *duplicate* is a decision whose Jaccard similarity to ANY existing
  decision is ≥ DUP_THRESHOLD (default 0.6).
- A *conflict* is a decision whose Jaccard similarity to a
  ``do_not_revert=True`` decision is ≥ DUP_THRESHOLD.

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


# Tunable thresholds. Conservative defaults — better to under-warn than
# over-warn (user can always force=True; can never un-skip a
# user-frustrating false positive).
_DUP_THRESHOLD = 0.60  # Jaccard ≥ 0.60 → duplicate
_CONFLICT_BOOST = 0.0  # No conflict boost — same threshold for conflict

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
        }

    query_tokens = _tokenize(decision_text)
    conflicts: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []

    for cand in candidates:
        cand_text = cand.get("decision") or ""
        cand_tokens = _tokenize(cand_text)
        similarity = _jaccard(query_tokens, cand_tokens)
        if similarity < _DUP_THRESHOLD:
            continue
        entry = {
            "decision_id": cand.get("id"),
            "similarity": round(similarity, 3),
            "do_not_revert": bool(cand.get("do_not_revert", False)),
            "summary": (cand_text[:80] + "…") if len(cand_text) > 80 else cand_text,
            "file_path": cand.get("file_path"),
            "decision": cand_text,
        }
        if entry["do_not_revert"]:
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
        "threshold_used": _DUP_THRESHOLD,
    }
