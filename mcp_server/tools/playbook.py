"""
MCP tool for retrieving curated rule playbooks by task type.

P0-C (rc.5 audit, 2026-05-13): playbooks are now PROJECT-SCOPED first.
The bundled rules under ``mcp_server/data/rules/`` are
Python/pytest-shaped and were being served verbatim to TS/JS/Go
projects, where they're at-best-irrelevant and at-worst-misleading
("use ruff", "run pytest", etc.). The new resolution chain:

1. ``<data_dir>/playbooks/<task_type>/*.md`` — project-specific overrides
2. ``<project_root>/.codevira/playbooks/<task_type>/*.md`` — in-repo
   overrides (committed; team-shared)
3. Bundled defaults — only served when the project's detected language
   matches the playbook's tag, AND only with a clear "this is a generic
   default; consider adding a project-specific playbook" warning.

Step 3 ensures that a TypeScript project asking for ``commit`` no longer
silently gets Python-flavoured rules. Instead it gets either the user's
own playbook or a clear empty-but-actionable response.
"""
from __future__ import annotations

from pathlib import Path

from mcp_server.paths import get_package_data_dir


def _rules_dir() -> Path:
    return get_package_data_dir() / "rules"


def _project_playbook_dirs() -> list[Path]:
    """P0-C (rc.5): override locations checked before bundled defaults."""
    dirs: list[Path] = []
    try:
        from mcp_server.paths import get_data_dir, get_project_root
        dirs.append(get_data_dir() / "playbooks")
        dirs.append(get_project_root() / ".codevira" / "playbooks")
    except Exception:
        pass
    return dirs


def _detect_project_language() -> str | None:
    """Read the language detected at init time. Returns None if unavailable."""
    try:
        from mcp_server.paths import get_data_dir
        import yaml
        cfg_path = get_data_dir() / "config.yaml"
        if not cfg_path.is_file():
            return None
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        proj = cfg.get("project", cfg)
        lang = proj.get("language")
        return lang.lower() if isinstance(lang, str) else None
    except Exception:
        return None


# P0-C (rc.5): which bundled playbooks are language-agnostic vs Python-shaped.
# Files NOT in this list are treated as language-specific (currently Python).
_LANGUAGE_AGNOSTIC_RULES: set[str] = {
    "git_commits.md",
    "git-cicd-governance.md",
    "engineering-excellence.md",
    "incremental-updates.md",
    "master_rule.md",
    "multi-language.md",
    "smoke-testing.md",
    "resilience-observability.md",
}

# Task type → relevant rule files (ordered by importance)
# These map common development tasks to the rule files that govern them.
PLAYBOOKS: dict[str, list[str]] = {
    "add_tool": [
        "coding-standards.md",
        "testing-standards.md",
    ],
    "add_service": [
        "coding-standards.md",
        "resilience-observability.md",
    ],
    "add_schema": [
        "coding-standards.md",
        "persistence.md",
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
        task_type: One of add_tool | add_service | add_schema |
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

    # P0-C (rc.5): project-scoped overrides first, then language-aware default.
    rules: list[dict] = []
    sources: list[str] = []
    seen_filenames: set[str] = set()

    # 1. Project-specific overrides (~/.codevira/projects/<slug>/playbooks/<task>/
    #    OR <project>/.codevira/playbooks/<task>/).
    for override_dir in _project_playbook_dirs():
        task_dir = override_dir / task_type
        if task_dir.is_dir():
            for md in sorted(task_dir.glob("*.md")):
                if md.name in seen_filenames:
                    continue
                seen_filenames.add(md.name)
                rules.append({
                    "file": md.name,
                    "source": "project_override",
                    "content": md.read_text().strip(),
                })
            sources.append(f"project_override({task_dir})")

    # 2. Bundled defaults — but filter out language-specific rules when the
    #    project's detected language doesn't match.
    project_language = _detect_project_language() or "unknown"
    rule_files = PLAYBOOKS[task_type]
    skipped: list[str] = []
    for filename in rule_files:
        if filename in seen_filenames:
            continue
        is_agnostic = filename in _LANGUAGE_AGNOSTIC_RULES
        # Language tagging: assume bundled defaults are Python-shaped unless
        # explicitly marked agnostic. Skip with a clear note when the project
        # isn't Python.
        if not is_agnostic and project_language not in (None, "unknown", "python"):
            skipped.append(filename)
            continue
        path = _rules_dir() / filename
        if path.exists():
            rules.append({
                "file": filename,
                "source": "bundled_default",
                "content": path.read_text().strip(),
            })
            seen_filenames.add(filename)

    response: dict = {
        "found": bool(rules),
        "task_type": task_type,
        "project_language_detected": project_language,
        "rules": rules,
    }
    if skipped:
        response["skipped_language_specific_rules"] = skipped
        response["warning"] = (
            f"Skipped {len(skipped)} bundled rule(s) "
            f"({', '.join(skipped)}) because they're Python-flavoured and your "
            f"project's detected language is '{project_language}'. To override, "
            f"drop your own playbook files into "
            f"~/.codevira/projects/<slug>/playbooks/{task_type}/ or "
            f"<project>/.codevira/playbooks/{task_type}/."
        )
    if not rules:
        response["hint"] = (
            f"No playbook content for '{task_type}' in this project. The "
            f"bundled defaults are Python-shaped; create your own at "
            f"<project>/.codevira/playbooks/{task_type}/<rule>.md so this tool "
            f"returns rules tailored to your stack."
        )
    elif sources:
        response["note"] = (
            f"Serving {len(rules)} rule file(s) for '{task_type}'. "
            f"Sources: {', '.join(sources) if sources else 'bundled_default'}. "
            f"Project-specific rules under get_node().rules take precedence."
        )
    else:
        response["note"] = (
            f"Serving {len(rules)} bundled rule file(s) for '{task_type}'. "
            f"Add project-specific playbooks at "
            f"<project>/.codevira/playbooks/{task_type}/ to override."
        )
    return response
