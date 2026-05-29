"""
consensus_store.py — v3.1.0 M6 Phase B: cross-IDE conflict materialization.

The consensus subsystem in v3.1.0 ships as Phase B — read-only
conflict surfacing. It scans decisions written since this IDE's last
checkpoint, looks for ones authored by a *different* IDE that conflict
with a decision authored by *this* IDE since the same checkpoint, and
records the conflict in ``pending_conflicts.jsonl`` for human review.

Phase B never writes amendment rows; the handshake protocol is M7.

# Single-machine multi-IDE scope

v3.1.0 assumes one machine sharing one filesystem across multiple
IDEs. Cross-machine conflicts (introduced via ``git pull`` of a
teammate's branch) are scanned the same way but not auto-resolved;
the human decides via ``supersede_decision`` if needed.

# Checkpoint design

``ide_key`` → last_seen_decision_id (the largest D-id this IDE has
scanned). Decisions land in monotonically-increasing order in
decisions.jsonl thanks to ``jsonl_store.append_with_generated_id``, so
the checkpoint scalar avoids cross-machine clock drift. After each
``codevira consensus check`` run, the checkpoint advances to
``max(D-id) at scan time``.

# Pending-conflict row schema

::

    {
      "id":               "PC000001",
      "ts":               "2026-05-28T10:00:00+00:00",
      "current_ide":      "claude_code",
      "foreign_decision_id":  "D000123",
      "foreign_origin":   {"ide", "agent_model", "host_hash", "ts"},
      "current_decision_id":  "D000119",
      "current_origin":   {...},
      "conflict_kind":    "duplicate" | "asymmetric-conflict",
      "similarity":       0.78,
      "summary":          "<short rendering of the foreign decision>",
      "do_not_revert":    bool,  # of the existing protected decision
      "_schema_v":        1,
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp_server.storage import atomic, jsonl_store, paths

logger = logging.getLogger(__name__)


SCHEMA_V = 1

CONFLICT_KIND_DUPLICATE = "duplicate"
CONFLICT_KIND_ASYMMETRIC = "asymmetric-conflict"

# v3.1.0 M7: row kinds in pending_conflicts.jsonl
ROW_KIND_CONFLICT = "conflict"  # M6 read-only conflict materializations
ROW_KIND_PROPOSAL = "proposed_supersession"  # M7 proposals
ROW_KIND_RESOLUTION = "resolution"  # M7 approve/reject/withdraw

# Proposal lifecycle states.
PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPROVED = "approved"
PROPOSAL_STATUS_REJECTED = "rejected"
PROPOSAL_STATUS_WITHDRAWN = "withdrawn"
PROPOSAL_STATUS_EXPIRED = "expired"

# Default handshake timeout (overridable via
# memory.consensus.handshake_timeout_days in .codevira/config.yaml).
DEFAULT_HANDSHAKE_TIMEOUT_DAYS = 14


# ──────────────────────────────────────────────────────────────────────
# Checkpoint
# ──────────────────────────────────────────────────────────────────────


def read_checkpoint(ide_key: str) -> dict[str, Any]:
    """Return ``{last_seen_decision_id, last_seen_at}`` for ``ide_key``.

    Empty dict if the file doesn't exist (first run for this IDE).
    Malformed files return empty dict + log a warning — we'd rather
    re-scan a few extra decisions than crash the CLI.
    """
    path = paths.ide_checkpoint_path(ide_key)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("consensus_store.read_checkpoint(%s) failed: %s", ide_key, exc)
        return {}


def write_checkpoint(ide_key: str, *, last_seen_decision_id: str) -> None:
    """Persist the checkpoint atomically. Creates the checkpoints
    subdir lazily so callers don't have to."""
    path = paths.ide_checkpoint_path(ide_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_seen_decision_id": last_seen_decision_id,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "_schema_v": SCHEMA_V,
    }
    atomic.atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


# ──────────────────────────────────────────────────────────────────────
# Pending conflicts (append-only)
# ──────────────────────────────────────────────────────────────────────


def append_conflict(rec: dict[str, Any]) -> str:
    """Append a pending-conflict row; return the PC-id."""
    paths.ensure_dirs()
    rec = dict(rec)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    rec.setdefault("_schema_v", SCHEMA_V)
    return jsonl_store.append_with_generated_id(
        paths.pending_conflicts_path(), rec, prefix="PC", width=6
    )


def list_pending(*, limit: int = 50) -> list[dict[str, Any]]:
    """Return pending conflict rows, newest first."""
    return jsonl_store.read_recent(paths.pending_conflicts_path(), limit=limit)


# ──────────────────────────────────────────────────────────────────────
# Scan
# ──────────────────────────────────────────────────────────────────────


def scan_and_materialize(*, current_ide: str | None = None) -> dict[str, Any]:
    """The core of ``codevira consensus check``.

    Walks decisions with id > the current IDE's checkpoint. For each
    decision authored by a DIFFERENT IDE, runs ``check_conflict``
    against decisions authored by ``current_ide`` since the same
    checkpoint. Materializes matches into pending_conflicts.jsonl.
    Advances the checkpoint to the max decision id seen.

    Returns ``{scanned, foreign, conflicts_recorded, new_checkpoint}``.
    """
    # Lazy origin import so tests that monkeypatch CODEVIRA_IDE see
    # the override at call time.
    from mcp_server.storage import origin as origin_module

    ide_key = current_ide or origin_module.current_origin().get("ide") or "unknown"
    if ide_key == "unknown":
        # Without a known ide_key we can't meaningfully distinguish
        # 'foreign' decisions — bail out cleanly.
        return {
            "scanned": 0,
            "foreign": 0,
            "conflicts_recorded": 0,
            "skipped_reason": "current_ide=unknown (CODEVIRA_IDE not set)",
        }

    checkpoint = read_checkpoint(ide_key)
    last_seen = str(checkpoint.get("last_seen_decision_id") or "")

    # Pull all decisions via the merged view (skips superseded).
    from mcp_server.storage import decisions_store

    merged = decisions_store._read_merged()
    if not merged:
        return {
            "scanned": 0,
            "foreign": 0,
            "conflicts_recorded": 0,
            "new_checkpoint": last_seen,
        }

    fresh_decisions = [
        d for d in merged if _id_after(str(d.get("id") or ""), last_seen)
    ]

    # Current-IDE candidates since checkpoint — used as the "what does
    # the local agent believe?" corpus for the conflict check.
    current_corpus = [
        d
        for d in fresh_decisions
        if _origin_ide(d) == ide_key
        and not (d.get("is_superseded") or d.get("superseded_by"))
    ]

    foreign_decisions = [d for d in fresh_decisions if _origin_ide(d) != ide_key]

    new_pcs: list[str] = []
    for fd in foreign_decisions:
        if fd.get("is_superseded") or fd.get("superseded_by"):
            continue
        for cd in current_corpus:
            kind, sim = _check_pair(fd, cd)
            if kind is None:
                continue
            pc_rec = {
                "current_ide": ide_key,
                "foreign_decision_id": fd.get("id"),
                "foreign_origin": fd.get("origin"),
                "foreign_decision": fd.get("decision"),
                "foreign_do_not_revert": bool(fd.get("do_not_revert")),
                "current_decision_id": cd.get("id"),
                "current_origin": cd.get("origin"),
                "current_decision": cd.get("decision"),
                "current_do_not_revert": bool(cd.get("do_not_revert")),
                "conflict_kind": kind,
                "similarity": round(sim, 3),
                "summary": _short_summary(fd.get("decision") or ""),
                "do_not_revert": bool(
                    fd.get("do_not_revert") or cd.get("do_not_revert")
                ),
            }
            new_pcs.append(append_conflict(pc_rec))

    max_id = last_seen
    for d in fresh_decisions:
        did = str(d.get("id") or "")
        if _id_after(did, max_id):
            max_id = did
    if max_id and max_id != last_seen:
        write_checkpoint(ide_key, last_seen_decision_id=max_id)

    return {
        "scanned": len(fresh_decisions),
        "foreign": len(foreign_decisions),
        "conflicts_recorded": len(new_pcs),
        "new_checkpoint": max_id or last_seen,
        "current_ide": ide_key,
    }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _id_after(candidate: str, last_seen: str) -> bool:
    """Monotonic D-id comparison. Empty last_seen → all decisions are
    after. Plain string ordering works because the IDs are
    zero-padded base-36 (``D000001`` < ``D00000Z`` < ``D000010``).
    """
    if not candidate:
        return False
    if not last_seen:
        return True
    return candidate > last_seen


def _origin_ide(rec: dict[str, Any]) -> str:
    origin = rec.get("origin")
    if isinstance(origin, dict):
        return str(origin.get("ide") or "unknown")
    return "unknown"


def _check_pair(fd: dict[str, Any], cd: dict[str, Any]) -> tuple[str | None, float]:
    """Reuse the Jaccard / overlap math from check_conflict, applied
    pairwise.

    Returns (kind, similarity). kind is None if the pair doesn't
    cross the thresholds.
    """
    # Import the existing helpers — single source of truth for the
    # tokenizer + Jaccard / overlap math.
    from mcp_server.tools.check_conflict import (
        _CONFLICT_MIN_SHARED_TOKENS,
        _CONFLICT_OVERLAP_THRESHOLD,
        _DUP_THRESHOLD,
        _jaccard,
        _overlap_coefficient,
        _tokenize,
    )

    a_tokens = _tokenize(str(fd.get("decision") or ""))
    b_tokens = _tokenize(str(cd.get("decision") or ""))
    if not a_tokens or not b_tokens:
        return None, 0.0
    jaccard = _jaccard(a_tokens, b_tokens)
    overlap = _overlap_coefficient(a_tokens, b_tokens)
    shared = len(a_tokens & b_tokens)
    is_protected = bool(fd.get("do_not_revert")) or bool(cd.get("do_not_revert"))

    if jaccard >= _DUP_THRESHOLD:
        return CONFLICT_KIND_DUPLICATE, max(jaccard, overlap)
    if (
        is_protected
        and overlap >= _CONFLICT_OVERLAP_THRESHOLD
        and shared >= _CONFLICT_MIN_SHARED_TOKENS
        and jaccard < _DUP_THRESHOLD
    ):
        return CONFLICT_KIND_ASYMMETRIC, max(jaccard, overlap)
    return None, 0.0


def _short_summary(text: str, *, cap: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[: cap - 1] + "…"


# ──────────────────────────────────────────────────────────────────────
# v3.1.0 M7 Phase C — handshake protocol
# ──────────────────────────────────────────────────────────────────────


def propose_supersession(
    target_decision_id: str,
    *,
    new_decision: str,
    reason: str,
    proposing_origin: dict | None = None,
    timeout_days: int | None = None,
) -> dict[str, Any]:
    """Open a supersession proposal against ``target_decision_id``.

    Writes a ``proposed_supersession`` row to pending_conflicts.jsonl
    with ``expires_at = ts + timeout_days``. Default timeout is
    ``DEFAULT_HANDSHAKE_TIMEOUT_DAYS`` (overridable via
    ``memory.consensus.handshake_timeout_days``).

    Single-IDE fast path: if the proposing origin's IDE matches the
    target decision's origin IDE, no handshake is needed — the same
    author can revise their own decisions directly. The proposal
    short-circuits with ``fast_path: True`` so the caller can route
    to ``decisions_store.supersede`` immediately.

    Returns ``{proposed: True, proposal_id, expires_at}`` on success,
    ``{proposed: False, error}`` on failure (missing target, etc.),
    or ``{fast_path: True, ide_match}`` for the same-IDE case.
    """
    from mcp_server.storage import config as cfg
    from mcp_server.storage import decisions_store
    from mcp_server.storage import origin as origin_module

    target = decisions_store.get(target_decision_id)
    if target is None:
        return {
            "proposed": False,
            "error": f"target decision {target_decision_id} not found",
        }

    proposing_origin = proposing_origin or origin_module.current_origin()
    proposer_ide = (
        proposing_origin.get("ide") if isinstance(proposing_origin, dict) else None
    )
    target_origin = target.get("origin") or {}
    target_ide = target_origin.get("ide") if isinstance(target_origin, dict) else None

    # Fast path: same author. The protocol is a courtesy across IDEs;
    # if the same IDE proposed both, just supersede directly.
    if proposer_ide and target_ide and proposer_ide == target_ide:
        return {
            "fast_path": True,
            "ide_match": proposer_ide,
            "hint": (
                "Same author; call decisions_store.supersede directly "
                "(no handshake required)."
            ),
        }

    days = (
        timeout_days
        if isinstance(timeout_days, int) and timeout_days > 0
        else int(
            cfg.get_flag(
                "memory.consensus.handshake_timeout_days",
                default=DEFAULT_HANDSHAKE_TIMEOUT_DAYS,
            )
        )
    )
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=days)).isoformat()

    pc_id = append_conflict(
        {
            "kind": ROW_KIND_PROPOSAL,
            "ts": now.isoformat(),
            "status": PROPOSAL_STATUS_PENDING,
            "proposing_origin": proposing_origin,
            "target_decision_id": target_decision_id,
            "target_origin": target_origin,
            "proposed_new_decision": new_decision,
            "reason": reason,
            "expires_at": expires_at,
            "do_not_revert": bool(target.get("do_not_revert")),
            "summary": _short_summary(new_decision),
        }
    )
    return {
        "proposed": True,
        "proposal_id": pc_id,
        "expires_at": expires_at,
        "target_decision_id": target_decision_id,
    }


def resolve_proposal(
    proposal_id: str,
    *,
    action: str,
    comment: str | None = None,
    resolver_origin: dict | None = None,
) -> dict[str, Any]:
    """Approve, reject, or withdraw a proposal.

    Appends a ``resolution`` row referencing the proposal. The proposal
    itself stays in the JSONL (audit trail); ``find_proposal`` reads
    the latest resolution if present.

    ``approve`` / ``reject`` should come from an IDE matching the
    target decision's origin (or ``unknown``). ``withdraw`` may come
    from the proposing IDE only — we don't enforce here (caller's
    responsibility) but record the resolver_origin for audit.
    """
    if action not in (
        PROPOSAL_STATUS_APPROVED,
        PROPOSAL_STATUS_REJECTED,
        PROPOSAL_STATUS_WITHDRAWN,
    ):
        return {
            "resolved": False,
            "error": (
                f"action must be one of "
                f"{[PROPOSAL_STATUS_APPROVED, PROPOSAL_STATUS_REJECTED, PROPOSAL_STATUS_WITHDRAWN]}; "
                f"got {action!r}"
            ),
        }
    proposal = find_proposal(proposal_id)
    if proposal is None:
        return {"resolved": False, "error": f"proposal {proposal_id} not found"}

    from mcp_server.storage import origin as origin_module

    resolver_origin = resolver_origin or origin_module.current_origin()

    res_id = append_conflict(
        {
            "kind": ROW_KIND_RESOLUTION,
            "proposal_id": proposal_id,
            "action": action,
            "comment": comment,
            "resolver_origin": resolver_origin,
        }
    )
    return {
        "resolved": True,
        "resolution_id": res_id,
        "proposal_id": proposal_id,
        "action": action,
    }


def find_proposal(proposal_id: str) -> dict[str, Any] | None:
    """Locate a proposal row by id. Returns the base row (without
    folded resolution)."""
    for r in jsonl_store.read_all(paths.pending_conflicts_path()):
        if r.get("id") == proposal_id and r.get("kind") == ROW_KIND_PROPOSAL:
            return r
    return None


def find_latest_resolution(proposal_id: str) -> dict[str, Any] | None:
    """Return the most-recent resolution row for ``proposal_id`` or
    None if none exists. We walk in append order, so the last match
    wins (matches the supersede-style "latest amendment" pattern)."""
    latest: dict[str, Any] | None = None
    for r in jsonl_store.read_all(paths.pending_conflicts_path()):
        if r.get("kind") == ROW_KIND_RESOLUTION and r.get("proposal_id") == proposal_id:
            latest = r
    return latest


def proposal_status(proposal_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the merged proposal view: base + latest resolution +
    derived status (pending, approved, rejected, withdrawn, expired).
    """
    proposal = find_proposal(proposal_id)
    if proposal is None:
        return {"found": False}

    latest = find_latest_resolution(proposal_id)
    derived = PROPOSAL_STATUS_PENDING
    if latest is not None:
        action = latest.get("action")
        if action in (
            PROPOSAL_STATUS_APPROVED,
            PROPOSAL_STATUS_REJECTED,
            PROPOSAL_STATUS_WITHDRAWN,
        ):
            derived = action
    if derived == PROPOSAL_STATUS_PENDING:
        # Check expiry.
        exp = proposal.get("expires_at")
        if isinstance(exp, str):
            try:
                exp_dt = datetime.fromisoformat(exp)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                now_dt = now or datetime.now(timezone.utc)
                if now_dt >= exp_dt:
                    derived = PROPOSAL_STATUS_EXPIRED
            except (ValueError, TypeError):
                pass

    return {
        "found": True,
        "proposal": proposal,
        "latest_resolution": latest,
        "status": derived,
    }


def finalize_proposal(
    proposal_id: str,
    *,
    expired_unilateral: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convert an approved (or expired) proposal into a real
    supersession via ``decisions_store.supersede``.

    ``expired_unilateral=True``: the proposer is force-finalizing
    past the expiry; an audit-only ``resolution`` row is appended
    with ``action='expired'`` and ``expired_unilateral=True`` so the
    history shows the proposer didn't wait for human approval.
    """
    state = proposal_status(proposal_id, now=now)
    if not state["found"]:
        return {"finalized": False, "error": "proposal not found"}

    derived = state["status"]
    if derived not in (PROPOSAL_STATUS_APPROVED, PROPOSAL_STATUS_EXPIRED):
        return {
            "finalized": False,
            "error": (
                f"proposal {proposal_id} cannot be finalized from "
                f"status={derived!r}"
            ),
        }
    if derived == PROPOSAL_STATUS_EXPIRED and not expired_unilateral:
        return {
            "finalized": False,
            "error": (
                f"proposal {proposal_id} has expired; pass "
                f"expired_unilateral=True to force-finalize and "
                f"record the audit row."
            ),
        }

    proposal = state["proposal"]
    target_id = proposal.get("target_decision_id")
    new_text = proposal.get("proposed_new_decision") or ""
    reason = proposal.get("reason") or "consensus-handshake supersession"

    from mcp_server.storage import decisions_store

    sup_result = decisions_store.supersede(
        old_id=str(target_id),
        new_decision=new_text,
        reason=reason,
    )
    if not sup_result.get("success"):
        return {"finalized": False, "error": sup_result.get("error")}

    # Audit-only row when we expired-unilateral.
    if expired_unilateral:
        from mcp_server.storage import origin as origin_module

        append_conflict(
            {
                "kind": ROW_KIND_RESOLUTION,
                "proposal_id": proposal_id,
                "action": PROPOSAL_STATUS_EXPIRED,
                "expired_unilateral": True,
                "resolver_origin": origin_module.current_origin(),
                "comment": "force-finalized past expires_at",
            }
        )

    return {
        "finalized": True,
        "proposal_id": proposal_id,
        "supersedes": target_id,
        "new_decision_id": sup_result.get("new_id"),
        "expired_unilateral": expired_unilateral,
    }


def list_proposals(
    *,
    status: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return proposals (newest first) with their derived status.

    ``status`` filter: ``"pending"`` / ``"approved"`` / ``"rejected"``
    / ``"withdrawn"`` / ``"expired"`` / ``None`` (all).
    """
    raw = jsonl_store.read_recent(paths.pending_conflicts_path(), limit=limit * 4)
    out: list[dict[str, Any]] = []
    for row in raw:
        if row.get("kind") != ROW_KIND_PROPOSAL:
            continue
        pid = str(row.get("id") or "")
        st = proposal_status(pid, now=now)
        if not st["found"]:
            continue
        derived = st["status"]
        if status is not None and derived != status:
            continue
        out.append({**row, "_derived_status": derived})
        if len(out) >= limit:
            break
    return out
