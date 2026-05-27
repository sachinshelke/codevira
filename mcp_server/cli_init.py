"""
cli_init.py — ``codevira init`` command.

Scaffolds the in-repo storage layer for a new (or existing) project:

  .codevira/                      ← source of truth (committed)
    decisions.jsonl               ← empty (entries appended here)
    digest.jsonl                  ← regenerated
    manifest.yaml                 ← regenerated
    outcomes.jsonl                ← empty (git-observed kept/reverted)
    sessions.jsonl                ← empty (session events)
    config.yaml                   ← project settings
    enforcement.yaml              ← per-decision enforcement policy

  .codevira-cache/                ← gitignored (rebuildable)

  .gitignore                      ← updated: + .codevira-cache/

  AGENTS.md                       ← codevira-managed block added (preserves
                                    any existing user content outside markers)

Run on a fresh project OR an existing project (idempotent — running
twice doesn't clobber anything you've already configured).

v3.0.0 (2026-05-22 surface-cut audit): we no longer scaffold
``preferences.jsonl`` or ``learned_rules.jsonl``. The MCP tools that
wrote them (get_preferences / get_learned_rules / retire_rule) were
deleted in the audit; the files would just be empty noise. Existing
projects keep their files (init is idempotent — never touches files
that are already present).

If the project has v2.1.x data at ``~/.codevira/projects/<key>/graph.db``,
``codevira init`` does NOT migrate it. Run ``codevira archive-legacy``
afterwards to preserve those decisions as a read-only reference at
``.codevira/legacy.jsonl``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mcp_server.storage import paths


def cmd_init(*, yes: bool = False, dry_run: bool = False) -> int:
    """Scaffold .codevira/ + .codevira-cache/ + update AGENTS.md / .gitignore.

    Returns POSIX exit code (0 success, 1 error).
    """
    from mcp_server.paths import get_project_root

    project = get_project_root()
    cv_dir = paths.codevira_dir(project)
    cache_dir = paths.codevira_cache_dir(project)

    print()
    print("  Codevira — Init")
    print(f"  Project: {project}")
    print("  " + "─" * 60)
    print()

    # Detect existing state.
    cv_exists = cv_dir.is_dir()
    gitignore_path = project / ".gitignore"
    gitignore_text = (
        gitignore_path.read_text(encoding="utf-8") if gitignore_path.is_file() else ""
    )
    gitignore_has_cache = ".codevira-cache" in gitignore_text
    # v3.0 hardening (2026-05-23 RC audit): detect when .codevira/ ITSELF
    # is gitignored. That defeats codevira's "shared in-repo memory"
    # promise — decisions.jsonl, sessions.jsonl, manifest.yaml never get
    # committed, so collaborators / other machines / other AI tools see
    # an empty memory store. Heuristic: any non-comment line that exactly
    # matches `.codevira`, `.codevira/`, `/.codevira`, or `/.codevira/`
    # (with optional trailing comment). Doesn't catch every edge case
    # (e.g. wildcard like `*codevira*`) but covers the common gitignore
    # pattern users land on by reflex.
    gitignore_blocks_codevira = False
    for raw_line in gitignore_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line in {".codevira", ".codevira/", "/.codevira", "/.codevira/"}:
            gitignore_blocks_codevira = True
            break
    agents_md_path = project / "AGENTS.md"
    agents_md_exists = agents_md_path.is_file()

    # Loud surface — the user must see this BEFORE the plan.
    if gitignore_blocks_codevira:
        print("  ⚠  WARNING: .codevira/ is listed in your .gitignore")
        print("     This defeats codevira's core promise. Without committing")
        print("     .codevira/decisions.jsonl, manifest.yaml, and friends,")
        print("     your memory is local-machine-only — collaborators and")
        print("     other AI tools (Cursor, Windsurf, etc.) won't see any of")
        print("     your decisions. Remove `.codevira/` from .gitignore")
        print("     and keep only `.codevira-cache/` ignored (cache is")
        print("     rebuildable; .codevira/ is the canonical store).")
        print()

    if cv_exists:
        print(f"  ⚠ .codevira/ already exists at {cv_dir}")
        print("    Init is idempotent — will preserve all existing data.")
        print()

    # Plan
    print("  Plan:")
    print(f"    {'(exists)' if cv_exists else 'CREATE  '} {cv_dir}/")
    print(f"    {'(exists)' if cache_dir.is_dir() else 'CREATE  '} {cache_dir}/")
    # v3.0.0: scaffold ONLY the storage files v3.0.0 code actually reads
    # or writes. preferences.jsonl / learned_rules.jsonl removed in the
    # 2026-05-22 surface-cut audit (their MCP tools were deleted).
    files_to_create = [
        "decisions.jsonl",
        "outcomes.jsonl",
        "sessions.jsonl",
    ]
    for f in files_to_create:
        target = cv_dir / f
        status = "(exists)" if target.is_file() else "CREATE  "
        print(f"    {status} {target.relative_to(project)}")

    cfg_path = paths.config_path(project)
    enf_path = paths.enforcement_path(project)
    print(
        f"    {'(exists)' if cfg_path.is_file() else 'CREATE  '} "
        f"{cfg_path.relative_to(project)}"
    )
    print(
        f"    {'(exists)' if enf_path.is_file() else 'CREATE  '} "
        f"{enf_path.relative_to(project)}"
    )

    print(
        f"    {'(in gitignore)' if gitignore_has_cache else 'UPDATE       '} "
        f".gitignore  (+ .codevira-cache/)"
    )
    print(
        f"    {'(has marker)' if agents_md_exists and _agents_md_has_marker(agents_md_path) else 'UPDATE      '} "
        f"AGENTS.md  (+ codevira-managed block)"
    )
    print()

    if dry_run:
        print("  [dry-run] No changes made.")
        return 0

    if not yes and not cv_exists:
        # Only ask on fresh init; idempotent re-init is safe by default.
        try:
            response = input("  Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Aborted.")
            return 0
        if response not in ("", "y", "yes"):
            print("  Aborted.")
            return 0

    # Step 1: create directories
    paths.ensure_dirs(project)

    # Step 2: create empty JSONL files (idempotent — only if missing).
    for f in files_to_create:
        target = cv_dir / f
        if not target.is_file():
            target.touch()

    # Step 3: write config.yaml (idempotent — only if missing).
    if not cfg_path.is_file():
        project_name = _detect_project_name(project)
        import yaml

        cfg = {
            "schema_version": 1,
            "project_name": project_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "codevira_version": _codevira_version(),
            "agents_md_max_kb": 5,
            "inject_max_decisions": 3,
            "inject_max_tokens": 600,
            "archive_after_days": 90,
        }
        from mcp_server.storage.atomic import atomic_write_text

        atomic_write_text(
            cfg_path,
            yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
        )

    # Step 4: write default enforcement.yaml (idempotent).
    if not enf_path.is_file():
        from mcp_server.storage.atomic import atomic_write_text

        atomic_write_text(
            enf_path,
            "# enforcement.yaml — per-project decision-enforcement policy.\n"
            "# v2.2.0 default: do_not_revert decisions hard-block matching edits.\n"
            "schema_version: 1\n"
            "defaults:\n"
            "  do_not_revert: hard-block\n"
            "  protected_age_days: 7\n"
            "  override_requires_reason: true\n"
            "overrides: {}\n",
        )

    # Step 5: update .gitignore (idempotent).
    if not gitignore_has_cache:
        from mcp_server.storage.atomic import atomic_write_text

        existing = (
            gitignore_path.read_text(encoding="utf-8")
            if gitignore_path.is_file()
            else ""
        )
        addition = (
            "\n# Codevira cache (gitignored; rebuilt by `codevira sync`)\n"
            ".codevira-cache/\n"
        )
        if not existing.endswith("\n") and existing:
            existing += "\n"
        # .gitignore is read+rewrite (not append) — atomic_write_text
        # is safe because we hold the full content in memory and rename
        # into place; a crash mid-write leaves the old .gitignore intact.
        atomic_write_text(gitignore_path, existing + addition)

    # Step 6: regenerate AGENTS.md (creates the marker block if missing).
    from mcp_server.storage import agents_md_generator

    agents_md_generator.regenerate()

    # Step 7: regenerate manifest + digest + FTS5 from (possibly empty) decisions.
    from mcp_server.storage import decisions_store

    decisions_store.rebuild_indexes()

    # Success summary
    print()
    print("  ✓ Initialized.")
    print()
    print("  Next steps:")
    print("    1. git add .codevira/ AGENTS.md .gitignore && git commit")
    print("    2. Open Claude Code / Cursor / Antigravity in this project;")
    print("       codevira's MCP server is ready.")
    print("    3. Record your first decision:")
    print('         `record_decision(decision="Use bcrypt for passwords",')
    print('                          file_path="auth.py", do_not_revert=True)`')
    print()
    return 0


def _agents_md_has_marker(path: Path) -> bool:
    try:
        return "<!-- codevira:begin" in path.read_text(encoding="utf-8")
    except Exception:
        return False


def _detect_project_name(project_root: Path) -> str:
    """Best-effort project name from pyproject.toml, package.json, or dir name."""
    try:
        py = project_root / "pyproject.toml"
        if py.is_file():
            try:
                import tomllib  # Python 3.11+
            except ModuleNotFoundError:  # Python 3.10 — stdlib tomllib absent
                import tomli as tomllib  # type: ignore[no-redef]

            data = tomllib.loads(py.read_text())
            name = data.get("project", {}).get("name")
            if name:
                return str(name)
    except Exception:
        pass

    try:
        pkg = project_root / "package.json"
        if pkg.is_file():
            import json

            data = json.loads(pkg.read_text())
            name = data.get("name")
            if name:
                return str(name)
    except Exception:
        pass

    return project_root.name


def _codevira_version() -> str:
    try:
        from mcp_server import __version__

        return __version__
    except Exception:
        return "2.2.0"
