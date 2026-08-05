"""
provenance.py — which IDE/machine wrote a decision (v3.1.0 M1).

Extracted from tools/consensus.py in 4.0 when the consensus subsystem was
removed. Provenance is NOT consensus: origin tagging ships on every write
and is read by `codevira doctor` and cross-tool debugging, whereas the
consensus handshake produced zero artifacts in any project and was cut.
"""

from __future__ import annotations

from typing import Any


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
