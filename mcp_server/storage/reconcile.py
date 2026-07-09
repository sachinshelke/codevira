"""
reconcile.py — shared decision-similarity core (v3.7.0, Phase 29).

The Jaccard / overlap-coefficient tokenizer + thresholds used to live inside
``tools/check_conflict``; ``consensus_store`` imported them from there. v3.7.0
needs the SAME classification in two more places — supersede-on-write
(Phase 30) and the cross-engineer Tier-1 semantic reconcile (Phase 25) — so
the primitives move here as the single source of truth. ``check_conflict`` and
``consensus_store`` now import from this module.

Everything here is deterministic (pure lexical set math). LLM arbitration, if
used later, only *classifies/suggests* on top of this — the committed result
stays a deterministic function of the text, preserving convergence.
"""

from __future__ import annotations

import re
from typing import Any

# Tunable thresholds. Duplicate path stays conservative; conflict path adds the
# asymmetric-overlap detector with its own thresholds.
_DUP_THRESHOLD = 0.60  # symmetric Jaccard ≥ 0.60 → duplicate
_CONFLICT_OVERLAP_THRESHOLD = 0.60  # asymmetric overlap ≥ 0.60 → conflict
_CONFLICT_MIN_SHARED_TOKENS = 3  # floor on |A∩B| to avoid 1- or 2-token noise

KIND_DUPLICATE = "duplicate"
KIND_CONFLICT = "conflict"
KIND_DISTINCT = "distinct"

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
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    """Asymmetric overlap coefficient: |A∩B| / min(|A|, |B|) ∈ [0, 1].

    Catches the contradiction shape where a terse new decision shares most of
    its tokens with a longer protected decision — Jaccard misses this because
    the longer decision's extra context tokens dilute the symmetric union.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def classify(a_text: str, b_text: str, *, b_protected: bool = False) -> dict[str, Any]:
    """Classify a pair of decision texts as duplicate / conflict / distinct.

    ``b_protected`` gates the asymmetric-conflict path to protected decisions
    only (matches consensus_store's contradiction semantics — a terse new
    decision overlapping a protected one is a conflict to surface, not a dup).

    Returns ``{kind, similarity, jaccard, overlap, shared}`` — pure and
    deterministic.
    """
    a, b = _tokenize(a_text), _tokenize(b_text)
    jac = _jaccard(a, b)
    ov = _overlap_coefficient(a, b)
    shared = len(a & b)
    sim = max(jac, ov)
    if jac >= _DUP_THRESHOLD:
        kind = KIND_DUPLICATE
    elif (
        ov >= _CONFLICT_OVERLAP_THRESHOLD
        and shared >= _CONFLICT_MIN_SHARED_TOKENS
        and jac < _DUP_THRESHOLD
        and b_protected
    ):
        kind = KIND_CONFLICT
    else:
        kind = KIND_DISTINCT
    return {
        "kind": kind,
        "similarity": sim,
        "jaccard": jac,
        "overlap": ov,
        "shared": shared,
    }


def reconcile_candidate(
    text: str,
    corpus: list[dict[str, Any]],
    *,
    id_field: str = "id",
    text_field: str = "decision",
    protected_field: str = "do_not_revert",
) -> dict[str, Any]:
    """Classify ``text`` against every decision in ``corpus``.

    Returns ``{"duplicates": [...], "conflicts": [...]}`` — each entry carries
    the corpus record's id/text plus the classification. Deterministically
    ordered (similarity desc, then id) so callers converge. Used by
    supersede-on-write (pick the best duplicate to supersede) and the Tier-1
    reconcile (cluster near-duplicates / escalate conflicts).
    """
    dups: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for rec in corpus:
        if not isinstance(rec, dict):
            continue
        c = classify(
            text,
            str(rec.get(text_field) or ""),
            b_protected=bool(rec.get(protected_field)),
        )
        if c["kind"] == KIND_DISTINCT:
            continue
        entry = {
            "id": rec.get(id_field),
            "decision": rec.get(text_field),
            "do_not_revert": bool(rec.get(protected_field)),
            "similarity": round(c["similarity"], 4),
            "jaccard": round(c["jaccard"], 4),
            "overlap": round(c["overlap"], 4),
        }
        if c["kind"] == KIND_DUPLICATE:
            dups.append(entry)
        else:
            conflicts.append(entry)

    def _rank(e: dict[str, Any]) -> tuple[float, str]:
        return (-e["similarity"], str(e["id"] or ""))

    dups.sort(key=_rank)
    conflicts.sort(key=_rank)
    return {"duplicates": dups, "conflicts": conflicts}
