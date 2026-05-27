"""
digest.py — slim per-decision records for cheap relevance lookup.

The full ``decisions.jsonl`` carries ~200-500 tokens per record. The
``digest.jsonl`` carries ~50 tokens per record: id, summary, tags,
file_path, do_not_revert, outcome-weight. That's what the relevance
hook loads to score and rank candidate decisions for injection.

Generation rules (deterministic, idempotent):

- ``summary``: first 80 chars of ``decision``, word-boundary trimmed.
  We reuse the existing pattern from v2.1.2 Item 7
  (indexer/sqlite_graph.py::record_decision summary derivation).
- ``weight``: outcome-weighted score in [0.0, 1.0]
    - kept (decision survived a git observation):  1.0
    - modified (file changed but not reverted):     0.6
    - reverted (decision rolled back):              0.2
    - archived (>90 days, no outcome events):       0.0
    - no outcome yet:                               0.5 (neutral)
- Decisions marked ``is_superseded`` are EXCLUDED from digest by
  default (the new replacement decision is in there instead).

Used by:
- ``relevance_inject.py`` to score + rank candidate decisions
- ``manifest.py`` (indirectly — both files are regenerated together)
- ``agents_md_generator.py`` to know which decisions are do_not_revert
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_server.storage import jsonl_store

# Outcome → weight mapping. Tuned by hand; revisit after outcome tracker
# (Phase F) lands so we can ground these in real data.
_OUTCOME_WEIGHTS: dict[str, float] = {
    "kept": 1.0,
    "modified": 0.6,
    "reverted": 0.2,
    "archived": 0.0,
}
_DEFAULT_WEIGHT = 0.5  # no outcome events yet

# Summary length: matches v2.1.2 Item 7 to keep round-trip behaviour
# consistent for users upgrading.
_SUMMARY_MAX_CHARS = 80


def make_summary(decision_text: str) -> str:
    """Derive a slim summary from a decision's full text.

    Mirrors v2.1.2 Item 7 (indexer/sqlite_graph.py: word-boundary trim
    to ~80 chars). Returns text unchanged if shorter.
    """
    if not decision_text:
        return ""
    text = decision_text.strip().replace("\n", " ").replace("\r", "")
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    cut = text[:_SUMMARY_MAX_CHARS]
    last_space = cut.rfind(" ")
    if last_space > _SUMMARY_MAX_CHARS // 2:
        cut = cut[:last_space]
    return cut.rstrip(" .,;:") + "…"


def weight_for_outcome(outcome: str | None) -> float:
    """Map a decision's outcome_type to a relevance weight."""
    if outcome is None:
        return _DEFAULT_WEIGHT
    return _OUTCOME_WEIGHTS.get(outcome, _DEFAULT_WEIGHT)


def digest_record(decision: dict[str, Any]) -> dict[str, Any]:
    """Project a full decision record to its digest shape.

    Input shape (from decisions.jsonl):
        {id, ts, session_id, file_path, decision, context, do_not_revert,
         tags, supersedes, superseded_by, outcome, ...}

    Output shape:
        {id, summary, tags, file, do_not_revert, weight}
    """
    return {
        "id": decision.get("id"),
        "summary": make_summary(decision.get("decision", "")),
        "tags": list(decision.get("tags") or []),
        "file": decision.get("file_path"),
        "do_not_revert": bool(decision.get("do_not_revert", False)),
        "weight": weight_for_outcome(decision.get("outcome")),
    }


def regenerate(
    decisions_path: Path,
    digest_path: Path,
    *,
    exclude_superseded: bool = True,
) -> int:
    """Rewrite ``digest_path`` from ``decisions_path``.

    Idempotent. Safe to call any time. Returns the number of digest
    records written.

    v3.0.0 round-3: serializes the digest to a single JSONL string
    in memory, then writes it via ``atomic.atomic_write_text`` for
    crash-safety + concurrent-rename safety. The string-then-write
    approach is fine here because the digest is bounded by the
    decision count (≤ a few hundred records typically); no streaming
    pressure.
    """
    import json

    from mcp_server.storage import atomic

    decisions = jsonl_store.read_all(decisions_path)
    digest_records: list[dict[str, Any]] = []
    for d in decisions:
        if exclude_superseded and d.get("is_superseded"):
            continue
        if exclude_superseded and d.get("superseded_by"):
            continue
        digest_records.append(digest_record(d))

    # Serialize as JSONL (one record per line; UTF-8 preserved).
    lines = [
        json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        for rec in digest_records
    ]
    payload = "\n".join(lines) + ("\n" if lines else "")
    atomic.atomic_write_text(digest_path, payload)
    return len(digest_records)
