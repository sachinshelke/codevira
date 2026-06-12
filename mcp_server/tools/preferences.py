"""
preferences.py — v3.3.0 Phase 4: preference distillation + retrieval.

Second half of the preference-capture loop (decision D0000LU):

  - distill_preferences — session-end entry. Feeds the captured prompts
    (``.codevira-cache/prompts.jsonl``, written by the prompt_capture
    policy) to the host LLM via MCP ``sampling/createMessage`` — same
    integration reflect_async uses — and upserts the extracted
    preferences into ``~/.codevira/global.db`` ``global_preferences``
    (category-tagged, user-scoped, cross-project). On success the
    capture file is cleared. Degrades to a rendered_prompt stub when
    sampling is unavailable.

  - search_preferences — retrieval by category. CLAUDE.md documented
    this tool for several versions while it didn't exist; this closes
    that gap, backed by global_preferences.

Storage note: global_preferences is the dormant table the v2.2.0
surface-cut audit left behind when the noisy frequency-counted learning
was deleted. Reviving the TABLE, not the old mechanism — rows here are
LLM-distilled with explicit categories, and ``codevira export setup``
already transfers them across machines.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_DISTILL_PROMPT_TEMPLATE = """\
You are analyzing a developer's raw prompts to an AI coding assistant,
captured across recent sessions in one project. Extract DURABLE USER
PREFERENCES: instructions about how the user wants the assistant to
communicate or work that would apply beyond the immediate task.

Look for repeated or emphatic signals — communication style ("keep
answers short"), workflow habits ("always run tests first"), formatting
wishes, pet peeves. Ignore one-off task requests, questions, and
anything project-specific that wouldn't transfer to another repo.

Respond with ONLY a JSON array (no prose, no code fences). Each element:
  {{"category": "communication" | "workflow" | "formatting",
    "signal": "<the preference, imperative, max 12 words>",
    "example": "<short quote from a prompt that evidences it>"}}

Return [] if no durable preferences are evident. Do not invent
preferences from weak evidence — precision over recall.

Captured prompts (oldest first):
{prompts_block}
"""


def _render_distill_prompt(prompts: list[dict[str, Any]]) -> str:
    lines = [f"- {p.get('prompt', '')}" for p in prompts]
    return _DISTILL_PROMPT_TEMPLATE.format(prompts_block="\n".join(lines))


def _parse_preferences(text: str) -> list[dict[str, str]]:
    """Parse the LLM response defensively. Returns [] on garbage."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or "").strip()
        if not signal:
            continue
        out.append(
            {
                "category": str(item.get("category") or "communication").strip()
                or "communication",
                "signal": signal[:200],
                "example": str(item.get("example") or "")[:300],
            }
        )
    return out


def _stub_response(
    *, prompt: str, pending: int, dry_run: bool, sampling_error: str | None
) -> dict[str, Any]:
    return {
        "sampling_supported": False,
        "sampling_error": sampling_error,
        "pending_prompts": pending,
        "rendered_prompt": prompt,
        "dry_run": bool(dry_run),
        "hint": (
            "Sampling unavailable — feed rendered_prompt to your own LLM "
            "and record the results with record_decision, or retry from an "
            "MCP host that advertises sampling capability."
        ),
    }


async def distill_preferences_async(
    *,
    dry_run: bool = True,
    server_session: Any = None,
) -> dict[str, Any]:
    """Distill captured prompts into global preferences via the host LLM.

    Mirrors reflect_async's sampling integration: advertise-check →
    create_message → graceful stub on any failure. With dry_run=False
    and a successful parse, preferences are upserted into
    global_preferences and the capture file is cleared.
    """
    from mcp_server.engine.policies.prompt_capture import (
        clear_pending,
        read_pending,
    )
    from mcp_server.paths import get_project_root

    project_root = get_project_root()
    prompts = read_pending(project_root)
    if not prompts:
        return {
            "sampling_supported": None,
            "pending_prompts": 0,
            "preferences_extracted": 0,
            "hint": "No captured prompts to distill — nothing to do.",
            "dry_run": bool(dry_run),
        }

    prompt = _render_distill_prompt(prompts)

    raw_text: str | None = None
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
            if getattr(caps, "sampling", None) is None:
                sampling_error = "client_did_not_advertise_sampling"
            else:
                from mcp.types import SamplingMessage

                result = await server_session.create_message(
                    messages=[
                        SamplingMessage(
                            role="user",
                            content={"type": "text", "text": prompt},
                        )
                    ],
                    max_tokens=1000,
                    temperature=0.2,
                    metadata={"codevira_purpose": "distill_preferences"},
                )
                content = getattr(result, "content", None)
                text = getattr(content, "text", None) if content is not None else None
                if isinstance(text, str) and text.strip():
                    raw_text = text
                    model_used = getattr(result, "model", None)
                else:
                    sampling_error = "empty_or_non_text_response"
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            sampling_error = f"{type(exc).__name__}:{str(exc)[:160]}"

    if raw_text is None:
        return _stub_response(
            prompt=prompt,
            pending=len(prompts),
            dry_run=dry_run,
            sampling_error=sampling_error,
        )

    extracted = _parse_preferences(raw_text)
    persisted = 0
    if extracted and not dry_run:
        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_db_path

        db = GlobalDB(get_global_db_path())
        try:
            for pref in extracted:
                db.upsert_preference(
                    category=pref["category"],
                    signal=pref["signal"],
                    example=pref["example"] or None,
                    source_project=project_root.name,
                    frequency=1,
                )
                persisted += 1
        finally:
            db.close()
        clear_pending(project_root)

    return {
        "sampling_supported": True,
        "model_used": model_used,
        "pending_prompts": len(prompts),
        "preferences_extracted": len(extracted),
        "preferences_persisted": persisted,
        "capture_file_cleared": bool(persisted),
        "preferences": extracted,
        "dry_run": bool(dry_run),
    }


def search_preferences(
    *,
    category: str | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """Search learned user preferences by category (global, cross-project).

    Returns highest-frequency preferences first. Categories in use:
    'communication', 'workflow', 'formatting' (LLM-distilled), plus any
    legacy categories from older codevira versions.
    """
    from mcp_server.paths import get_global_db_path

    db_path = get_global_db_path()
    if not db_path.is_file():
        return {
            "count": 0,
            "preferences": [],
            "hint": (
                "No global.db yet — preferences appear after the first "
                "distill_preferences run (or codevira import)."
            ),
        }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if category:
                rows = conn.execute(
                    "SELECT category, signal, example, frequency, updated_at "
                    "FROM global_preferences WHERE category = ? "
                    "ORDER BY frequency DESC, updated_at DESC LIMIT ?",
                    (category, max(1, min(int(top_k), 100))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT category, signal, example, frequency, updated_at "
                    "FROM global_preferences "
                    "ORDER BY frequency DESC, updated_at DESC LIMIT ?",
                    (max(1, min(int(top_k), 100)),),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "count": 0,
            "preferences": [],
            "error": f"global.db unreadable: {exc}",
            "fix_command": "codevira doctor",
        }
    return {
        "count": len(rows),
        "preferences": [dict(r) for r in rows],
        "filtered_by": {"category": category},
    }
