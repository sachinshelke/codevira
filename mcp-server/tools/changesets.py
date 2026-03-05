"""
MCP tools for managing multi-file change sets.
Tracks in-progress fixes that span multiple files across sessions.
"""
from datetime import date
from pathlib import Path
from typing import Any

import yaml

CHANGESETS_DIR = Path(__file__).parent.parent.parent / "graph" / "changesets"


def _changeset_path(changeset_id: str) -> Path:
    return CHANGESETS_DIR / f"{changeset_id}.yaml"


def start_changeset(
    changeset_id: str,
    description: str,
    files: list[str],
    trigger: str = "medium_change",
) -> dict[str, Any]:
    """
    Begin tracking a multi-file fix. Call BEFORE touching any files.

    Args:
        changeset_id: Short slug (e.g. "synonym-pipeline-fix")
        description: What this changeset does
        files: All files that will be modified (include ones not yet modified)
        trigger: small_fix | medium_change | large_change

    Creates .agents/graph/changesets/{id}.yaml
    """
    CHANGESETS_DIR.mkdir(exist_ok=True)
    path = _changeset_path(changeset_id)

    if path.exists():
        return {
            "success": False,
            "message": f"Changeset '{changeset_id}' already exists. Use a different ID or complete the existing one.",
        }

    data = {
        "id": changeset_id,
        "status": "in_progress",
        "created": date.today().isoformat(),
        "trigger": trigger,
        "description": description,
        "files_modified": [],
        "files_pending": files,
        "blocker": None,
        "decisions": [],
    }

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return {"success": True, "changeset_id": changeset_id, "tracking": files}


def update_changeset_progress(
    changeset_id: str,
    file_done: str,
    blocker: str | None = None,
) -> dict[str, Any]:
    """
    Move a file from files_pending to files_modified.
    Call after each file is completed within the changeset.
    """
    path = _changeset_path(changeset_id)
    if not path.exists():
        return {"success": False, "message": f"Changeset '{changeset_id}' not found."}

    with open(path) as f:
        data = yaml.safe_load(f)

    pending = data.get("files_pending", [])
    modified = data.get("files_modified", [])

    if file_done in pending:
        pending.remove(file_done)
    if file_done not in modified:
        modified.append(file_done)

    data["files_pending"] = pending
    data["files_modified"] = modified
    if blocker is not None:
        data["blocker"] = blocker

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return {
        "success": True,
        "files_done": len(modified),
        "files_remaining": len(pending),
        "blocker": blocker,
    }


def complete_changeset(changeset_id: str, decisions: list[str]) -> dict[str, Any]:
    """
    Mark a changeset as complete. Call at session end after all files are done.

    Args:
        changeset_id: The changeset to complete
        decisions: Key decisions made (e.g. "synonyms in text only, not metadata")
                   These are written to the changeset record for future agents.
    """
    path = _changeset_path(changeset_id)
    if not path.exists():
        return {"success": False, "message": f"Changeset '{changeset_id}' not found."}

    with open(path) as f:
        data = yaml.safe_load(f)

    if data.get("files_pending"):
        return {
            "success": False,
            "message": f"Cannot complete — {len(data['files_pending'])} files still pending: {data['files_pending']}",
            "hint": "Either finish the pending files or document the blocker with update_changeset_progress.",
        }

    data["status"] = "complete"
    data["completed"] = date.today().isoformat()
    data["decisions"] = decisions
    data["blocker"] = None

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return {"success": True, "changeset_id": changeset_id, "decisions_recorded": len(decisions)}


def get_changeset(changeset_id: str) -> dict[str, Any]:
    """Get the current state of a changeset."""
    path = _changeset_path(changeset_id)
    if not path.exists():
        return {"found": False, "message": f"Changeset '{changeset_id}' not found."}

    with open(path) as f:
        data = yaml.safe_load(f)
    return {"found": True, "changeset": data}


def list_open_changesets() -> dict[str, Any]:
    """List all in-progress changesets. Call at session start to check for unfinished work."""
    CHANGESETS_DIR.mkdir(exist_ok=True)
    open_cs = []
    for yaml_file in CHANGESETS_DIR.glob("*.yaml"):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data and data.get("status") == "in_progress":
                open_cs.append({
                    "id": data["id"],
                    "description": data.get("description", ""),
                    "created": data.get("created", ""),
                    "files_pending": data.get("files_pending", []),
                    "blocker": data.get("blocker"),
                })
        except Exception:
            pass

    return {
        "open_changesets": open_cs,
        "count": len(open_cs),
        "warning": "Complete or document blockers before starting new work." if open_cs else None,
    }


def update_node_after_change(file_path: str, changes: dict[str, Any]) -> dict[str, Any]:
    """
    Update a graph node's metadata after making changes to that file.
    Called by the documenter agent at session end.

    Args:
        file_path: The file that was changed
        changes: Dict with any of: last_changed_by, new_rules (list), new_connections (list)
    """
    from tools.graph import _load_all_nodes
    from pathlib import Path
    import glob

    graph_dir = Path(__file__).parent.parent.parent / "graph"
    nodes_by_file: dict[str, Path] = {}

    for yaml_file in graph_dir.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data and "nodes" in data and file_path in data["nodes"]:
                nodes_by_file[yaml_file] = data
                break
        except Exception:
            pass

    if not nodes_by_file:
        return {
            "success": False,
            "message": f"Node '{file_path}' not found in any graph file.",
            "hint": "Add it to the appropriate .agents/graph/*.yaml file.",
        }

    yaml_file, data = next(iter(nodes_by_file.items()))
    node = data["nodes"][file_path]

    if "last_changed_by" in changes:
        node["last_changed_by"] = changes["last_changed_by"]

    if "new_rules" in changes:
        existing = node.get("rules", [])
        for rule in changes["new_rules"]:
            if rule not in existing:
                existing.append(rule)
        node["rules"] = existing

    if "do_not_revert" in changes:
        node["do_not_revert"] = changes["do_not_revert"]

    if "new_connections" in changes:
        existing = node.get("connects_to", [])
        existing_targets = {e.get("target") for e in existing}
        for conn in changes["new_connections"]:
            target = conn.get("target")
            if target and target not in existing_targets:
                existing.append(conn)
                existing_targets.add(target)
        node["connects_to"] = existing

    if "key_functions" in changes:
        # Merge new functions into existing list (no duplicates)
        existing = node.get("key_functions", [])
        for fn in changes["key_functions"]:
            if fn not in existing:
                existing.append(fn)
        node["key_functions"] = existing

    if "stability" in changes:
        node["stability"] = changes["stability"]

    if "new_tests" in changes:
        existing = node.get("tests", [])
        for t in changes["new_tests"]:
            if t not in existing:
                existing.append(t)
        node["tests"] = existing

    data["nodes"][file_path] = node
    with open(yaml_file, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {"success": True, "updated_node": file_path, "in_file": str(yaml_file)}
