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

import hashlib
import json
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


# ─── canonical pick (Phase 29, second clause) ─────────────────────────────
#
# Consumed by the Tier-1 cross-engineer reconcile (Phase 25). Kept here rather
# than in either caller because BOTH sides of a two-machine merge must compute
# the identical answer, and a rule that lives in one caller drifts.
#
# The order deliberately mirrors ``id_repair._order_key``: a cluster is merged
# independently on every machine that holds it, so a tie broken by list
# position makes two engineers converge on different files. That module's order
# was found to be non-total this release (a stable sort was silently deciding
# winners); this one carries the content hash from the start.

_CANON_SENTINEL = "\uffff"  # sorts after any real ISO timestamp


def _canon_hash(record: dict[str, Any], *, id_field: str) -> str:
    """Content identity, excluding the id so it survives renumbering."""
    body = {k: v for k, v in record.items() if k != id_field}
    return hashlib.sha1(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _writer(record: dict[str, Any]) -> str:
    origin = record.get("origin")
    if not isinstance(origin, dict):
        return ""
    return str(origin.get("device_id") or origin.get("host_hash") or "")


def pick_canonical(
    cluster: list[dict[str, Any]],
    *,
    id_field: str = "id",
    text_field: str = "decision",
    protected_field: str = "do_not_revert",
) -> dict[str, Any]:
    """Choose ONE canonical record for a duplicate cluster, deterministically.

    Returns ``{canonical, canonical_id, aliases, tags, provenance, protected,
    ambiguous}``. ``aliases`` maps every merged-away id to the canonical one so
    ``[[Dxxxx]]`` references still resolve after the merge.

    Winner order — total, and every component a pure function of the record:

    1. **protected first.** Merging a ``do_not_revert`` record into an
       unprotected one would silently drop the protection, so a lock always
       outranks a plain decision regardless of age.
    2. **earliest ``ts``.** The oldest id is the one other decisions are most
       likely to reference, so keeping it minimises alias churn.
    3. **writer id**, then **content hash** — the components that make the
       order strict when ts and protection collide.

    The canonical TEXT is always one of the inputs. An LLM may suggest a merged
    wording upstream, but committing synthesised text would make the result a
    function of sampling instead of content and convergence would be gone.

    ``ambiguous`` is set when two or more PROTECTED members disagree on text.
    That is a real conflict, not a duplicate: the pick stays deterministic so
    the run is reproducible, but the caller must escalate rather than merge.
    """
    records = [r for r in cluster if isinstance(r, dict)]
    if not records:
        return {
            "canonical": None,
            "canonical_id": None,
            "aliases": {},
            "tags": [],
            "provenance": [],
            "protected": False,
            "ambiguous": False,
        }

    def _key(r: dict[str, Any]) -> tuple[int, str, str, str, str]:
        return (
            0 if r.get(protected_field) else 1,
            str(r.get("ts") or "") or _CANON_SENTINEL,
            _writer(r),
            _canon_hash(r, id_field=id_field),
            # The id is the LAST resort and it is what makes the order total.
            # _canon_hash excludes the id, so records differing only by id hash
            # identically and tie on every other component — and `sorted` is
            # stable, so input order would decide. That is the exact defect
            # found in id_repair this release; a permutation test caught it
            # here before it shipped.
            str(r.get(id_field) or ""),
        )

    ordered = sorted(records, key=_key)
    winner = ordered[0]
    winner_id = str(winner.get(id_field) or "")

    protected_texts = {
        str(r.get(text_field) or "") for r in records if r.get(protected_field)
    }

    tags: set[str] = set()
    provenance: list[dict[str, Any]] = []
    seen_origins: set[str] = set()
    for r in ordered:
        for tag in r.get("tags") or []:
            tags.add(str(tag))
        origin = r.get("origin")
        if isinstance(origin, dict):
            fp = json.dumps(origin, sort_keys=True, default=str)
            if fp not in seen_origins:
                seen_origins.add(fp)
                provenance.append(origin)

    return {
        "canonical": winner,
        "canonical_id": winner_id,
        "aliases": {
            str(r.get(id_field)): winner_id
            for r in ordered[1:]
            if r.get(id_field) and str(r.get(id_field)) != winner_id
        },
        "tags": sorted(tags),
        "provenance": provenance,
        "protected": bool(winner.get(protected_field)),
        "ambiguous": len(protected_texts) > 1,
    }
