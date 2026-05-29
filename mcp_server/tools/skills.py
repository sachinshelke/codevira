"""
skills.py — v3.1.0 M3 Phase 2 MCP tools for the skill library.

Six tools cover the agent-facing surface:

  - record_skill        — author a new skill.
  - get_skill           — composite-ranked search (BM25 + tags + recency).
  - apply_skill_outcome — manual reinforcement (success / failure).
                          Canonical reinforcement comes from M5's
                          outcomes_writer integration; this tool is
                          the override path.
  - list_skills         — filtered list (status / source / tags).
  - supersede_skill     — version a skill, preserving audit chain.
  - promote_skill_to_playbook
                        — write the skill's procedure as markdown to
                          .codevira/playbooks/<task_type>/<name>.md.
                          Refuses on existing file unless force=True.

Each tool returns a structured ``dict`` (never raises). Validation
errors from the storage layer surface as ``{success/recorded: False,
error: ...}`` so the agent can correct the input and retry without
crashing the dispatcher.
"""

from __future__ import annotations

import re
from typing import Any

from mcp_server.storage import skills_store


# Filesystem-safe slug pattern for promotion to playbook.
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ──────────────────────────────────────────────────────────────────────
# record_skill
# ──────────────────────────────────────────────────────────────────────


def record_skill(
    name: str,
    procedure: str,
    *,
    summary: str | None = None,
    triggers: dict[str, list[str]] | None = None,
    source: str = "explicit",
    do_not_revert: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Record a new skill in the canonical store.

    Runs ``check_conflict`` against the SKILLS corpus first (not
    decisions) so re-recording a near-duplicate surfaces a warning.
    Pass ``force=True`` to record anyway.
    """
    if not isinstance(name, str) or not name.strip():
        return {"recorded": False, "error": "name must be a non-empty string"}
    if not isinstance(procedure, str) or not procedure.strip():
        return {"recorded": False, "error": "procedure must be a non-empty string"}

    # Conflict check: search the existing skill corpus for near-matches
    # by procedure text. The skills FTS5 corpus is the natural index.
    # We surface a warning but don't auto-block — agents may legitimately
    # want a parallel skill (e.g., language-specific variants).
    conflict_warning: dict[str, Any] | None = None
    if not force:
        try:
            hits = skills_store.search(f"{name} {procedure[:200]}", top_k=3)
            # If any hit has a BM25 component above an "obvious dup"
            # threshold, surface it. 0.85 in [0,1] is the rough
            # "near-identical text" line.
            obvious_dups = [
                h
                for h in hits
                if h.get("score_breakdown", {}).get("bm25_norm", 0.0) >= 0.85
            ]
            if obvious_dups:
                conflict_warning = {
                    "kind": "duplicate",
                    "message": (
                        f"This skill text looks similar to "
                        f"{len(obvious_dups)} existing skill(s). Pass "
                        f"force=True to record anyway, or supersede the "
                        f"existing skill via supersede_skill(old_id, ...)."
                    ),
                    "candidate_skill_ids": [h["id"] for h in obvious_dups],
                }
        except Exception:  # noqa: BLE001 — P9: never block writes
            pass

    if conflict_warning and not force:
        return {"recorded": False, "_conflict_warning": conflict_warning}

    try:
        kid = skills_store.record(
            name=name,
            procedure=procedure,
            summary=summary,
            triggers=triggers,
            source=source,
            do_not_revert=do_not_revert,
        )
    except ValueError as exc:
        return {"recorded": False, "error": str(exc)}

    return {
        "recorded": True,
        "skill_id": kid,
        "name": name.strip(),
        "do_not_revert": bool(do_not_revert),
        "hint": (
            "Use get_skill(query=...) to retrieve. apply_skill_outcome "
            "tracks success/failure for the auto-archive sweep."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# get_skill
# ──────────────────────────────────────────────────────────────────────


def get_skill(
    query: str,
    *,
    top_k: int = 5,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Composite-ranked search over active skills.

    Returns a ``hits`` list where each entry includes ``score`` (the
    composite) + ``score_breakdown`` (the three component scores)
    so the agent can reason about WHY a skill was surfaced.
    """
    if not isinstance(query, str) or not query.strip():
        return {"hits": [], "count": 0, "query": query}

    hits = skills_store.search(query, top_k=top_k, file_path=file_path)
    return {
        "hits": [
            {
                "skill_id": h.get("id"),
                "name": h.get("name"),
                "summary": h.get("summary"),
                "procedure": h.get("procedure"),
                "triggers": h.get("triggers"),
                "do_not_revert": h.get("do_not_revert"),
                "score": h.get("score"),
                "score_breakdown": h.get("score_breakdown"),
                "snippet": h.get("snippet"),
            }
            for h in hits
        ],
        "count": len(hits),
        "query": query,
        "file_path": file_path,
    }


# ──────────────────────────────────────────────────────────────────────
# apply_skill_outcome
# ──────────────────────────────────────────────────────────────────────


def apply_skill_outcome(skill_id: str, success: bool) -> dict[str, Any]:
    """Manually record one outcome for a skill.

    The canonical reinforcement loop in v3.1.0 M5 wires this through
    ``outcomes_writer.py`` so the success signal is git-derived rather
    than agent-self-reported. Until M5 ships, this tool IS the
    reinforcement loop — agents and humans use it directly.
    """
    if not isinstance(skill_id, str) or not skill_id.strip():
        return {"success": False, "error": "skill_id must be a non-empty string"}
    res = skills_store.mark_used(skill_id, success=bool(success))
    return res


# ──────────────────────────────────────────────────────────────────────
# list_skills
# ──────────────────────────────────────────────────────────────────────


def list_skills(
    *,
    status: str | None = "active",
    source: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List skills filtered by status / source / tags intersection.

    ``status="all"`` returns every state (active + archived +
    superseded); any other string filters to that one state. Default
    surfaces the daily-driver active set.
    """
    effective_status = None if status == "all" else status
    rows = skills_store.list_all(
        status=effective_status, source=source, tags=tags, limit=limit
    )
    return {
        "skills": [
            {
                "skill_id": s.get("id"),
                "name": s.get("name"),
                "summary": s.get("summary"),
                "status": s.get("status"),
                "source": s.get("source"),
                "do_not_revert": s.get("do_not_revert"),
                "success_count": s.get("success_count", 0),
                "failure_count": s.get("failure_count", 0),
                "consecutive_failures": s.get("consecutive_failures", 0),
                "last_used_at": s.get("last_used_at"),
                "triggers": s.get("triggers"),
            }
            for s in rows
        ],
        "count": len(rows),
        "filtered_by": {"status": status, "source": source, "tags": tags},
    }


# ──────────────────────────────────────────────────────────────────────
# supersede_skill
# ──────────────────────────────────────────────────────────────────────


def supersede_skill(
    old_id: str,
    *,
    name: str,
    procedure: str,
    summary: str | None = None,
    triggers: dict[str, list[str]] | None = None,
    reason: str = "",
    do_not_revert: bool = False,
) -> dict[str, Any]:
    """Replace an existing skill with a new version.

    Writes the new skill + amendment-marks the old as ``superseded``
    with a backref. Triggers inherit from the old skill when not
    supplied. The old skill never returns from search after this.
    """
    if not isinstance(old_id, str) or not old_id.strip():
        return {"success": False, "error": "old_id must be a non-empty string"}
    try:
        return skills_store.supersede(
            old_id,
            name=name,
            procedure=procedure,
            summary=summary,
            triggers=triggers,
            reason=reason,
            do_not_revert=do_not_revert,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────
# promote_skill_to_playbook
# ──────────────────────────────────────────────────────────────────────


def promote_skill_to_playbook(
    skill_id: str,
    *,
    task_type: str,
    name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write the skill's procedure as a playbook markdown file.

    Destination: ``<project>/.codevira/playbooks/<task_type>/<name>.md``
    where ``name`` defaults to the skill's name (slugified). The
    existing playbook resolution chain (``mcp_server/tools/playbook.py``)
    picks this up automatically — once promoted, the skill's procedure
    is also available via ``get_playbook(task_type=task_type)``.

    Refuses on existing file unless ``force=True`` so a teammate's
    hand-written playbook isn't clobbered silently. The source skill
    stays in the skill library (no automatic archive); humans manage
    versioning via ``supersede_skill`` if the playbook later
    supersedes the skill.
    """
    if not isinstance(skill_id, str) or not skill_id.strip():
        return {"promoted": False, "error": "skill_id must be a non-empty string"}
    if not isinstance(task_type, str) or not task_type.strip():
        return {"promoted": False, "error": "task_type must be a non-empty string"}

    skill = skills_store.get(skill_id)
    if skill is None:
        return {"promoted": False, "error": f"skill {skill_id} not found"}
    if skill.get("status") == "superseded":
        return {
            "promoted": False,
            "error": (
                f"skill {skill_id} is superseded; promote the successor "
                f"({skill.get('superseded_by')}) instead."
            ),
        }

    # Resolve the destination filename.
    effective_name = name or skill.get("name") or skill_id
    slug = _slugify(effective_name)
    if not _SAFE_NAME_RE.match(slug):
        return {
            "promoted": False,
            "error": (
                f"could not derive a filesystem-safe slug from "
                f"{effective_name!r}; pass an explicit `name` argument."
            ),
        }

    from mcp_server.storage import paths as _paths

    dest = _paths.codevira_dir() / "playbooks" / task_type / f"{slug}.md"

    if dest.exists() and not force:
        return {
            "promoted": False,
            "error": (
                f"playbook already exists at {dest.relative_to(_paths.codevira_dir().parent)} "
                f"— pass force=True to overwrite, or supply a different `name`."
            ),
            "existing_path": str(dest),
        }

    # Write the markdown. atomic_write_text covers crash-safety.
    from mcp_server.storage import atomic

    dest.parent.mkdir(parents=True, exist_ok=True)
    body = _render_playbook_markdown(skill, task_type=task_type)
    try:
        atomic.atomic_write_text(dest, body)
    except OSError as exc:
        return {
            "promoted": False,
            "error": f"could not write playbook at {dest}: {exc}",
        }

    return {
        "promoted": True,
        "skill_id": skill_id,
        "task_type": task_type,
        "path": str(dest),
        "name": slug,
        "hint": (
            f"The procedure is now also discoverable via "
            f"get_playbook(task_type={task_type!r}). The skill itself "
            f"stays in the library — supersede it via supersede_skill if "
            f"the playbook becomes the canonical version."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Lowercase + hyphenate; drop characters outside [a-z0-9_-]."""
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower().strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64]


def _render_playbook_markdown(skill: dict[str, Any], *, task_type: str) -> str:
    """Render a skill into a playbook .md file body.

    Includes a small header so the file announces its origin (skill_id
    + provenance) — useful when a teammate sees an unfamiliar playbook
    appear in git diff.
    """
    lines = [
        f"# {skill.get('name') or skill.get('id')}",
        "",
        f"_Promoted from skill {skill.get('id')} on "
        f"{__import__('datetime').datetime.now().date().isoformat()}_  ",
        f"_task_type: {task_type}_",
        "",
    ]
    summary = skill.get("summary")
    if summary:
        lines.append(f"> {summary}")
        lines.append("")
    procedure = (skill.get("procedure") or "").strip()
    if procedure:
        lines.append(procedure)
        lines.append("")
    return "\n".join(lines)
