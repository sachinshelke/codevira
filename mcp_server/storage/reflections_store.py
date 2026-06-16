"""
reflections_store.py — v3.1.0 M8: durable LLM abstractions.

Reflections are Generative-Agents-style abstractions over recent
episodic memory. The agent (or a CLI invocation) periodically asks
the host LLM (via MCP ``sampling/createMessage``) to synthesize the
pattern in recent decisions + sessions; the result lands here as a
durable semantic artifact the next agent can consult on
get_session_context.

# Why a separate store

- **Canonical**: lives in ``.codevira/reflections.jsonl`` — committed
  to the repo because reflections survive sessions and inform
  teammates (not scratchpad).
- **Sanitized inputs**: before the LLM ever sees the source records,
  we strip secrets (API keys, Bearer tokens, AWS-style AKIA, password
  fields, long hex/base64 blobs) so reflections can't accidentally
  encode credentials.
- **Cap-then-sample**: hard caps on input size (≤30 sessions + ≤100
  decisions per reflection; ~6 KB input envelope) so a giant project
  history doesn't blow the LLM's context budget.

# Schema

::

    {
      "id":                  "R000001",
      "ts":                  "2026-05-28T10:00:00+00:00",
      "period_start":        "2026-05-21T00:00:00+00:00",
      "period_end":          "2026-05-28T00:00:00+00:00",
      "source_session_ids":  ["sess-abc", "sess-def"],
      "source_decision_ids": ["D000123", "D000124"],
      "abstraction":         "<≤ 4 KB markdown from the LLM>",
      "confidence":          0.0-1.0,
      "tags":                ["release", "auth"],
      "model_used":          "<model id reported by client>",
      "origin":              {ide, agent_model, host_hash, ts},
      "_schema_v":           1,
    }

# Sampling integration scope (v3.1.0 vs v3.2)

The MCP ``sampling/createMessage`` request is what asks the connected
client (Claude Code / Claude Desktop) to invoke its LLM on a prompt.
v3.1.0 ships the storage + sanitization + prompt-template + the API
surface; the *actual* sampling call is stubbed (``reflect()`` returns
``{sampling_unsupported: True, deferred_to: "v3.2"}``). When v3.2 wires
the live sampling RPC through, the existing tests and CLI flow stay
the same — only the inner call swaps from stub to real.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import jsonl_store, origin as origin_module, paths

logger = logging.getLogger(__name__)

SCHEMA_V = 1

# Caps from the plan.
MAX_SESSIONS_PER_REFLECTION = 30
MAX_DECISIONS_PER_REFLECTION = 100
MAX_INPUT_BYTES = 6 * 1024  # 6 KB cap on the source context envelope


# ──────────────────────────────────────────────────────────────────────
# Sanitization
# ──────────────────────────────────────────────────────────────────────


# Re-export from the shared sanitize module so the public surface
# (``reflections_store.scrub_sensitive``, ``_SECRET_PATTERNS``) stays
# stable for existing callers + tests.
from mcp_server.storage.sanitize import (  # noqa: E402
    scrub_sensitive,
)


# ──────────────────────────────────────────────────────────────────────
# Source context aggregation
# ──────────────────────────────────────────────────────────────────────


def build_source_context(
    *,
    period_days: int = 7,
    now: datetime | None = None,
    include_transcripts: bool = False,
    project_root: Any | None = None,
) -> dict[str, Any]:
    """Aggregate recent sessions + decisions for the reflection prompt.

    Applies the plan's caps (≤30 sessions, ≤100 decisions, ≤6 KB
    serialized envelope) and runs ``scrub_sensitive`` over every text
    field that could carry a secret. Returns ``{period_start,
    period_end, sessions, decisions, source_session_ids,
    source_decision_ids, envelope_bytes}``.

    E2 (Phase 20): when ``include_transcripts`` is True, a READ-ONLY scan of
    local AI-IDE session logs (Claude Code / Codex / Gemini) is folded in as
    ``transcript_signals`` — sanitized, capped failure/correction signals.
    Best-effort: a scan failure leaves the field empty and never breaks the
    reflect call. This feeds CANDIDATES only — nothing is auto-committed.
    """
    now_dt = now or datetime.now(timezone.utc)
    from datetime import timedelta

    period_start = now_dt - timedelta(days=max(period_days, 1))

    raw_sessions = jsonl_store.read_recent(
        paths.sessions_path(), limit=MAX_SESSIONS_PER_REFLECTION * 2
    )
    sessions: list[dict[str, Any]] = []
    for s in raw_sessions:
        if s.get("_amendment_to_id"):
            continue
        ts_str = s.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < period_start or ts > now_dt:
                continue
        except (ValueError, TypeError):
            continue
        sessions.append(s)
        if len(sessions) >= MAX_SESSIONS_PER_REFLECTION:
            break

    raw_decisions = jsonl_store.read_recent(
        paths.decisions_path(), limit=MAX_DECISIONS_PER_REFLECTION * 2
    )
    decisions: list[dict[str, Any]] = []
    for d in raw_decisions:
        if d.get("_amendment_to_id"):
            continue
        ts_str = d.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < period_start or ts > now_dt:
                continue
        except (ValueError, TypeError):
            continue
        decisions.append(d)
        if len(decisions) >= MAX_DECISIONS_PER_REFLECTION:
            break

    # Sanitize narrative fields before they hit the prompt.
    sanitized_sessions = [
        {
            "session_id": s.get("session_id"),
            "task": scrub_sensitive(s.get("task") or ""),
            "task_type": s.get("task_type"),
            "summary": scrub_sensitive(s.get("summary") or ""),
            "outcome": s.get("outcome"),
        }
        for s in sessions
    ]
    sanitized_decisions = [
        {
            "id": d.get("id"),
            "decision": scrub_sensitive(d.get("decision") or ""),
            "context": scrub_sensitive(d.get("context") or ""),
            "file_path": d.get("file_path"),
            "tags": d.get("tags") or [],
        }
        for d in decisions
    ]

    # Envelope-size enforcement: trim from the *oldest* end first if
    # we exceed the cap. A clipped reflection is fine; a reflection
    # that blew the LLM's context isn't.
    def _serialize_size(sess: list[dict], dec: list[dict]) -> int:
        return len(repr({"s": sess, "d": dec}).encode("utf-8"))

    while _serialize_size(
        sanitized_sessions, sanitized_decisions
    ) > MAX_INPUT_BYTES and (sanitized_sessions or sanitized_decisions):
        if len(sanitized_decisions) >= len(sanitized_sessions):
            sanitized_decisions.pop()  # drop oldest in iteration order
        else:
            sanitized_sessions.pop()

    # E2: optional READ-ONLY transcript scan. Excerpts are already scrubbed
    # at parse time; capped per-session + per-scan. Best-effort by design.
    transcript_signals: list[dict[str, Any]] = []
    if include_transcripts:
        try:
            from mcp_server.ingest import scan_sessions, to_reflection_signals

            root = project_root
            if root is None:
                from mcp_server.paths import get_project_root

                root = get_project_root()
            digests = scan_sessions(
                root, since_days=max(period_days, 1), max_sessions=12
            )
            transcript_signals = to_reflection_signals(digests)
        except Exception:  # noqa: BLE001 — scan must never break reflect
            transcript_signals = []

    return {
        "period_start": period_start.isoformat(),
        "period_end": now_dt.isoformat(),
        "sessions": sanitized_sessions,
        "decisions": sanitized_decisions,
        "transcript_signals": transcript_signals,
        "source_session_ids": [
            str(s.get("session_id") or "")
            for s in sanitized_sessions
            if s.get("session_id")
        ],
        "source_decision_ids": [
            str(d.get("id") or "") for d in sanitized_decisions if d.get("id")
        ],
        "envelope_bytes": _serialize_size(sanitized_sessions, sanitized_decisions),
    }


def render_prompt(source_context: dict[str, Any]) -> str:
    """Inline the source context into the bundled prompt template.

    The placeholder ``<<<SOURCE_CONTEXT>>>`` in
    ``mcp_server/data/prompts/reflection_v1.md`` is replaced with a
    deterministic YAML-ish rendering of the aggregated sessions +
    decisions. Falls back to a minimal inline template if the bundled
    file is missing (defensive — shouldn't happen with package-data).
    """
    template_path = paths.reflection_prompt_path()
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "reflections_store.render_prompt: bundled template missing "
            "(%s); using minimal fallback",
            exc,
        )
        template = (
            "Reflect on the project's recent decisions and sessions. "
            "Output a YAML block with abstraction, tags, confidence.\n"
            "<<<SOURCE_CONTEXT>>>"
        )

    return template.replace(
        "<<<SOURCE_CONTEXT>>>", _render_context_block(source_context)
    )


def _render_context_block(ctx: dict[str, Any]) -> str:
    lines: list[str] = [
        f"period_start: {ctx.get('period_start')}",
        f"period_end:   {ctx.get('period_end')}",
        "",
        "sessions:",
    ]
    for s in ctx.get("sessions") or []:
        lines.append(
            f"  - session_id: {s.get('session_id')!r}"
            f"  task_type: {s.get('task_type')!r}"
        )
        if s.get("task"):
            lines.append(f"    task: {s['task']}")
        if s.get("summary"):
            lines.append(f"    summary: {s['summary']}")
    lines.append("")
    lines.append("decisions:")
    for d in ctx.get("decisions") or []:
        tags = ", ".join(d.get("tags") or [])
        lines.append(
            f"  - id: {d.get('id')!r}  tags: [{tags}]  file: {d.get('file_path')!r}"
        )
        if d.get("decision"):
            lines.append(f"    decision: {d['decision']}")

    # E2 (Phase 20): heuristic signals mined from recent IDE session
    # transcripts — tool FAILURES and user CORRECTIONS worth reflecting on.
    signals = ctx.get("transcript_signals") or []
    if signals:
        lines.append("")
        lines.append("session_transcript_signals:")
        for sig in signals:
            lines.append(
                f"  - source: {sig.get('source')!r}  tool_calls: {sig.get('tool_calls')}"
            )
            for f in sig.get("failures") or []:
                lines.append(f"    failed[{f.get('tool')}]: {f.get('error')}")
            for c in sig.get("corrections") or []:
                lines.append(
                    f"    user_correction (after {c.get('after_tool')!r}): {c.get('said')}"
                )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Writes / Reads
# ──────────────────────────────────────────────────────────────────────


def append(
    *,
    abstraction: str,
    confidence: float | None,
    tags: list[str],
    period_start: str,
    period_end: str,
    source_session_ids: list[str],
    source_decision_ids: list[str],
    model_used: str | None = None,
    target: str = "reflections",
) -> str:
    """Persist a finalized reflection (target='reflections') or a
    pending proposal (target='proposals'). Returns the R-id.
    """
    paths.ensure_dirs()
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "period_start": period_start,
        "period_end": period_end,
        "source_session_ids": list(source_session_ids or []),
        "source_decision_ids": list(source_decision_ids or []),
        "abstraction": (abstraction or "").strip(),
        "confidence": (
            float(confidence) if isinstance(confidence, (int, float)) else None
        ),
        "tags": [str(t).strip().lower() for t in (tags or []) if str(t).strip()],
        "model_used": model_used,
        "origin": origin_module.current_origin(),
        "_schema_v": SCHEMA_V,
    }
    dest = (
        paths.reflections_path()
        if target == "reflections"
        else paths.reflection_proposals_path()
    )
    return jsonl_store.append_with_generated_id(dest, rec, prefix="R", width=6)


def list_recent(*, limit: int = 5, target: str = "reflections") -> list[dict[str, Any]]:
    """Return the most recent reflections (or proposals)."""
    dest = (
        paths.reflections_path()
        if target == "reflections"
        else paths.reflection_proposals_path()
    )
    return jsonl_store.read_recent(dest, limit=limit)


def list_filtered(
    *,
    target: str = "reflections",
    since: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return reflections filtered by since (ISO ts) and tags
    intersection (every requested tag must be present)."""
    rows = list_recent(target=target, limit=limit * 4)
    norm_tags = (
        {str(t).strip().lower() for t in tags if str(t).strip()} if tags else None
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if since and (r.get("ts") or "") < since:
            continue
        if norm_tags:
            row_tags = {
                str(t).strip().lower() for t in (r.get("tags") or []) if str(t).strip()
            }
            if not norm_tags.issubset(row_tags):
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out
