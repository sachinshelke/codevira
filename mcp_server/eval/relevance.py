"""Read-side relevance eval — E3 (Phase 21). The intelligent harness.

Codevira's leverage is the READ side (D00005N): does ``search_decisions`` /
``get_session_context`` surface the RIGHT memory at the right moment? Its
documented failure mode is *signal-to-noise* — relevant decisions buried
under tangential ones. This module measures exactly that, and it maintains
ITSELF: every eval case is derived from the project's own accumulated
``.codevira/decisions.jsonl`` — there is no hand-written fixture list to rot
as the system evolves.

For each decision we synthesize an INTENT-style query (what an agent working
in that area would actually type — file name + tags + a couple of salient
topic words, NOT the decision's verbatim text), run the real retrieval the
agents use, and score:

* **recall@k** — is the target decision in the top-k?
* **MRR**      — how high (1/rank)? buried-but-present is still poor.
* **precision@k / noise** — how many of the top-k are actually relevant?
  (scored by :mod:`mcp_server.eval.judge` — lexical by default, LLM-as-judge
  when a sampling-capable client is available.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "use",
        "uses",
        "used",
        "not",
        "any",
        "all",
        "are",
        "was",
        "via",
        "per",
        "its",
        "now",
        "new",
        "old",
        "must",
        "should",
        "into",
        "onto",
        "than",
        "then",
        "when",
        "decision",
        "decisions",
        "codevira",
        "phase",
        "fix",
        "add",
        "change",
    }
)
DEFAULT_K = 5
DEFAULT_MAX_CASES = 200


@dataclass(frozen=True)
class EvalCase:
    decision_id: str
    query: str
    source: dict[str, Any]  # the source decision (for precision judging)


@dataclass
class CaseResult:
    case: EvalCase
    hit: bool
    rank: int | None  # 1-based position of the target, None if absent
    returned_ids: list[str] = field(default_factory=list)
    relevant_in_topk: int | None = None  # filled by the judge
    k: int = DEFAULT_K


@dataclass
class RelevanceReport:
    n_cases: int
    k: int
    recall_at_k: float
    mrr: float
    mean_precision: float | None  # None if precision wasn't scored
    results: list[CaseResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_cases": self.n_cases,
            "k": self.k,
            "recall_at_k": round(self.recall_at_k, 4),
            "mrr": round(self.mrr, 4),
            "mean_precision": (
                round(self.mean_precision, 4)
                if self.mean_precision is not None
                else None
            ),
        }


def _salient(text: str, top: int = 3) -> list[str]:
    """The ``top`` most distinctive subject tokens of ``text`` (longest first
    — long/compound tokens carry more topic signal than short common words)."""
    seen: dict[str, None] = {}
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lower()
        if len(tok) >= 4 and tok not in _STOP:
            seen.setdefault(tok, None)
    return sorted(seen, key=len, reverse=True)[:top]


def derive_query(decision: dict[str, Any]) -> str | None:
    """Build an INTENT-style query from a decision's metadata.

    Uses the file basename + tags + a couple of salient topic words — the
    shape of what an agent in that area would search — deliberately NOT the
    decision's full text (that would make recall a trivial self-match).
    Returns ``None`` when there's no intent signal to query on.
    """
    terms: list[str] = []
    fp = decision.get("file_path")
    if isinstance(fp, str) and fp:
        stem = PurePosixPath(fp).stem
        terms += [t for t in re.split(r"[_\-.]", stem) if len(t) >= 3]
    for tag in decision.get("tags") or []:
        terms += [t for t in re.split(r"[_\-]", str(tag)) if len(t) >= 3]
    terms += _salient(decision.get("decision") or "", top=2)

    # De-dup preserving order; drop stop-words.
    out: list[str] = []
    for t in terms:
        tl = t.lower()
        if tl not in _STOP and tl not in out:
            out.append(tl)
    return " ".join(out) if out else None


def build_cases(
    decisions: list[dict[str, Any]], *, max_cases: int = DEFAULT_MAX_CASES
) -> list[EvalCase]:
    """Self-derived eval set: one case per decision that yields an intent query."""
    cases: list[EvalCase] = []
    for d in decisions:
        did = d.get("id")
        if not did:
            continue
        query = derive_query(d)
        if not query:
            continue
        cases.append(EvalCase(decision_id=str(did), query=query, source=d))
        if len(cases) >= max_cases:
            break
    return cases


def run_recall(cases: list[EvalCase], *, k: int = DEFAULT_K) -> list[CaseResult]:
    """Run each case's query through the REAL retrieval agents use
    (``decisions_store.search``) and record hit + rank + returned ids."""
    from mcp_server.storage import decisions_store

    results: list[CaseResult] = []
    for case in cases:
        try:
            hits = decisions_store.search(case.query, limit=k)
        except Exception:  # noqa: BLE001 — a bad query never breaks the sweep
            hits = []
        ids = [str(h.get("id")) for h in hits]
        rank = ids.index(case.decision_id) + 1 if case.decision_id in ids else None
        results.append(
            CaseResult(
                case=case,
                hit=rank is not None,
                rank=rank,
                returned_ids=ids,
                k=k,
            )
        )
    return results


def summarize(results: list[CaseResult], *, k: int) -> RelevanceReport:
    n = len(results)
    hits = sum(1 for r in results if r.hit)
    mrr = sum((1.0 / r.rank) for r in results if r.rank) / n if n else 0.0
    scored = [r.relevant_in_topk for r in results if r.relevant_in_topk is not None]
    mean_prec: float | None = None
    if scored:
        # precision per case = relevant_in_topk / min(k, returned)
        per_case = [
            r.relevant_in_topk / max(1, min(k, len(r.returned_ids)))
            for r in results
            if r.relevant_in_topk is not None and r.returned_ids
        ]
        mean_prec = sum(per_case) / len(per_case) if per_case else None
    return RelevanceReport(
        n_cases=n,
        k=k,
        recall_at_k=(hits / n) if n else 0.0,
        mrr=mrr,
        mean_precision=mean_prec,
        results=results,
    )
