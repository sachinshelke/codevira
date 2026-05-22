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

    Atomic via write-to-tmp + rename (so a concurrent reader never sees
    a half-written digest).
    """
    import os
    import tempfile

    decisions = jsonl_store.read_all(decisions_path)
    digest_records: list[dict[str, Any]] = []
    for d in decisions:
        if exclude_superseded and d.get("is_superseded"):
            continue
        if exclude_superseded and d.get("superseded_by"):
            continue
        digest_records.append(digest_record(d))

    # v3.0.0 (2026-05-22 round-2): per-write UNIQUE tmp filename so two
    # concurrent regenerate() calls don't race on the rename target.
    # Pre-fix the tmp was a fixed ``<digest_path>.tmp`` — thread A's
    # tmp got consumed by its own replace(), thread B's later replace()
    # raised FileNotFoundError. Caught by the 50-thread record_decision
    # smoke test (which triggers regenerate via _sync_agents_md_best_effort).
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{digest_path.name}.",
        suffix=".tmp",
        dir=str(digest_path.parent),
    )
    os.close(fd)  # close mkstemp's fd; append_many opens its own
    tmp_path = Path(tmp_name)
    try:
        if digest_records:
            # append_many provides the lock + fsync semantics; we just
            # need it on the unique tmp file.
            jsonl_store.append_many(tmp_path, digest_records)
        # If no records, the empty mkstemp file is already valid.
        os.replace(tmp_name, digest_path)
        tmp_name = None  # ownership transferred
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return len(digest_records)
