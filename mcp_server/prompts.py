"""
prompts.py — MCP workflow prompt templates.

v3.0.0 (2026-05-22 surface-cut audit): the prompt library was pruned
from 5 templates → 1. The deleted templates (``review_changes``,
``debug_issue``, ``pre_commit_check``, ``architecture_overview``)
all referenced MCP tools that the audit deleted (``analyze_changes``,
``find_hotspots``, ``get_learned_rules``, ``get_preferences``,
``get_project_maturity``, ``list_open_changesets``, ``export_graph``,
``list_nodes``, ``search_codebase``). Rather than rewriting them
against the v3.0.0 surface, we kept only the one with proven user
value: ``onboard_session``, the "catch me up on this project"
starting point that maps cleanly to the single
``get_session_context()`` MCP tool.

If you find yourself wanting one of the deleted workflows back, ask:
does it add value beyond "the AI calls the right MCP tools in the
right order"? If not, the AI can synthesize the workflow itself from
the slim 25-tool surface — that's what MCP is for.
"""

from __future__ import annotations

PROMPTS: dict[str, dict] = {
    "onboard_session": {
        "name": "onboard_session",
        "description": "Start a new coding session with full project context — roadmap, recent decisions, open work.",
        "arguments": [],
        "template": (
            "Orient to this project and prepare for a productive coding session.\n\n"
            "Steps:\n"
            "1. Call get_session_context() — single round-trip that returns the "
            "current phase, next action, recent decisions, top tags, and a brief "
            "of what was last worked on.\n"
            "2. If get_session_context surfaces decisions with do_not_revert=true, "
            "treat those as architectural constraints for this session.\n"
            "3. If you need detail on a specific phase, call get_phase(n).\n\n"
            "Present a concise briefing:\n"
            "- Current phase + next action\n"
            "- Locked decisions (do_not_revert=true) you must respect\n"
            "- Recent decisions / themes you should be aware of\n"
            "- Open questions or risks you'd like to confirm with the user"
        ),
    },
}


def list_prompts() -> list[dict]:
    """Return all available prompts for MCP list_prompts()."""
    return [
        {
            "name": p["name"],
            "description": p["description"],
            "arguments": p.get("arguments", []),
        }
        for p in PROMPTS.values()
    ]


def get_prompt(name: str, arguments: dict | None = None) -> dict | None:
    """Return a prompt template with arguments substituted."""
    prompt = PROMPTS.get(name)
    if not prompt:
        return None

    template = prompt["template"]
    if arguments:
        for key, value in arguments.items():
            template = template.replace(f"{{{key}}}", str(value))

    return {
        "name": prompt["name"],
        "description": prompt["description"],
        "messages": [
            {"role": "user", "content": {"type": "text", "text": template}},
        ],
    }
