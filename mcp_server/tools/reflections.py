"""
reflections.py — v3.1.0 M8 MCP tools for episodic abstractions.

Three tools cover the agent-facing surface:

  - reflect           — build the source context + rendered prompt for
                        the host LLM to abstract over. v3.1.0 ships
                        with the actual sampling call stubbed; v3.2
                        wires the MCP sampling/createMessage RPC
                        through. In v3.1, callers receive
                        ``{sampling_supported: False}`` plus the
                        prerendered prompt so they can feed it to the
                        LLM manually and ``reflect_apply()`` (CLI) to
                        persist the result.
  - get_reflections   — top-K most recent reflections.
  - list_reflections  — filtered list (since / tags / limit).

The opt-in scheduled-reflection path
(``memory.reflections.auto_reflect_days``) reads its flag via
config.get_flag. The MCP tools themselves always work — reflections
are read-only consumers of episodic memory and never produce
side effects on decisions/sessions.
"""

from __future__ import annotations

from typing import Any

from mcp_server.storage import reflections_store


def reflect(
    *,
    period_days: int = 7,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Build the source context + rendered prompt for an LLM
    abstraction.

    v3.1.0: returns ``sampling_supported: False`` with the rendered
    prompt + source_context so callers can feed the prompt to a
    locally-available LLM (or via codevira reflect --from-file). The
    sampling/createMessage MCP RPC integration is the v3.2 deliverable;
    swapping the stub for a real sampling call is a single-function
    change.

    ``dry_run=True`` (the default) is the storage-safe path; it never
    writes — the caller decides when to apply via reflect_apply or
    ``codevira reflect --apply``.
    """
    ctx = reflections_store.build_source_context(period_days=period_days)
    prompt = reflections_store.render_prompt(ctx)
    return {
        "sampling_supported": False,
        "deferred_to": "v3.2",
        "hint": (
            "v3.1.0 ships reflections' storage + prompt-rendering + "
            "sanitization. The MCP sampling/createMessage RPC that "
            "would call the host LLM is the v3.2 follow-up. Until then "
            "you can run `codevira reflect --from-file <path>` to commit "
            "an LLM-supplied abstraction, or read the rendered prompt "
            "below and feed it to your own LLM."
        ),
        "period_days": period_days,
        "period_start": ctx["period_start"],
        "period_end": ctx["period_end"],
        "source_context": {
            "session_count": len(ctx["sessions"]),
            "decision_count": len(ctx["decisions"]),
            "envelope_bytes": ctx["envelope_bytes"],
            "source_session_ids": ctx["source_session_ids"],
            "source_decision_ids": ctx["source_decision_ids"],
        },
        "rendered_prompt": prompt,
        "dry_run": bool(dry_run),
    }


def get_reflections(*, top_k: int = 5) -> dict[str, Any]:
    """Top-K most recent reflections (newest first)."""
    rows = reflections_store.list_recent(limit=top_k)
    return {
        "count": len(rows),
        "reflections": [
            {
                "reflection_id": r.get("id"),
                "ts": r.get("ts"),
                "period_start": r.get("period_start"),
                "period_end": r.get("period_end"),
                "abstraction": r.get("abstraction"),
                "confidence": r.get("confidence"),
                "tags": r.get("tags") or [],
                "model_used": r.get("model_used"),
                "source_session_ids": r.get("source_session_ids") or [],
                "source_decision_ids": r.get("source_decision_ids") or [],
            }
            for r in rows
        ],
    }


def list_reflections(
    *,
    since: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Filtered reflection list. ``since`` is an ISO 8601 timestamp
    cutoff; ``tags`` is set-intersection (every requested tag must
    appear)."""
    rows = reflections_store.list_filtered(since=since, tags=tags, limit=limit)
    return {
        "count": len(rows),
        "reflections": [
            {
                "reflection_id": r.get("id"),
                "ts": r.get("ts"),
                "tags": r.get("tags") or [],
                "abstraction": r.get("abstraction"),
                "confidence": r.get("confidence"),
            }
            for r in rows
        ],
        "filtered_by": {"since": since, "tags": tags},
    }
