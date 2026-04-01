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


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _phase_number(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("phase", entry.get("number"))
    return entry


def _normalize_phase_entry(entry: Any, default_status: str | None = None) -> dict[str, Any]:
    normalized = dict(entry) if isinstance(entry, dict) else {}
    phase_number = _phase_number(entry)

    if phase_number is not None:
        normalized["phase"] = phase_number
        normalized["number"] = phase_number

    if default_status and not normalized.get("status"):
        normalized["status"] = default_status

    description = normalized.get("description") or normalized.get("goal")
    if description is not None:
        normalized["description"] = description
        normalized.setdefault("goal", description)

    return normalized


def _normalize_current_phase(raw_current: Any, data: dict[str, Any]) -> dict[str, Any]:
    phases = _list_or_empty(data.get("phases"))
    current = dict(raw_current) if isinstance(raw_current, dict) else {}
    current_number = _phase_number(raw_current)

    if current_number is None and phases:
        for candidate in phases:
            if isinstance(candidate, dict) and candidate.get("status") in {"in_progress", "blocked", "pending"}:
                current_number = _phase_number(candidate)
                break
        if current_number is None:
            current_number = _phase_number(phases[0])

    matched_phase = next(
        (
            phase
            for phase in phases
            if str(_phase_number(phase)) == str(current_number)
        ),
        {},
    )
    if isinstance(matched_phase, dict):
        for key, value in matched_phase.items():
            current.setdefault(key, value)

    if current_number is None:
        current_number = current.get("number", current.get("phase"))

    if current_number is not None:
        current["number"] = current_number

    normalized = _normalize_phase_entry(current, default_status="pending")
    normalized.pop("phase", None)

    if current_number is not None:
        normalized["number"] = current_number
        normalized.setdefault("name", f"Phase {current_number}")
    else:
        normalized.setdefault("name", "Getting Started")

    normalized.setdefault(
        "next_action",
        data.get("next_action")
        or (
            "Define your first phase: use add_phase() to queue upcoming work, "
            "or update_next_action() to describe what needs doing next."
        ),
    )
    normalized["open_changesets"] = _list_or_empty(
        normalized.get("open_changesets", data.get("open_changesets", []))
    )

    return normalized


def _normalize_roadmap(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _create_stub_roadmap()

    current = _normalize_current_phase(data.get("current_phase"), data)
    current_number = current.get("number")
    phases = _list_or_empty(data.get("phases"))

    upcoming_raw = data.get("upcoming_phases")
    if not isinstance(upcoming_raw, list):
        upcoming_raw = data.get("upcoming")
    if not isinstance(upcoming_raw, list):
        upcoming_raw = [
            phase
            for phase in phases
            if str(_phase_number(phase)) != str(current_number)
            and str(getattr(phase, "get", lambda _k, _d=None: None)("status", "")).lower()
            not in {"done", "complete", "completed"}
        ]

    completed_raw = data.get("completed_phases")
    if not isinstance(completed_raw, list):
        completed_raw = [
            phase
            for phase in phases
            if str(_phase_number(phase)) != str(current_number)
            and str(getattr(phase, "get", lambda _k, _d=None: None)("status", "")).lower()
            in {"done", "complete", "completed"}
        ]

    deferred_raw = data.get("deferred")
    if not isinstance(deferred_raw, list):
        deferred_raw = data.get("deferred_phases", [])

    return {
        "project": data.get("project", get_project_root().name),
        "version": str(data.get("version", "1.0")),
        "current_phase": current,
        "upcoming_phases": [
            _normalize_phase_entry(phase, default_status="pending")
            for phase in _list_or_empty(upcoming_raw)
        ],
        "deferred": [
            _normalize_phase_entry(phase, default_status="deferred")
            for phase in _list_or_empty(deferred_raw)
        ],
        "completed_phases": [
            _normalize_phase_entry(phase, default_status="completed")
            for phase in _list_or_empty(completed_raw)
        ],
    }


def _load_roadmap() -> dict:
    roadmap_file = _roadmap_file()
    if not roadmap_file.exists():
        stub = _create_stub_roadmap()
        _save_roadmap(stub)
        return stub
    with open(roadmap_file) as f:
        raw_data = yaml.safe_load(f) or {}

    normalized = _normalize_roadmap(raw_data)
    if normalized != raw_data:
        _save_roadmap(normalized)
    return normalized


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
    _roadmap_file().parent.mkdir(parents=True, exist_ok=True)
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
        "deferred_phases": data.get("deferred", []),
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
        "number": phase,
        "name": name,
        "priority": priority,
        "depends_on": depends_on or [],
        "description": description,
        "goal": description,
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
        "number": target.get("number", target.get("phase")),
        "reason": reason,
        "deferred_date": date.today().isoformat(),
        "original_priority": target.get("priority"),
        "goal": target.get("goal", target.get("description")),
        "description": target.get("description", target.get("goal", "")),
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
        "number": current["number"],
        "name": current["name"],
        "completed": date.today().isoformat(),
        "key_decisions": key_decisions,
        "goal": current.get("goal", current.get("description", "")),
        "description": current.get("description", current.get("goal", "")),
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
            "goal": next_phase.get("goal", next_phase.get("description", "")),
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
