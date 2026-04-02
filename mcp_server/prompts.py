"""
prompts.py — MCP workflow prompt templates.

Five pre-built prompts that give AI agents structured starting points
for common workflows: review, debug, onboard, pre-commit, and architecture.
"""
from __future__ import annotations

PROMPTS = {
    "review_changes": {
        "name": "review_changes",
        "description": "Review current changes with risk analysis, test coverage gaps, and learned rules.",
        "arguments": [
            {"name": "base_ref", "description": "Base git ref to diff against (default: main)", "required": False},
        ],
        "template": (
            "Review the current code changes for quality, risks, and missed tests.\n\n"
            "Steps:\n"
            "1. Call analyze_changes() to get function-level risk scores and test coverage gaps.\n"
            "2. Call get_learned_rules() to check if any changes violate learned patterns.\n"
            "3. Call get_preferences() to verify the changes match the developer's coding style.\n"
            "4. For each HIGH risk change, call query_graph() to understand callers and downstream impact.\n\n"
            "Present a structured review with:\n"
            "- Risk summary (high/medium/low changes)\n"
            "- Test coverage gaps\n"
            "- Style violations\n"
            "- Suggested improvements"
        ),
    },
    "debug_issue": {
        "name": "debug_issue",
        "description": "Debug an issue by tracing code paths, searching past decisions, and checking confidence.",
        "arguments": [
            {"name": "description", "description": "Description of the issue to debug", "required": True},
        ],
        "template": (
            "Debug the following issue: {description}\n\n"
            "Steps:\n"
            "1. Call search_codebase() with the issue description to find relevant code.\n"
            "2. Call query_graph() on the most relevant files to trace callers and callees.\n"
            "3. Call search_decisions() to check if similar issues were addressed before.\n"
            "4. Call get_decision_confidence() on affected files to check if this is an ambiguous area.\n"
            "5. Call get_impact() on the likely source file to understand blast radius.\n\n"
            "Present:\n"
            "- Most likely root cause with evidence\n"
            "- Affected code paths\n"
            "- Past decisions that relate to this area\n"
            "- Recommended fix approach"
        ),
    },
    "onboard_session": {
        "name": "onboard_session",
        "description": "Start a new coding session with full context — roadmap, open work, rules, and recent history.",
        "arguments": [],
        "template": (
            "Orient to this project and prepare for a productive coding session.\n\n"
            "Steps:\n"
            "1. Call get_session_context() for a complete catch-up: roadmap phase, open changesets, "
            "recent decisions, confidence scores, preferences, and learned rules.\n"
            "2. Call list_open_changesets() to check for interrupted multi-file work.\n"
            "3. Call get_project_maturity() to understand overall project intelligence level.\n\n"
            "Present a concise session briefing:\n"
            "- Current project phase and next action\n"
            "- Any open changesets that need resuming\n"
            "- Key rules and preferences to follow\n"
            "- Areas of low confidence that need extra care"
        ),
    },
    "pre_commit_check": {
        "name": "pre_commit_check",
        "description": "Pre-commit review — check staged changes for risks and missing tests.",
        "arguments": [],
        "template": (
            "Review the staged changes before committing.\n\n"
            "Steps:\n"
            "1. Call analyze_changes(base_ref='HEAD') to review staged changes at function level.\n"
            "2. Call find_hotspots() to check if any modified functions are complexity hotspots.\n"
            "3. Call get_learned_rules() to verify no patterns are violated.\n\n"
            "Present:\n"
            "- Changes summary with risk scores\n"
            "- Missing test coverage\n"
            "- Hotspot warnings (large functions, high fan-in)\n"
            "- Go/no-go recommendation"
        ),
    },
    "architecture_overview": {
        "name": "architecture_overview",
        "description": "Show project architecture — dependency graph, layers, hotspots, and maturity.",
        "arguments": [],
        "template": (
            "Generate an architecture overview of this project.\n\n"
            "Steps:\n"
            "1. Call export_graph(format='mermaid') to generate a dependency diagram.\n"
            "2. Call list_nodes() to see all files organized by layer.\n"
            "3. Call find_hotspots() to identify complexity and risk areas.\n"
            "4. Call get_project_maturity() for overall intelligence metrics.\n\n"
            "Present:\n"
            "- Dependency diagram (Mermaid)\n"
            "- Layer breakdown (API, service, database, utility, test)\n"
            "- Hotspots and risk areas\n"
            "- Maturity score and what it means"
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
