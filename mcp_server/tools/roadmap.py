"""
MCP tools for reading and managing the project roadmap.

Full planning lifecycle:
  get_roadmap()              → session start orientation (compact)
  get_full_roadmap()         → complete picture for planning sessions
  get_phase(number)          → full details of any phase by number
  update_phase_status()      → mark current phase in_progress | blocked | pending
  add_phase()                → agents plan new upcoming work
  defer_phase()              → move an upcoming phase to deferred
  complete_phase()           → mark current phase done, advance to next
  update_next_action()       → update next_action at session end
  add_open_changeset()       → register active changeset in current phase
  remove_open_changeset()    → resolve changeset from current phase
"""
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from mcp_server.paths import get_data_dir, get_project_root


def _roadmap_file() -> Path:
    return get_data_dir() / "roadmap.yaml"


def _load_roadmap() -> dict:
    roadmap_file = _roadmap_file()
    if not roadmap_file.exists():
        stub = _create_stub_roadmap()
        _save_roadmap(stub)
        return stub
    with open(roadmap_file) as f:
        return yaml.safe_load(f) or {}


def _create_stub_roadmap() -> dict:
    return {
        "project": get_project_root().name,
        "version": "1.0",
        "current_phase": {
            "number": 1,
            "name": "Getting Started",
            "status": "pending",
            "next_action": (
                "Define your first phase: use add_phase() to queue upcoming work, "
                "or update_next_action() to describe what needs doing next."
            ),
            "open_changesets": [],
            "description": "Auto-generated stub — update this to reflect your project.",
        },
        "upcoming_phases": [],
        "deferred": [],
        "completed_phases": [],
    }


def _save_roadmap(data: dict) -> None:
    with open(_roadmap_file(), "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ─────────────────────────────────────────────
# READ TOOLS
# ─────────────────────────────────────────────

def get_roadmap() -> dict[str, Any]:
    """
    Return current project state: phase, next action, open changesets, upcoming work.
    Call this at the start of every session for quick orientation.

    Returns a compact summary — use get_full_roadmap() for planning sessions.
    """
    data = _load_roadmap()
    current = data.get("current_phase", {})
    upcoming = data.get("upcoming_phases", [])[:3]  # top 3 only

    return {
        "project": data.get("project", "My Project"),
        "version": data.get("version", "1.0"),
        "current_phase": {
            "number": current.get("number"),
            "name": current.get("name"),
            "status": current.get("status"),
            "next_action": current.get("next_action"),
            "open_changesets": current.get("open_changesets", []),
            "description": current.get("description", ""),
        },
        "upcoming": [
            {
                "phase": p.get("phase"),
                "name": p.get("name"),
                "priority": p.get("priority"),
                "depends_on": p.get("depends_on", []),
            }
            for p in upcoming
        ],
        "deferred_count": len(data.get("deferred", [])),
        "completed_phases_count": len(data.get("completed_phases", [])),
    }


def get_full_roadmap() -> dict[str, Any]:
    """
    Return the complete roadmap: all completed phases with decisions,
    current phase details, all upcoming phases, and all deferred items.

    Use this for planning sessions or when you need the full project history.
    More expensive than get_roadmap() — only call when you need the full picture.
    """
    data = _load_roadmap()
    return {
        "project": data.get("project"),
        "version": data.get("version"),
        "current_phase": data.get("current_phase", {}),
        "upcoming_phases": data.get("upcoming_phases", []),
        "deferred": data.get("deferred", []),
        "completed_phases": data.get("completed_phases", []),
        "summary": {
            "completed": len(data.get("completed_phases", [])),
            "upcoming": len(data.get("upcoming_phases", [])),
            "deferred": len(data.get("deferred", [])),
        },
    }


def get_phase(phase_number: int | str) -> dict[str, Any]:
    """
    Get full details of any phase by number — completed, current, or upcoming.

    Useful for understanding what was decided in a past phase, or inspecting
    a planned upcoming phase before starting it.

    Args:
        phase_number: Phase number (e.g. 19, "8R", "12A")

    Returns:
        Phase details including key_decisions (if completed), description, files, status.
    """
    data = _load_roadmap()
    pn = str(phase_number)

    # Check current phase
    current = data.get("current_phase", {})
    if str(current.get("number")) == pn:
        return {"found": True, "location": "current", "phase": current}

    # Check completed phases
    for p in data.get("completed_phases", []):
        if str(p.get("phase")) == pn:
            return {"found": True, "location": "completed", "phase": p}

    # Check upcoming phases
    for p in data.get("upcoming_phases", []):
        if str(p.get("phase")) == pn:
            return {"found": True, "location": "upcoming", "phase": p}

    return {
        "found": False,
        "message": f"Phase {phase_number} not found in roadmap.",
        "hint": "Use get_full_roadmap() to see all phases.",
    }


# ─────────────────────────────────────────────
# PLANNING TOOLS
# ─────────────────────────────────────────────

def add_phase(
    phase: int | str,
    name: str,
    description: str,
    priority: str = "medium",
    depends_on: list[int | str] | None = None,
    files: list[str] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """
    Add a new upcoming phase to the roadmap.

    Agents call this when they identify new work during a session —
    e.g., discovering a gap, a refactor need, or a follow-up phase.

    Args:
        phase: Phase number or label (e.g. 26, "26A")
        name: Short phase name (e.g. "Schema Versioning")
        description: What this phase does and why
        priority: high | medium | low
        depends_on: List of phase numbers that must complete first
        files: Key files that will be touched
        effort: Rough effort estimate (e.g. "~2 hours", "1 day")

    Returns:
        success, phase added, position in upcoming queue.
    """
    data = _load_roadmap()
    upcoming = data.get("upcoming_phases", [])

    # Check if phase number already exists
    existing_phases = {str(p.get("phase")) for p in upcoming}
    existing_phases.add(str(data.get("current_phase", {}).get("number")))
    for p in data.get("completed_phases", []):
        existing_phases.add(str(p.get("phase")))

    if str(phase) in existing_phases:
        return {
            "success": False,
            "message": f"Phase {phase} already exists in the roadmap.",
        }

    entry: dict[str, Any] = {
        "phase": phase,
        "name": name,
        "priority": priority,
        "depends_on": depends_on or [],
        "description": description,
    }
    if files:
        entry["files"] = files
    if effort:
        entry["effort"] = effort

    # Insert by priority: high → front, medium → after existing highs, low → end
    if priority == "high":
        insert_at = 0
        for i, p in enumerate(upcoming):
            if p.get("priority") == "high":
                insert_at = i + 1
        upcoming.insert(insert_at, entry)
    else:
        upcoming.append(entry)

    data["upcoming_phases"] = upcoming
    _save_roadmap(data)

    position = upcoming.index(entry) + 1
    return {
        "success": True,
        "phase": phase,
        "name": name,
        "position_in_queue": position,
        "total_upcoming": len(upcoming),
    }


def update_phase_status(
    status: str,
    blocker: str | None = None,
    started: str | None = None,
) -> dict[str, Any]:
    """
    Update the current phase's status.

    Args:
        status: pending | in_progress | blocked
        blocker: Required when status=blocked — describe what's blocking
        started: ISO date when work started (auto-fills today if status=in_progress)

    Returns:
        success, updated phase number, new status.
    """
    valid = {"pending", "in_progress", "blocked"}
    if status not in valid:
        return {"success": False, "message": f"Invalid status '{status}'. Must be one of: {sorted(valid)}"}

    if status == "blocked" and not blocker:
        return {"success": False, "message": "blocker description required when status=blocked"}

    data = _load_roadmap()
    current = data.get("current_phase", {})

    current["status"] = status
    if status == "blocked":
        current["blocker"] = blocker
    elif "blocker" in current:
        del current["blocker"]
    if status == "in_progress" and "started" not in current:
        current["started"] = started or date.today().isoformat()

    data["current_phase"] = current
    _save_roadmap(data)

    return {
        "success": True,
        "phase": current.get("number"),
        "name": current.get("name"),
        "status": status,
        "blocker": blocker,
    }


def defer_phase(
    phase_number: int | str,
    reason: str,
) -> dict[str, Any]:
    """
    Move an upcoming phase to the deferred list.

    Use when a phase depends on something not yet available, or when priorities
    shift and the work is genuinely not happening soon.

    Args:
        phase_number: Phase number to defer
        reason: Why this is being deferred (preserved for future context)

    Returns:
        success, phase name, reason recorded.
    """
    data = _load_roadmap()
    upcoming = data.get("upcoming_phases", [])

    target = None
    for i, p in enumerate(upcoming):
        if str(p.get("phase")) == str(phase_number):
            target = upcoming.pop(i)
            break

    if target is None:
        return {
            "success": False,
            "message": f"Phase {phase_number} not found in upcoming phases.",
            "hint": "Can only defer upcoming phases, not completed or current.",
        }

    deferred_entry = {
        "name": target.get("name"),
        "phase": target.get("phase"),
        "reason": reason,
        "deferred_date": date.today().isoformat(),
        "original_priority": target.get("priority"),
    }

    data["upcoming_phases"] = upcoming
    data.setdefault("deferred", []).append(deferred_entry)
    _save_roadmap(data)

    return {
        "success": True,
        "phase": phase_number,
        "name": target.get("name"),
        "reason": reason,
        "remaining_upcoming": len(upcoming),
    }


# ─────────────────────────────────────────────
# LIFECYCLE TOOLS
# ─────────────────────────────────────────────

def complete_phase(phase_number: int | str, key_decisions: list[str]) -> dict[str, Any]:
    """
    Mark the current phase as complete and advance to the next upcoming phase.

    Args:
        phase_number: Must match the current phase number (safety check)
        key_decisions: List of decisions made — preserved for all future agents

    Returns:
        success, completed phase, advanced_to phase number.
    """
    data = _load_roadmap()
    current = data.get("current_phase", {})

    if str(current.get("number")) != str(phase_number):
        return {
            "success": False,
            "message": f"Current phase is {current.get('number')}, not {phase_number}. Cannot complete.",
        }

    completed_entry = {
        "phase": current["number"],
        "name": current["name"],
        "completed": date.today().isoformat(),
        "key_decisions": key_decisions,
    }
    if current.get("started"):
        completed_entry["started"] = current["started"]

    data.setdefault("completed_phases", []).append(completed_entry)

    # Advance to next upcoming phase
    upcoming = data.get("upcoming_phases", [])
    if upcoming:
        next_phase = upcoming.pop(0)
        data["current_phase"] = {
            "number": next_phase["phase"],
            "name": next_phase["name"],
            "status": "pending",
            "next_action": f"Begin {next_phase['name']}: {next_phase.get('description', '')}".strip(": "),
            "open_changesets": [],
            "description": next_phase.get("description", ""),
        }
        data["upcoming_phases"] = upcoming
        advanced_to = data["current_phase"]["number"]
    else:
        data["current_phase"] = {
            "number": None,
            "name": "No upcoming phases",
            "status": "pending",
            "next_action": "Add new phases with add_phase() or plan the next milestone.",
            "open_changesets": [],
        }
        advanced_to = None

    _save_roadmap(data)
    return {
        "success": True,
        "completed_phase": phase_number,
        "key_decisions_recorded": len(key_decisions),
        "advanced_to": advanced_to,
    }


def update_next_action(next_action: str) -> dict[str, Any]:
    """
    Update the next_action field in the current phase.
    Call at session end — tells the next agent exactly where to pick up.
    """
    data = _load_roadmap()
    data.setdefault("current_phase", {})["next_action"] = next_action
    _save_roadmap(data)
    return {"success": True, "next_action": next_action}


def add_open_changeset(changeset_id: str) -> dict[str, Any]:
    """Register a changeset as open in the current phase."""
    data = _load_roadmap()
    open_cs = data.get("current_phase", {}).get("open_changesets", [])
    if changeset_id not in open_cs:
        open_cs.append(changeset_id)
    data["current_phase"]["open_changesets"] = open_cs
    _save_roadmap(data)
    return {"success": True, "open_changesets": open_cs}


def remove_open_changeset(changeset_id: str) -> dict[str, Any]:
    """Remove a resolved changeset from the current phase open list."""
    data = _load_roadmap()
    open_cs = data.get("current_phase", {}).get("open_changesets", [])
    data["current_phase"]["open_changesets"] = [c for c in open_cs if c != changeset_id]
    _save_roadmap(data)
    return {"success": True, "open_changesets": data["current_phase"]["open_changesets"]}
