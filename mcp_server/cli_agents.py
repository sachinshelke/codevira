"""
cli_agents.py — `codevira agents` standalone command.

Pillar 2.2 of the v2.0 master plan. Re-runs ONLY the per-IDE nudge-file
generation step of `codevira setup`. Useful when:

  * You edited the templates locally and want to regenerate
  * A new IDE installed mid-project and you want to add ITS nudge file
    without re-running full setup
  * You want to dry-run the nudge files before committing

Wraps ``mcp_server.agents_md`` (the generator module) which already has
the section-replaceable ``<!-- codevira:start -->...<!-- codevira:end -->``
logic so existing user content is preserved on regenerate.

Used by ``mcp_server.cli`` (the ``agents`` subcommand). Also importable
for tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import IO

from mcp_server import agents_md


# Map raw WriteAction → human-readable label + icon
_ACTION_LABELS: dict[str, tuple[str, str]] = {
    "created":            ("✓", "created"),
    "block_replaced":     ("✓", "updated"),
    "block_appended":     ("✓", "appended"),
    "no_change":          ("·", "unchanged"),
    "would_create":       ("▸", "would create"),
    "would_replace":      ("▸", "would update"),
    "would_append":       ("▸", "would append"),
    "would_be_no_change": ("·", "would be unchanged"),
}


def cmd_agents(
    *,
    ide: str | None = None,         # one of supported_ides() or None for all
    dry_run: bool = False,
    project: Path | None = None,
    out: IO[str] | None = None,     # for testability
) -> int:
    """Generate per-IDE nudge files. Returns process exit code.

    0 — success (or dry-run with no errors)
    1 — invalid --ide value, or write failure
    """
    out = out or sys.stdout

    # Resolve project root with the same Bug-8 defense the rest of the
    # CLI uses.
    try:
        from mcp_server.paths import (
            get_project_root, set_project_dir,
            invalidate_data_dir_cache, is_invalid_project_root,
        )
        if project is not None:
            resolved = Path(project).resolve()
            rejection = is_invalid_project_root(resolved)
            if rejection:
                out.write(
                    f"Error: --project {project!r} is not a valid project "
                    f"root: {rejection}\n"
                )
                return 1
            set_project_dir(resolved)
            invalidate_data_dir_cache()
        project_root = get_project_root()
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not resolve project — {e}\n")
        return 1

    # Determine which IDEs to render.
    # P1-1 (rc.5): default to ONLY detected IDEs so this command's plan
    # matches `codevira setup --dry-run`. Pre-fix, `agents` rendered nudge
    # files for every codevira-supported IDE regardless of whether the user
    # had it installed — confusing when comparing `setup` and `agents`
    # output side-by-side. Pass `ide="all"` (or set `_AGENTS_ALL=1` env)
    # to restore the legacy "render for everything" behaviour.
    supported = agents_md.supported_ides()
    if ide is not None and ide != "all":
        if ide not in supported:
            out.write(
                f"Error: unknown --ide {ide!r}. "
                f"Valid: {', '.join(supported)} or 'all'\n"
            )
            return 1
        targets = (ide,)
    elif ide == "all":
        targets = supported
    else:
        # Default: align with detect_installed_ides()
        try:
            from mcp_server.ide_inject import detect_installed_ides
            detected = set(detect_installed_ides(project_root))
            # Always include agents_md (the universal fallback).
            detected.add("agents_md")
            targets = tuple(i for i in supported if i in detected)
        except Exception:
            targets = supported

    out.write(f"▸ Generating nudge files in {project_root}\n")
    if dry_run:
        out.write("  (dry-run — nothing will be written)\n")
    out.write("\n")

    failures: list[str] = []
    written: list[str] = []
    skipped: list[str] = []

    for ide_name in targets:
        try:
            result = agents_md.write_nudge_file(
                ide=ide_name,
                project_root=project_root,
                dry_run=dry_run,
            )
        except Exception as e:  # noqa: BLE001
            out.write(f"  ✗ {ide_name:<14} FAILED: {e}\n")
            failures.append(ide_name)
            continue

        try:
            rel = result.target_path.relative_to(project_root)
        except ValueError:
            rel = result.target_path  # shouldn't happen — defense

        icon, label = _ACTION_LABELS.get(result.action, ("?", result.action))
        size_hint = f", {result.bytes_written:,} bytes" if result.bytes_written else ""
        out.write(f"  {icon} {ide_name:<14} → {rel}  ({label}{size_hint})\n")

        if result.action in ("created", "block_replaced", "block_appended",
                              "would_create", "would_replace", "would_append"):
            written.append(ide_name)
        else:
            skipped.append(ide_name)

    out.write("\n")
    out.write(
        f"▸ summary: {len(written)} would write / wrote · "
        f"{len(skipped)} unchanged · {len(failures)} failed\n"
    )
    if failures:
        out.write(f"  failed IDEs: {', '.join(failures)}\n")
        return 1
    return 0


def cmd_hooks_install(
    *,
    project: Path | None = None,
    dry_run: bool = False,
    out: IO[str] | None = None,
) -> int:
    """`codevira hooks install` — Pillar 2.3.

    Re-runs ONLY the Claude Code lifecycle-hook installation step from
    setup_wizard. Useful when you want to add hooks without touching
    nudge files or MCP config (e.g., you re-installed Claude Code and
    its global config got reset).

    Wraps ``mcp_server.setup_wizard`` step planners + executors directly.
    """
    out = out or sys.stdout

    try:
        from mcp_server.paths import (
            get_project_root, set_project_dir,
            invalidate_data_dir_cache, is_invalid_project_root,
        )
        if project is not None:
            resolved = Path(project).resolve()
            rejection = is_invalid_project_root(resolved)
            if rejection:
                out.write(
                    f"Error: --project {project!r} is not a valid project "
                    f"root: {rejection}\n"
                )
                return 1
            set_project_dir(resolved)
            invalidate_data_dir_cache()
        project_root = get_project_root()
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not resolve project — {e}\n")
        return 1

    from mcp_server import setup_wizard

    out.write(f"▸ Installing Claude Code lifecycle hooks for {project_root}\n")
    if dry_run:
        out.write("  (dry-run — nothing will be written)\n")
    out.write("\n")

    # Run only the hook-installation steps.
    hook_steps = setup_wizard._plan_hook_steps()
    if not hook_steps:
        out.write("  · nothing to install (Claude Code not detected)\n")
        return 0

    failures: list[str] = []
    successes: list[str] = []
    for step in hook_steps:
        result = setup_wizard._execute_hook(step, dry_run=dry_run)
        label = f"{step.kind}: {step.preview}"
        if result.succeeded:
            out.write(f"  ✓ {label}  ({result.action})\n")
            successes.append(label)
        else:
            out.write(f"  ✗ {label}  ({result.error or result.action})\n")
            failures.append(label)

    out.write(
        f"\n▸ summary: {len(successes)} ok, {len(failures)} failed\n"
    )
    return 1 if failures else 0
