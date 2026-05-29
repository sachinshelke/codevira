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

from mcp_server.storage import config, consensus_store


# v3.1.0 M7 Phase C: the handshake-using tools call
# config.is_enabled("memory.consensus.handshake_enabled", default=False)
# inline at entry. Inlined (not via a helper) to keep the
# blast-radius surface minimal on this module.


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


# ──────────────────────────────────────────────────────────────────────
# v3.1.0 M7 Phase C — handshake MCP tools
# ──────────────────────────────────────────────────────────────────────


def consensus_propose_supersession(
    target_decision_id: str,
    *,
    new_decision: str,
    reason: str,
) -> dict[str, Any]:
    """Open a cross-IDE supersession proposal.

    Opt-in: returns ``{"disabled": True}`` unless
    ``memory.consensus.handshake_enabled`` is set in
    ``.codevira/config.yaml``.

    Fast-path: when the proposing IDE is the same as the target
    decision's origin IDE, no handshake is needed and the response
    carries ``fast_path: True`` — the caller should use
    ``supersede_decision`` directly.
    """
    if not config.is_enabled("memory.consensus.handshake_enabled", default=False):
        return {
            "disabled": True,
            "feature": "memory.consensus.handshake_enabled",
            "hint": (
                "The handshake protocol is opt-in. Enable it via "
                ".codevira/config.yaml: memory.consensus."
                "handshake_enabled: true"
            ),
        }
    return consensus_store.propose_supersession(
        target_decision_id,
        new_decision=new_decision,
        reason=reason,
    )


def consensus_resolve(
    proposal_id: str,
    *,
    action: str,
    comment: str | None = None,
) -> dict[str, Any]:
    """Approve, reject, or withdraw a pending proposal.

    Opt-in via ``memory.consensus.handshake_enabled``. Returns a
    structured ``{"resolved": False, "error": ...}`` rather than
    raising on bad input so the agent can correct and retry.
    """
    if not config.is_enabled("memory.consensus.handshake_enabled", default=False):
        return {
            "disabled": True,
            "feature": "memory.consensus.handshake_enabled",
        }
    return consensus_store.resolve_proposal(proposal_id, action=action, comment=comment)


def origin_of(decision_id: str) -> dict[str, Any]:
    """Return the origin block attached to a decision (M1 provenance).

    Always available — does not require the handshake flag.
    """
    from mcp_server.storage import decisions_store

    decision = decisions_store.get(decision_id)
    if decision is None:
        return {"found": False, "error": f"decision {decision_id} not found"}
    origin = decision.get("origin")
    return {
        "found": True,
        "decision_id": decision_id,
        "origin": origin if isinstance(origin, dict) else None,
        "do_not_revert": bool(decision.get("do_not_revert")),
        "is_superseded": bool(decision.get("is_superseded")),
        "superseded_by": decision.get("superseded_by"),
    }
