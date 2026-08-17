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
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Tunable thresholds. Duplicate path stays conservative; conflict path adds the
# asymmetric-overlap detector with its own thresholds.
_DUP_THRESHOLD = 0.60  # symmetric Jaccard ≥ 0.60 → duplicate
_CONFLICT_OVERLAP_THRESHOLD = 0.60  # asymmetric overlap ≥ 0.60 → conflict
_CONFLICT_MIN_SHARED_TOKENS = 3  # floor on |A∩B| to avoid 1- or 2-token noise

#: A decision and its negation differ by ONE token, so pure set math scores
#: them a near-perfect duplicate — "never switch away from pnpm" vs "switch
#: away from pnpm" is Jaccard 0.83, comfortably over _DUP_THRESHOLD. That is
#: the worst false positive available here: Tier-1 would alias one meaning
#: away, and supersede-on-write would RETIRE "never do X" as a duplicate of
#: "do X". A negation present on one side and absent on the other is therefore
#: a conflict by construction, whatever the similarity says. Still fully
#: deterministic: a set difference against a fixed list, no model involved.
_NEGATIONS = frozenset(
    {
        "never",
        "not",
        "no",
        "none",
        "dont",
        "doesnt",
        "didnt",
        "wont",
        "cannot",
        "cant",
        "avoid",
        "stop",
        "without",
        "disallow",
        "forbid",
        "prohibit",
        "refuse",
        "disable",
        "deprecated",
        "remove",
    }
)

KIND_DUPLICATE = "duplicate"
KIND_CONFLICT = "conflict"
KIND_DISTINCT = "distinct"


def negation_disagrees(a_tokens: set[str], b_tokens: set[str]) -> bool:
    """True when exactly ONE side of a pair carries a negation.

    ``"never switch away from pnpm"`` and the same sentence without ``never``
    differ by a single token, so set math scores them Jaccard 0.83 — over the
    duplicate threshold and over the auto-supersede bar. Treating that as a
    re-record retires a decision in favour of its own opposite.

    Symmetric difference, so two texts that BOTH negate stay duplicates: the
    rule keys on *disagreement*, not on the mere presence of a negation.

    PUBLIC, and the reason it is: ``classify`` is not the only classifier.
    ``tools/check_conflict`` re-implements the duplicate/conflict decision
    inline from the shared primitives, and that is the path
    ``record_decision`` -> supersede-on-write actually takes. When the guard
    lived only inside ``classify`` it was dead code on the write path — every
    test that drove ``classify`` directly passed while real writes still
    superseded negations. One definition, both callers.
    """
    return bool((a_tokens ^ b_tokens) & _NEGATIONS)


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
    disagrees = negation_disagrees(a, b)
    if disagrees and (jac >= _DUP_THRESHOLD or ov >= _CONFLICT_OVERLAP_THRESHOLD):
        kind = KIND_CONFLICT
    elif jac >= _DUP_THRESHOLD:
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


# ─── Tier-1: cluster a whole store (Phase 25) ─────────────────────────────
#
# Tier-0 (``id_repair``) resolves records that collide on an ID. Tier-1
# resolves records that say the same THING in different words — two engineers
# recording the same decision independently, each with its own id.
#
# Governing rule from the phase: the structural tier is AUTHORITATIVE, this one
# is ASSISTIVE. It may merge provably-safe duplicates; it must never
# auto-resolve a contradiction. Conflicts come back as data for the caller to
# surface — never a silent pick.
#
# Escalation returns data rather than writing anywhere. The phase specified
# routing conflicts through ``consensus_store``, which was removed in 4.0, and
# a pure function that hands back what it found is the better fit regardless:
# it keeps this layer free of I/O and lets the caller choose the surface.

#: Pairwise scan is O(n^2). Bounded so a large store cannot stall a caller —
#: and the bound is REPORTED (``truncated``), because a truncated result that
#: looks complete is worse than a slow one.
_DEFAULT_MAX_RECORDS = 2000


def _cites_each_other(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    id_field: str,
    text_field: str,
) -> bool:
    """True when either record's text NAMES the other's id.

    A decision that cites another is referencing it — amending, honouring,
    superseding — not accidentally saying the same thing and not contradicting
    it. Citation is the cheapest, most reliable "these are deliberately
    related" signal available, and it is purely lexical, so the classifier
    stays deterministic.

    The case that motivated it, from this repo's own store on the first real
    run of ``codevira reconcile``: D0000NJ deferred per-prompt preference
    injection; D0000QI says *"Phase 10 resolved by HONORING the D0000NJ
    deferral"*. They AGREE — one explicitly upholds the other. Set math saw a
    shared topic and ``not`` present on only one side, and reported a
    contradiction at overlap 0.62.

    Matched on a WORD BOUNDARY, not a substring: ``D1`` must not match inside
    ``D100`` or ``D1000``, or the guard would silently suppress real findings
    as ids grow longer — a false negative in the classifier that exists to
    prevent false positives.
    """
    ids = (str(a.get(id_field) or ""), str(b.get(id_field) or ""))
    texts = (str(a.get(text_field) or ""), str(b.get(text_field) or ""))
    for rid, other_text in ((ids[0], texts[1]), (ids[1], texts[0])):
        if not rid:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(rid)}(?![A-Za-z0-9])", other_text):
            return True
    return False


def cluster_store(
    records: list[dict[str, Any]],
    *,
    id_field: str = "id",
    text_field: str = "decision",
    protected_field: str = "do_not_revert",
    max_records: int = _DEFAULT_MAX_RECORDS,
) -> dict[str, Any]:
    """Group a store into duplicate clusters and surfaced conflicts.

    Returns ``{merges, conflicts, scanned, truncated}``. Each merge carries the
    ``pick_canonical`` result plus the member ids; each conflict carries the
    id pair and its similarity, and is never merged.

    Deterministic and order-free: records are sorted by the same total key
    ``pick_canonical`` uses before any pairing, and clusters are grouped by
    union-find, so the output does not depend on the order they arrived in.
    Two engineers running this over the same store get the same plan.

    Duplicate grouping is TRANSITIVE. If A duplicates B and B duplicates C,
    all three land in one cluster even when A and C alone would not classify
    as duplicates — otherwise the same decision survives twice under two
    canonical ids, which is the outcome this exists to prevent.
    """
    valid = [r for r in records if isinstance(r, dict)]
    ordered = sorted(valid, key=lambda r: (str(r.get(id_field) or ""),))
    truncated = len(ordered) > max_records
    if truncated:
        logger.warning(
            "reconcile.cluster_store: %d records exceeds the %d scan cap; "
            "clustering the first %d by id. Raise max_records to cover the rest.",
            len(ordered),
            max_records,
            max_records,
        )
        ordered = ordered[:max_records]

    parent: dict[int, int] = {i: i for i in range(len(ordered))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    conflicts: list[dict[str, Any]] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if _cites_each_other(a, b, id_field=id_field, text_field=text_field):
                # Deliberately related, so neither merge nor conflict. See
                # _cites_each_other for the case that motivated this.
                continue
            c = classify(
                str(a.get(text_field) or ""),
                str(b.get(text_field) or ""),
                b_protected=bool(a.get(protected_field) or b.get(protected_field)),
            )
            if c["kind"] == KIND_DUPLICATE:
                union(i, j)
            elif c["kind"] == KIND_CONFLICT:
                conflicts.append(
                    {
                        "ids": sorted(
                            [str(a.get(id_field) or ""), str(b.get(id_field) or "")]
                        ),
                        "similarity": round(c["similarity"], 4),
                        "overlap": round(c["overlap"], 4),
                    }
                )

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, rec in enumerate(ordered):
        groups.setdefault(find(i), []).append(rec)

    merges: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        picked = pick_canonical(
            members,
            id_field=id_field,
            text_field=text_field,
            protected_field=protected_field,
        )
        picked["members"] = sorted(str(m.get(id_field) or "") for m in members)
        merges.append(picked)

    merges.sort(key=lambda m: str(m["canonical_id"] or ""))
    conflicts.sort(key=lambda c: (c["ids"][0], c["ids"][1]))
    return {
        "merges": merges,
        "conflicts": conflicts,
        "scanned": len(ordered),
        "truncated": truncated,
    }
