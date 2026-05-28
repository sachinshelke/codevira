"""
sessions_store.py — high-level facade over sessions.jsonl.

Sessions are append-only event records ("this session did X").
Decisions made during a session reference it via ``session_id``; this
file gives those session IDs their narrative summary.

Schema (one record per session-log call):

  {
    "id":           "S000001",
    "ts":           "2026-05-19T10:00:00Z",
    "session_id":   "morning-auth",   # human-chosen slug
    "task":         "Implemented bcrypt password hashing",
    "phase":        "1",              # optional
    "summary":      "...",            # optional
    "decision_ids": ["D000001"],      # decisions written during this session
    "outcome":      "...",            # optional
  }

Like decisions_store, sessions_store is append-only. We don't support
mid-session edits — if a session needs updating, a new entry is
appended (semantically a new session log; UI may render them grouped
by ``session_id``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import jsonl_store, origin, paths

logger = logging.getLogger(__name__)


def write(
    session_id: str,
    *,
    task: str | None = None,
    phase: str | None = None,
    summary: str | None = None,
    decision_ids: list[str] | None = None,
    outcome: str | None = None,
) -> str:
    """Append a single session log; return generated id."""
    paths.ensure_dirs()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "task": task,
        "phase": phase,
        "summary": summary,
        "decision_ids": list(decision_ids or []),
        "outcome": outcome,
        # v3.1.0 M1: provenance tagging — which IDE/agent/machine
        # wrote this session log. Reads tolerate absence on legacy
        # records (v3.0.x sessions have no origin).
        "origin": origin.current_origin(),
    }
    return jsonl_store.append_with_generated_id(
        paths.sessions_path(), record, prefix="S", width=6
    )


def write_many(logs: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Append many session logs. Returns ``(ids, errors)``.

    Each input log dict may have: session_id, task, phase, summary,
    decision_ids, outcome. session_id is required.
    """
    paths.ensure_dirs()
    ids: list[str] = []
    errors: list[dict[str, Any]] = []

    for i, log in enumerate(logs):
        sid = log.get("session_id")
        if not sid:
            errors.append({"index": i, "error": "session_id is required"})
            continue
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": sid,
            "task": log.get("task"),
            "phase": log.get("phase"),
            "summary": log.get("summary"),
            "decision_ids": list(log.get("decisions") or log.get("decision_ids") or []),
            "outcome": log.get("outcome"),
            # v3.1.0 M1: provenance tagging (see write() above).
            "origin": origin.current_origin(),
        }
        # If decisions are passed as full dicts (legacy contract from
        # v2.1.x write_session_log), extract just their ids when present.
        cleaned_decisions = []
        for d in record["decision_ids"]:
            if isinstance(d, dict):
                if "id" in d:
                    cleaned_decisions.append(d["id"])
                # else: skip — we don't auto-create decisions from session
                # log entries in v2.2.0 (use record_decision directly)
            else:
                cleaned_decisions.append(d)
        record["decision_ids"] = cleaned_decisions

        sid_out = jsonl_store.append_with_generated_id(
            paths.sessions_path(), record, prefix="S", width=6
        )
        ids.append(sid_out)

    return ids, errors


def read_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` session logs, newest first.

    Thin wrapper around the v3.0.1 shared primitive
    ``jsonl_store.read_recent`` so v3.1 stores (working memory,
    activity, reflections) get the same sort+slice behavior without
    duplicating it.
    """
    return jsonl_store.read_recent(paths.sessions_path(), limit=limit)


def by_session_id(session_id: str) -> list[dict[str, Any]]:
    """Return all log entries for a given session_id, oldest first."""
    matches = [
        s
        for s in jsonl_store.read_all(paths.sessions_path())
        if s.get("session_id") == session_id
    ]
    matches.sort(key=lambda s: s.get("ts") or "")
    return matches
