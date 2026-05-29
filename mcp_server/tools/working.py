"""
working.py — v3.1.0 M2 Phase 2 MCP tools for working memory.

Exposes the working-memory store as four MCP tools:

  - working_add        — record an observation or goal.
  - working_get        — top-K live entries by decay score.
  - working_promote    — move an entry to long-term memory
                         (decision / skill / playbook) and tombstone
                         the source via amendment.
  - get_working_context — compact rendering for ReAct-loop injection.

Promotion paths in v3.1.0:

  - ``to="decision"`` — fully wired. Calls ``check_conflict`` first;
    on novel/forced write, ``decisions_store.record(...)`` lands the
    new decision id, then ``working_store.mark_promoted`` tombstones
    the source. Constraint: only ``kind="observation"`` entries
    promote to decisions cleanly (observations are facts; goals are
    intents — see ``to="skill"`` for the latter).
  - ``to="skill"`` — deferred to M3 (skills_store doesn't exist yet
    in v3.1.0 Phase 2). Returns ``{deferred: True}``; the API surface
    is reserved so callers don't need a second pass when M3 lands.
  - ``to="playbook"`` — deferred to a later v3.1.x. The existing
    playbook resolution chain (``mcp_server/tools/playbook.py``) reads
    markdown from ``.codevira/playbooks/<task_type>/<name>.md``; the
    mapping from a working-memory entry to a task_type + filename
    needs more design before we wire it.
"""

from __future__ import annotations

from typing import Any

from mcp_server.storage import working_store


# Promotion targets recognised by ``working_promote``.
_PROMOTE_DECISION = "decision"
_PROMOTE_SKILL = "skill"
_PROMOTE_PLAYBOOK = "playbook"
_VALID_PROMOTE_TARGETS = frozenset(
    {_PROMOTE_DECISION, _PROMOTE_SKILL, _PROMOTE_PLAYBOOK}
)


# ──────────────────────────────────────────────────────────────────────
# working_add
# ──────────────────────────────────────────────────────────────────────


def working_add(
    content: str,
    *,
    kind: str = "observation",
    importance: int = 5,
    confidence: float | None = None,
    links: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record one working-memory entry.

    Returns ``{recorded, entry_id, kind, [hint]}`` or
    ``{recorded: False, error: ...}``.

    Validation errors from ``working_store.add`` surface as
    structured failures — the agent should fix the input and retry,
    not have its tool call crash the dispatcher.
    """
    try:
        wid = working_store.add(
            content,
            kind=kind,
            importance=importance,
            confidence=confidence,
            links=links,
            session_id=session_id,
        )
    except ValueError as exc:
        return {"recorded": False, "error": str(exc)}

    return {
        "recorded": True,
        "entry_id": wid,
        "kind": kind,
        "hint": (
            "Use working_get(top_k=N) to see the current scratchpad, "
            "or working_promote(entry_id, to='decision', ...) to move "
            "this into long-term memory."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# working_get / get_working_context
# ──────────────────────────────────────────────────────────────────────


def working_get(
    *,
    top_k: int = 10,
    kind: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Top-K live entries ranked by decay score.

    ``kind`` filters to ``observation`` or ``goal`` if set. The
    returned ``entries`` are sorted highest-score first.
    """
    entries = working_store.list_top_k(top_k=top_k, kind=kind, session_id=session_id)
    return {
        "entries": [
            {
                "entry_id": e["id"],
                "kind": e.get("kind"),
                "content": e.get("content"),
                "importance": e.get("importance"),
                "confidence": e.get("confidence"),
                "links": e.get("links") or [],
                "ts": e.get("ts"),
                "session_id": e.get("session_id"),
            }
            for e in entries
        ],
        "count": len(entries),
        "filtered_by": {"kind": kind, "session_id": session_id},
    }


def get_working_context(*, top_k: int = 5) -> dict[str, Any]:
    """Compact rendering of the working scratchpad for ReAct loops.

    Returns a single ``markdown`` string suitable for injecting into
    the agent's next prompt + a structured ``entries`` view for tools
    that prefer the data shape. Designed for the get_session_context
    panel in M2 Phase 3 — capped at ~150 tokens of output.
    """
    entries = working_store.list_top_k(top_k=top_k)
    if not entries:
        return {
            "markdown": "_(working memory empty)_",
            "entries": [],
            "count": 0,
        }

    lines = ["### Working memory (top-{}):".format(min(top_k, len(entries)))]
    for e in entries:
        prefix = "•" if e.get("kind") == "observation" else "→"
        # Keep each entry tight (~30 tokens). Truncate content at 120
        # chars so a single 2 KB entry can't blow the panel.
        content = e.get("content") or ""
        if len(content) > 120:
            content = content[:117] + "..."
        lines.append(
            f"{prefix} {content}  _({e['id']}, importance={e.get('importance')})_"
        )
    return {
        "markdown": "\n".join(lines),
        "entries": [
            {
                "entry_id": e["id"],
                "kind": e.get("kind"),
                "content": e.get("content"),
                "importance": e.get("importance"),
            }
            for e in entries
        ],
        "count": len(entries),
    }


# ──────────────────────────────────────────────────────────────────────
# working_promote
# ──────────────────────────────────────────────────────────────────────


def working_promote(
    entry_id: str,
    *,
    to: str = _PROMOTE_DECISION,
    file_path: str | None = None,
    context: str | None = None,
    do_not_revert: bool = False,
    tags: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Promote a working-memory entry to long-term memory.

    Workflow (for ``to="decision"``):
      1. Resolve the source entry; reject if missing or tombstoned.
      2. Run ``check_conflict`` on the content. If conflict and not
         ``force``, return a warning — caller decides whether to retry
         with ``force=True``.
      3. Call ``decisions_store.record(...)`` with the entry's content.
      4. Call ``working_store.mark_promoted(entry_id, target_id)`` to
         tombstone the source.

    ``to="skill"`` and ``to="playbook"`` are deferred to M3+/v3.1.x.
    """
    if to not in _VALID_PROMOTE_TARGETS:
        return {
            "promoted": False,
            "error": (
                f"working_promote: 'to' must be one of "
                f"{sorted(_VALID_PROMOTE_TARGETS)}; got {to!r}"
            ),
        }

    source = working_store.get(entry_id)
    if source is None:
        return {
            "promoted": False,
            "error": f"working_promote: entry {entry_id!r} not found",
        }

    # Tombstoned entries cannot be re-promoted. ``working_store.get``
    # returns the merged base; we need a separate liveness check.
    if entry_id in working_store._tombstoned_ids():
        return {
            "promoted": False,
            "error": (
                f"working_promote: entry {entry_id!r} has already been "
                f"tombstoned (evicted or promoted)."
            ),
        }

    if to == _PROMOTE_SKILL:
        return {
            "promoted": False,
            "deferred": True,
            "milestone": "M3",
            "hint": (
                "Skill promotion lands in v3.1.0 M3 (skills_store). "
                "The API surface is reserved; no caller-side change "
                "needed when M3 ships."
            ),
        }
    if to == _PROMOTE_PLAYBOOK:
        return {
            "promoted": False,
            "deferred": True,
            "milestone": "v3.1.x",
            "hint": (
                "Playbook promotion needs a working-memory→task_type "
                "mapping that's still being designed. The existing "
                "playbook resolution chain (mcp_server/tools/playbook.py) "
                "reads markdown from .codevira/playbooks/<task_type>/."
            ),
        }

    # to == "decision" — the fully wired path.
    content = source.get("content") or ""
    if source.get("kind") == "goal":
        # Goals are intents, not facts. We allow promotion but flag it
        # in the response so the agent knows the LTM record will read
        # as a doctrine-style note ("we want to do X") rather than a
        # decided fact.
        intent_hint = (
            "Note: promoting a 'goal' entry to a decision turns an "
            "in-flight intent into a recorded decision. Consider "
            "whether the goal is actually settled before doing this."
        )
    else:
        intent_hint = None

    # Lazy imports to keep working.py free of LTM imports at module
    # load time (helps the test harness mock).
    from mcp_server.storage import decisions_store
    from mcp_server.tools.check_conflict import check_conflict

    conflict_warning = None
    if not force:
        try:
            check = check_conflict(decision_text=content, file_path=file_path)
            conflicts = check.get("conflicts") or []
            duplicates = check.get("duplicates") or []
            if conflicts:
                conflict_warning = {
                    "kind": "conflict",
                    "message": (
                        f"Promoting this entry would create a decision that "
                        f"conflicts with {len(conflicts)} protected (do_not_revert=True) "
                        f"decision(s). Pass force=True to record anyway, or use "
                        f"supersede_decision(old_id, new_decision, reason) to "
                        f"explicitly retire the prior one."
                    ),
                    "conflicting_decision_ids": [
                        c.get("decision_id") for c in conflicts
                    ],
                }
            elif duplicates:
                conflict_warning = {
                    "kind": "duplicate",
                    "message": (
                        f"Promoting this entry would create a near-duplicate of "
                        f"{len(duplicates)} existing decision(s). Pass force=True "
                        f"to record anyway. Existing ids: "
                        f"{[d.get('decision_id') for d in duplicates]}."
                    ),
                    "duplicate_decision_ids": [
                        d.get("decision_id") for d in duplicates
                    ],
                }
        except Exception:  # noqa: BLE001 — P9 fail-open
            pass

    if conflict_warning and not force:
        return {
            "promoted": False,
            "entry_id": entry_id,
            "to": to,
            "_conflict_warning": conflict_warning,
        }

    # Carry forward links from the working entry as references.
    promotion_context = context
    if source.get("links"):
        link_note = "Promoted from working memory entry " + entry_id
        if source.get("links"):
            link_note += " (refs: " + ", ".join(source["links"]) + ")"
        promotion_context = (
            (promotion_context + "\n\n" + link_note) if promotion_context else link_note
        )

    new_id = decisions_store.record(
        decision=content,
        file_path=file_path,
        context=promotion_context,
        do_not_revert=bool(do_not_revert),
        session_id=source.get("session_id"),
        tags=tags,
    )

    working_store.mark_promoted(entry_id, target_id=new_id)

    response: dict[str, Any] = {
        "promoted": True,
        "entry_id": entry_id,
        "to": to,
        "target_id": new_id,
        "hint": (
            "Working entry tombstoned; future working_get calls will "
            "no longer surface it. The LTM record is now searchable "
            "via search_decisions / list_decisions."
        ),
    }
    if intent_hint:
        response["_intent_note"] = intent_hint
    return response
