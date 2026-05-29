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
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import atomic, jsonl_store, paths

logger = logging.getLogger(__name__)


SCHEMA_V = 1

CONFLICT_KIND_DUPLICATE = "duplicate"
CONFLICT_KIND_ASYMMETRIC = "asymmetric-conflict"


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
