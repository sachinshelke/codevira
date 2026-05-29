"""
working_store.py — v3.1.0 M2: bounded, decay-scored working memory.

Working memory is the agent's intra-session scratchpad. It holds
observations (things the agent saw — file edits, errors, command
outputs) and goals (what the agent is currently trying to
accomplish). Entries decay with time and accumulate "access" weight
from repeated reads; the top-K by score is what ``get_working_context``
returns into the ReAct loop.

# Why a separate store

- **Capacity-bounded**: byte-bounded so a single 20-file refactor
  can't flood the JSONL.
- **Ephemeral by default**: lives in ``.codevira-cache/working.jsonl``
  (gitignored, per-machine). Working memory IS scratchpad; the next
  developer doesn't need to inherit your half-formed hypotheses.
- **Opt-in promotion**: when a session produces something worth
  team-sharing, ``codevira working commit <session_id>`` copies the
  non-evicted entries to ``.codevira/working_archived/<session_id>.jsonl``
  (canonical, gitable).

# Lifecycle

- Append-only writes via ``jsonl_store.append_with_generated_id``
  (W-prefixed monotonic ids).
- Eviction = amendment row ``{_amendment_to_id: <wid>, _evicted: true}``.
  The read path tombstones the original.
- Promotion (working_promote → LTM) = amendment row
  ``{_amendment_to_id: <wid>, _promoted_to: <new_id>}``. Same tombstone
  effect on reads, plus a backref for audit.
- Periodic ``compact()`` (during ``codevira sync``) physically drops
  tombstoned rows so the file stays bounded.

# Decay scoring (computed lazily on read; nothing on disk)

::

    score = importance × exp(-Δt_hours / τ) + 0.5 × access_count
            τ = 6 hours (workday arc)

This is the additive Generative-Agents composition (recency ×
importance + access). importance is integer 1-10 (5 = default).
access_count is incremented externally when entries are looked at;
this module does not auto-increment on every list call (would force
a write per read).

# Schema

All entries carry ``_schema_v: 1`` per the v3.0.1 forward-compat
convention. The full base-record shape::

    {
      "id":               "W000001",
      "ts":               "2026-05-28T10:00:00+00:00",
      "session_id":       "ad-hoc-a1b2c3",
      "origin":           {"ide": ..., "agent_model": ..., "host_hash": ..., "ts": ...},
      "kind":             "observation" | "goal",
      "content":          "<≤ 2 KB markdown>",
      "importance":       1-10,
      "confidence":       0.0-1.0 | null,
      "access_count":     0,
      "last_accessed_at": null,
      "links":            ["D000123", ...],
      "_schema_v":        1,
    }

Amendment rows preserve ``id`` and add the marker (``_evicted: true`` or
``_promoted_to: "<wid>"``). They share the base id so
``jsonl_store.read_merged`` folds them automatically (the same
convention decisions.jsonl uses).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import jsonl_store, origin, paths


# Schema constants
SCHEMA_V = 1
KIND_OBSERVATION = "observation"
KIND_GOAL = "goal"
_VALID_KINDS = frozenset({KIND_OBSERVATION, KIND_GOAL})

# Caps
_CONTENT_MAX_BYTES = 2048  # plan: ≤ 2 KB
_DEFAULT_IMPORTANCE = 5
_DECAY_TAU_HOURS = 6.0  # workday arc


# ──────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────


def add(
    content: str,
    *,
    kind: str = KIND_OBSERVATION,
    importance: int = _DEFAULT_IMPORTANCE,
    confidence: float | None = None,
    links: list[str] | None = None,
    session_id: str | None = None,
    origin_override: dict | None = None,
) -> str:
    """Append a working-memory entry; return the generated W-id.

    Raises:
        ValueError: invalid kind, content too large, or importance/
            confidence out of range. All inputs are validated up front
            so the disk store never sees malformed data.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"working_store.add: kind must be one of {sorted(_VALID_KINDS)}; got {kind!r}"
        )
    if not isinstance(content, str) or not content:
        raise ValueError("working_store.add: content must be a non-empty string")
    if len(content.encode("utf-8")) > _CONTENT_MAX_BYTES:
        raise ValueError(
            f"working_store.add: content exceeds {_CONTENT_MAX_BYTES} byte cap "
            f"({len(content.encode('utf-8'))} bytes given)"
        )
    if not isinstance(importance, int) or not (1 <= importance <= 10):
        raise ValueError(
            f"working_store.add: importance must be int in 1..10; got {importance!r}"
        )
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise ValueError(
            f"working_store.add: confidence must be in 0.0..1.0 or None; got {confidence!r}"
        )

    paths.ensure_dirs()

    from mcp_server.storage import decisions_store  # local: avoid import cycle

    base_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or decisions_store.default_session_id(),
        "origin": origin_override or origin.current_origin(),
        "kind": kind,
        "content": content,
        "importance": int(importance),
        "confidence": float(confidence) if confidence is not None else None,
        "access_count": 0,
        "last_accessed_at": None,
        "links": list(links or []),
        "_schema_v": SCHEMA_V,
    }

    return jsonl_store.append_with_generated_id(
        paths.working_path(), base_record, prefix="W", width=6
    )


def mark_evicted(entry_id: str, *, reason: str | None = None) -> bool:
    """Tombstone an entry via amendment. Returns True on success.

    Eviction is logical (the row stays in the JSONL until ``compact()``
    physically drops it during ``codevira sync``). This keeps the file
    append-only and keeps the audit trail intact for the rest of the
    session.
    """
    paths.ensure_dirs()
    amendment = {
        "id": entry_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": entry_id,
        "_evicted": True,
    }
    if reason:
        amendment["_evict_reason"] = reason
    jsonl_store.append(paths.working_path(), amendment)
    return True


def mark_promoted(entry_id: str, target_id: str) -> bool:
    """Tombstone an entry as 'promoted to LTM', recording the new id.

    Called by ``working_promote`` after a successful LTM write (decision,
    skill, or playbook). The backref is audit-only; the read-side
    tombstoning is the same as eviction.
    """
    paths.ensure_dirs()
    amendment = {
        "id": entry_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": entry_id,
        "_promoted_to": target_id,
    }
    jsonl_store.append(paths.working_path(), amendment)
    return True


# ──────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────


def _tombstoned_ids() -> set[str]:
    """Pre-scan raw rows for amendments with ``_evicted`` or
    ``_promoted_to``. ``jsonl_store.read_merged`` deliberately filters
    underscore-prefixed fields when overlaying amendments (matches the
    decisions.jsonl convention — metadata markers don't pollute
    user-visible state). That filtering means we cannot detect
    tombstones from the merged view; instead we scan amendments
    separately and return the set of tombstoned base ids.

    Cheap for the working-memory size budget (< 64 KB live). For a
    larger store this would warrant caching.
    """
    out: set[str] = set()
    for rec in jsonl_store.read_all(paths.working_path()):
        if rec.get("_amendment_to_id") and (
            rec.get("_evicted") or rec.get("_promoted_to")
        ):
            out.add(str(rec.get("id") or ""))
    return out


def list_top_k(
    *,
    top_k: int = 10,
    kind: str | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the top-K live entries by decay score, newest-first on ties.

    ``kind`` and ``session_id`` are optional filters. Entries
    tombstoned via ``_evicted`` or ``_promoted_to`` amendments are
    excluded (detected by ``_tombstoned_ids``). Decay is computed
    against ``now`` (defaulting to wall-clock UTC); tests pin it for
    determinism.
    """
    merged = jsonl_store.read_merged(paths.working_path())
    if not merged:
        return []

    now_dt = now or datetime.now(timezone.utc)
    dead = _tombstoned_ids()

    out: list[tuple[float, dict[str, Any]]] = []
    for rec in merged:
        if str(rec.get("id") or "") in dead:
            continue
        if kind is not None and rec.get("kind") != kind:
            continue
        if session_id is not None and rec.get("session_id") != session_id:
            continue
        score = _compute_score(rec, now=now_dt)
        out.append((score, rec))

    # Sort by score desc, ts desc as tie-breaker (newest wins).
    out.sort(key=lambda x: (x[0], x[1].get("ts") or ""), reverse=True)
    return [r for _, r in out[:top_k]]


def list_session_entries(session_id: str) -> list[dict[str, Any]]:
    """Return all live (non-tombstoned) entries for a session in
    insertion order. Used by ``commit_session`` for the opt-in
    promotion to ``working_archived``.
    """
    merged = jsonl_store.read_merged(paths.working_path())
    dead = _tombstoned_ids()
    return [
        r
        for r in merged
        if r.get("session_id") == session_id and str(r.get("id") or "") not in dead
    ]


def get(entry_id: str) -> dict[str, Any] | None:
    """Return the merged record for a single entry, or None."""
    for rec in jsonl_store.read_merged(paths.working_path()):
        if str(rec.get("id")) == entry_id:
            return rec
    return None


# ──────────────────────────────────────────────────────────────────────
# Maintenance
# ──────────────────────────────────────────────────────────────────────


def compact() -> int:
    """Drop tombstoned (evicted or promoted) entries from working.jsonl.

    Called by ``codevira sync``. Holds the file lock for the entire
    read-filter-write via ``jsonl_store.compact``. Returns count
    dropped (counts BOTH the base row and its amendment row).

    Two-pass design: the keep predicate needs to know which base ids
    are tombstoned, but ``jsonl_store.compact`` evaluates the predicate
    per-record. We pre-scan the file to collect the tombstoned id set,
    then the predicate closes over it. Acceptable for the < 64 KB cap
    working memory targets; if we ever grow past that we'd want a
    single-pass compactor that streams.
    """
    path = paths.working_path()
    if not path.is_file():
        return 0
    return jsonl_store.compact(path, keep_predicate=_build_compact_predicate(path))


def _build_compact_predicate(path):
    """Pre-scan the file to find tombstoned base ids; return a
    predicate that drops them AND their amendment rows.
    """
    tombstoned: set[str] = set()
    for rec in jsonl_store.read_all(path):
        if rec.get("_amendment_to_id") and (
            rec.get("_evicted") or rec.get("_promoted_to")
        ):
            tombstoned.add(str(rec.get("id")))

    def predicate(rec: dict[str, Any]) -> bool:
        rec_id = str(rec.get("id") or "")
        if rec_id in tombstoned:
            return False  # drop both the base and any amendment rows for it
        return True

    return predicate


def commit_session(session_id: str) -> dict[str, Any]:
    """Copy a session's live entries from ``working.jsonl`` to
    ``.codevira/working_archived/<session_id>.jsonl``.

    The original cache file is left untouched (the user may want to
    keep iterating). Idempotent: re-running for the same session_id
    appends fresh rows (the destination is its own append-only log).

    Returns ``{"session_id", "committed_count", "destination"}``.
    """
    paths.ensure_dirs()
    entries = list_session_entries(session_id)
    if not entries:
        return {
            "session_id": session_id,
            "committed_count": 0,
            "destination": None,
            "note": "No live entries for this session_id in working memory.",
        }

    dest = paths.working_archived_path(session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for rec in entries:
        jsonl_store.append(dest, rec)
    return {
        "session_id": session_id,
        "committed_count": len(entries),
        "destination": str(dest),
    }


# ──────────────────────────────────────────────────────────────────────
# Decay scoring
# ──────────────────────────────────────────────────────────────────────


def _compute_score(entry: dict[str, Any], *, now: datetime) -> float:
    """``importance × exp(-Δt_hours / τ) + 0.5 × access_count``.

    Robust to malformed ts (returns importance + access term only —
    i.e., treat as 'just now' so the entry doesn't get penalized for
    bad metadata).
    """
    ts_raw = entry.get("ts")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
        except (ValueError, TypeError):
            delta_hours = 0.0
    else:
        delta_hours = 0.0

    importance = entry.get("importance", _DEFAULT_IMPORTANCE)
    access_count = entry.get("access_count", 0)
    try:
        imp = int(importance)
    except (ValueError, TypeError):
        imp = _DEFAULT_IMPORTANCE
    try:
        acc = int(access_count)
    except (ValueError, TypeError):
        acc = 0

    return imp * math.exp(-delta_hours / _DECAY_TAU_HOURS) + 0.5 * acc
