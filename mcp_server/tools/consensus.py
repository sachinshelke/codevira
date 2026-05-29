"""
consensus.py — v3.1.0 M6 Phase B MCP tools for cross-IDE consensus.

Two read-only tools cover the agent-facing surface:

  - consensus_check  — run the cross-IDE conflict scan; materialize
                       new conflicts to pending_conflicts.jsonl;
                       advance this IDE's checkpoint.
  - consensus_status — return counts + top-3 pending conflicts (for
                       the get_session_context panel + interactive
                       queries).

Phase B does NOT write amendment rows on decisions. The handshake
protocol that lets one IDE supersede another IDE's protected decision
is M7 (opt-in, default off).
"""

from __future__ import annotations

from typing import Any

from mcp_server.storage import consensus_store


def consensus_check() -> dict[str, Any]:
    """Run the scan; return the summary dict produced by
    ``consensus_store.scan_and_materialize``."""
    return consensus_store.scan_and_materialize()


def consensus_status(*, top_k: int = 3) -> dict[str, Any]:
    """Return the count of pending conflicts + top-K rows for surface
    rendering."""
    pending = consensus_store.list_pending(limit=max(top_k, 1) * 4)
    return {
        "count": len(pending),
        "pending": [
            {
                "pending_conflict_id": r.get("id"),
                "ts": r.get("ts"),
                "current_ide": r.get("current_ide"),
                "foreign_decision_id": r.get("foreign_decision_id"),
                "foreign_origin": r.get("foreign_origin"),
                "current_decision_id": r.get("current_decision_id"),
                "conflict_kind": r.get("conflict_kind"),
                "similarity": r.get("similarity"),
                "summary": r.get("summary"),
                "do_not_revert": r.get("do_not_revert"),
            }
            for r in pending[:top_k]
        ],
    }
