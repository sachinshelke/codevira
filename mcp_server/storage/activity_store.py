"""
activity_store.py — v3.1.0 M4 Phase 1: spatial-activity log.

Records *where* in the codebase the agent has been working — edits,
decisions tagged with a file. The downstream spatial tools
(``spatial_nearby``, ``spatial_heat``) read this log to surface
focus zones and rank neighbors by recent attention.

# Why a separate store

- **Per-developer**: each engineer's attention pattern is theirs.
  Living in ``.codevira-cache/activity.jsonl`` (gitignored, per
  machine) avoids polluting the team's git diff with someone else's
  exploration history.
- **Opt-in team export**: ``codevira spatial export-activity``
  aggregates and writes ``.codevira/activity_summary.yaml`` when a
  team wants the heat map shared.
- **Compaction-friendly**: append-only JSONL with capped retention
  (default 90 days) keeps the file from growing without bound on
  long-running projects.

# Schema

::

    {
      "id":         "A000001",
      "ts":         "2026-05-28T10:00:00+00:00",
      "node_id":    "<project-relative file path>",
      "kind":       "edit" | "decision_ref",
      "session_id": "ad-hoc-a1b2c3",
      "origin":     {"ide", "agent_model", "host_hash", "ts"},
      "_schema_v":  1,
    }

In v3.1.0 ``node_id`` is per-file. Per-symbol granularity needs
``graph.sqlite`` schema changes and is explicitly deferred to v3.2+.

# Kinds

  - ``edit``         — emitted by ``memory_fanout`` on Edit / Write /
                       MultiEdit / NotebookEdit / update_node events.
  - ``decision_ref`` — emitted by ``decisions_store.record()`` when
                       the new decision carries a ``file_path``.

The plan also reserves a ``visit`` kind for future use (read-only
tool calls), but those are deliberately NOT emitted in v3.1.0 to
keep the log dense with "did this" signal rather than "looked at"
noise — see ``memory_fanout._build_observation`` for the same
filter applied to working memory.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp_server.storage import jsonl_store, origin as origin_module, paths

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

SCHEMA_V = 1

KIND_EDIT = "edit"
KIND_DECISION_REF = "decision_ref"
_VALID_KINDS = frozenset({KIND_EDIT, KIND_DECISION_REF})

# Retention defaults — overridable via codevira sync command flags.
DEFAULT_RETENTION_DAYS = 90


# ──────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────


def add(
    node_id: str,
    *,
    kind: str = KIND_EDIT,
    session_id: str | None = None,
    origin_override: dict | None = None,
) -> str:
    """Append an activity row; return the generated A-id.

    Inputs validated up front; failures raise ValueError so callers
    in the hot path (``memory_fanout``) can wrap and drop silently
    per their fail-open contract.
    """
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("activity_store.add: node_id must be a non-empty string")
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"activity_store.add: kind must be one of {sorted(_VALID_KINDS)}; "
            f"got {kind!r}"
        )

    paths.ensure_dirs()

    from mcp_server.storage import decisions_store  # local: avoid cycle

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "node_id": node_id.strip(),
        "kind": kind,
        "session_id": session_id or decisions_store.default_session_id(),
        "origin": origin_override or origin_module.current_origin(),
        "_schema_v": SCHEMA_V,
    }
    return jsonl_store.append_with_generated_id(
        paths.activity_path(), rec, prefix="A", width=6
    )


# ──────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────


def list_recent(
    *,
    limit: int = 50,
    kind: str | None = None,
    node_id: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` activity rows, newest first.

    Optional filters compose AND-wise. ``since`` excludes rows older
    than the cutoff (useful for time-windowed heatmaps).
    """
    raw = jsonl_store.read_recent(paths.activity_path(), limit=limit * 4)
    out: list[dict[str, Any]] = []
    for rec in raw:
        if kind is not None and rec.get("kind") != kind:
            continue
        if node_id is not None and rec.get("node_id") != node_id:
            continue
        if since is not None:
            ts_str = rec.get("ts")
            if not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
            except (ValueError, TypeError):
                continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def list_top_k_files(
    *,
    top_k: int = 20,
    since: datetime | None = None,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Top-K ``node_id`` values by weighted activity count.

    Weights default to ``{"edit": 1.0, "decision_ref": 2.0}`` — a
    decision tied to a file is a stronger "attention" signal than a
    single edit (edits are abundant; decisions are deliberate).

    Returns ``[{node_id, edit_count, decision_ref_count, score}, ...]``
    sorted by ``score`` descending.
    """
    w = weights or {KIND_EDIT: 1.0, KIND_DECISION_REF: 2.0}

    raw = jsonl_store.read_all(paths.activity_path())
    counts: dict[str, dict[str, int]] = {}

    for rec in raw:
        if since is not None:
            ts_str = rec.get("ts")
            if not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
            except (ValueError, TypeError):
                continue
        nid = rec.get("node_id")
        kind = rec.get("kind")
        if not isinstance(nid, str) or kind not in _VALID_KINDS:
            continue
        bucket = counts.setdefault(nid, {KIND_EDIT: 0, KIND_DECISION_REF: 0})
        bucket[kind] = bucket.get(kind, 0) + 1

    scored: list[dict[str, Any]] = []
    for nid, by_kind in counts.items():
        score = sum(w.get(k, 0.0) * v for k, v in by_kind.items())
        scored.append(
            {
                "node_id": nid,
                "edit_count": by_kind.get(KIND_EDIT, 0),
                "decision_ref_count": by_kind.get(KIND_DECISION_REF, 0),
                "score": round(score, 3),
            }
        )

    scored.sort(key=lambda r: (r["score"], r["node_id"]), reverse=True)
    return scored[:top_k]


def visit_count_30d(node_id: str, *, now: datetime | None = None) -> int:
    """Total ``edit`` + ``decision_ref`` events for ``node_id`` in the
    last 30 days. Used by ``spatial_nearby`` ranking.
    """
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=30)
    n = 0
    for rec in jsonl_store.read_all(paths.activity_path()):
        if rec.get("node_id") != node_id:
            continue
        ts_str = rec.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            n += 1
    return n


# ──────────────────────────────────────────────────────────────────────
# Maintenance
# ──────────────────────────────────────────────────────────────────────


def compact(*, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Drop activity rows older than ``retention_days``. Called by
    ``codevira sync``. Returns count dropped.

    Holds the file lock for the entire read-filter-write via
    ``jsonl_store.compact``. The default 90-day window is long
    enough for monthly spatial heatmaps without unbounded growth on
    a project the agent has worked on for a year.
    """
    path = paths.activity_path()
    if not path.is_file():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    def _keep(rec: dict[str, Any]) -> bool:
        ts_str = rec.get("ts")
        if not isinstance(ts_str, str):
            return True  # don't drop malformed rows (codevira doctor handles those)
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        return ts >= cutoff

    return jsonl_store.compact(path, keep_predicate=_keep)
