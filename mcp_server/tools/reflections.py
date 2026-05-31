"""
reflections.py — v3.1.0 M8 + v3.2.0 sampling MCP tools for episodic abstractions.

Three tools cover the agent-facing surface:

  - reflect           — sync entry; renders the source-context + prompt
                        for callers that can't (or won't) call the host
                        LLM. Used by the CLI.
  - reflect_async     — v3.2.0: async entry used by the MCP server. Tries
                        ``sampling/createMessage`` against the connected
                        client; degrades to the v3.1.0 stub on any failure.
  - get_reflections   — top-K most recent reflections.
  - list_reflections  — filtered list (since / tags / limit).

v3.2.0 ships the MCP sampling/createMessage integration. When the host
client advertises ``capabilities.sampling``, ``reflect_async`` calls the
host LLM and (if ``dry_run=False``) persists the result via
``reflections_store.append``. The sync ``reflect`` keeps the v3.1.0
behavior so existing CLI flows are unchanged.
"""

from __future__ import annotations

from typing import Any

from mcp_server.storage import reflections_store


def _source_context_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_count": len(ctx["sessions"]),
        "decision_count": len(ctx["decisions"]),
        "envelope_bytes": ctx["envelope_bytes"],
        "source_session_ids": ctx["source_session_ids"],
        "source_decision_ids": ctx["source_decision_ids"],
    }


def _stub_response(
    *,
    ctx: dict[str, Any],
    prompt: str,
    period_days: int,
    dry_run: bool,
    sampling_error: str | None = None,
) -> dict[str, Any]:
    """v3.1.0-compatible response shape — returned when sampling isn't
    available OR the host LLM call failed. Callers can still feed
    rendered_prompt into a local LLM and commit via the CLI."""
    return {
        "sampling_supported": False,
        "deferred_to": "v3.2-or-host-without-sampling",
        "sampling_error": sampling_error,
        "hint": (
            "Sampling unavailable — either the MCP host doesn't advertise "
            "sampling capability, or codevira's `reflect` tool was called "
            "outside an active MCP request context (e.g. from the CLI). "
            "Feed rendered_prompt below to your own LLM, then commit via "
            "`codevira reflect --from-file <path>`."
        ),
        "period_days": period_days,
        "period_start": ctx["period_start"],
        "period_end": ctx["period_end"],
        "source_context": _source_context_summary(ctx),
        "rendered_prompt": prompt,
        "dry_run": bool(dry_run),
    }


def reflect(
    *,
    period_days: int = 7,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Sync entry — returns the rendered prompt + stub (no LLM call).

    Used by the CLI (which has no MCP session) and by callers that
    want the v3.1.0-compatible shape. The MCP server uses
    :func:`reflect_async` instead so it can call the host LLM via
    ``sampling/createMessage`` when the client supports it.
    """
    ctx = reflections_store.build_source_context(period_days=period_days)
    prompt = reflections_store.render_prompt(ctx)
    return _stub_response(
        ctx=ctx,
        prompt=prompt,
        period_days=period_days,
        dry_run=dry_run,
    )


async def reflect_async(
    *,
    period_days: int = 7,
    dry_run: bool = True,
    server_session: Any = None,
) -> dict[str, Any]:
    """v3.2.0: real ``sampling/createMessage`` path.

    If ``server_session`` has sampling capability advertised by the
    client, this calls the host LLM to synthesize the reflection. On
    success and when ``dry_run=False``, the abstraction is persisted
    via ``reflections_store.append`` and the returned dict carries
    the new R-id.

    On any failure (no session, no capability, LLM error, malformed
    response) the response gracefully degrades to the v3.1.0 stub
    shape so callers keep working. The ``sampling_error`` field
    surfaces the diagnostic reason for ``codevira doctor``.
    """
    ctx = reflections_store.build_source_context(period_days=period_days)
    prompt = reflections_store.render_prompt(ctx)

    abstraction: str | None = None
    model_used: str | None = None
    sampling_error: str | None = None

    if server_session is None:
        sampling_error = "no_server_session"
    else:
        try:
            client_params = getattr(server_session, "client_params", None)
            caps = (
                getattr(client_params, "capabilities", None) if client_params else None
            )
            sampling_cap = getattr(caps, "sampling", None) if caps else None
            if sampling_cap is None:
                sampling_error = "client_did_not_advertise_sampling"
            else:
                # Import SamplingMessage lazily so the CLI/sync path keeps
                # working when mcp isn't installed. Content is passed as a
                # dict (Pydantic validates into the discriminated union)
                # so we don't need a working TextContent class at runtime —
                # matters under test environments that swap mcp.types
                # attributes for lightweight stubs.
                from mcp.types import SamplingMessage

                result = await server_session.create_message(
                    messages=[
                        SamplingMessage(
                            role="user",
                            content={"type": "text", "text": prompt},
                        )
                    ],
                    max_tokens=2000,
                    temperature=0.7,
                    metadata={"codevira_purpose": "reflect"},
                )
                content = getattr(result, "content", None)
                text = getattr(content, "text", None) if content is not None else None
                if isinstance(text, str) and text.strip():
                    abstraction = text
                    model_used = getattr(result, "model", None)
                else:
                    sampling_error = "empty_or_non_text_response"
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            sampling_error = f"{type(exc).__name__}:{str(exc)[:160]}"

    if abstraction is None:
        return _stub_response(
            ctx=ctx,
            prompt=prompt,
            period_days=period_days,
            dry_run=dry_run,
            sampling_error=sampling_error,
        )

    reflection_id: str | None = None
    persisted = False
    if not dry_run:
        reflection_id = reflections_store.append(
            abstraction=abstraction,
            confidence=None,
            tags=[],
            period_start=ctx["period_start"],
            period_end=ctx["period_end"],
            source_session_ids=ctx["source_session_ids"],
            source_decision_ids=ctx["source_decision_ids"],
            model_used=model_used or "host-llm",
        )
        persisted = True

    return {
        "sampling_supported": True,
        "abstraction": abstraction,
        "model_used": model_used,
        "reflection_id": reflection_id,
        "persisted": persisted,
        "period_days": period_days,
        "period_start": ctx["period_start"],
        "period_end": ctx["period_end"],
        "source_context": _source_context_summary(ctx),
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
