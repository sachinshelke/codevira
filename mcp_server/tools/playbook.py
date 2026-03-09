"""
MCP tool for retrieving curated rule playbooks by task type.
Serves the right 2-3 rule files for a given task — not all of them.
"""
from pathlib import Path

from mcp_server.paths import get_package_data_dir


def _rules_dir() -> Path:
    return get_package_data_dir() / "rules"

# Task type → relevant rule files (ordered by importance)
PLAYBOOKS: dict[str, list[str]] = {
    "add_route": [
        "api-standards.md",
        "coding-standards.md",
    ],
    "add_service": [
        "coding-standards.md",
        "resilience-observability.md",
    ],
    "add_schema": [
        "coding-standards.md",
        "api-standards.md",
    ],
    "debug_pipeline": [
        "resilience-observability.md",
        "coding-standards.md",
    ],
    "commit": [
        "git_commits.md",
        "git-cicd-governance.md",
    ],
    "write_test": [
        "testing-standards.md",
        "smoke-testing.md",
    ],
}


def get_playbook(task_type: str) -> dict:
    """
    Return curated rule content for a specific task type.
    Serves only the relevant 2-3 rule files, not all of them.

    Args:
        task_type: One of add_route | add_service | add_schema |
                   debug_pipeline | commit | write_test

    Returns:
        Dict with task_type, rules (list of {file, content}), token_note.
    """
    task_type = task_type.lower().strip()

    if task_type not in PLAYBOOKS:
        return {
            "found": False,
            "task_type": task_type,
            "available_task_types": sorted(PLAYBOOKS.keys()),
            "hint": "Use get_node() for file-specific rules embedded in the context graph.",
        }

    rule_files = PLAYBOOKS[task_type]
    rules = []

    for filename in rule_files:
        path = _rules_dir() / filename
        if path.exists():
            rules.append({
                "file": filename,
                "content": path.read_text().strip(),
            })
        else:
            rules.append({
                "file": filename,
                "content": f"[File not found: {path}]",
            })

    return {
        "found": True,
        "task_type": task_type,
        "rules": rules,
        "note": f"Serving {len(rules)} rule files for '{task_type}'. "
                f"File-specific rules are in get_node() → rules field.",
    }
