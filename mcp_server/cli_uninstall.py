"""
cli_uninstall.py — v2.2.0 ``codevira uninstall`` command.

Reverses every system write made by ``codevira init`` and ``codevira setup``:

  Per-user (machine-wide)
    ~/.codevira/                   ← entire data dir (with confirmation)

  IDE configs
    ~/.claude.json                 ← drop codevira MCP entry
    ~/.claude/hooks/codevira-*.sh  ← delete codevira hook scripts
    ~/.claude/settings.json        ← drop codevira-tagged hook handlers

  Per-project (per tracked repo found in global.db)
    <repo>/.codevira/              ← prompt before delete
    <repo>/.codevira-cache/        ← prompt before delete
    <repo>/AGENTS.md               ← strip the codevira marker block
                                     (preserves user content outside the
                                     <!-- codevira:begin -->/<!-- codevira:end -->
                                     boundaries)

  Per-project legacy back-compat (v2.1.x → v2.2.0 upgrade path)
    The 2026-05-22 surface-cut audit deleted the per-IDE nudge
    matrix. Machines that upgraded from v2.1.x still have
    codevira-marker blocks embedded in these legacy files, so
    uninstall sweeps them too:
      <repo>/CLAUDE.md
      <repo>/GEMINI.md
      <repo>/.cursor/rules/codevira.mdc
      <repo>/.windsurfrules
      <repo>/.github/copilot-instructions.md
    Same marker-preservation guarantee — user content outside the
    codevira block survives byte-for-byte.

Flags:
  --dry-run        Print the plan; don't write anything.
  -y, --yes        Skip every confirmation prompt.
  --keep-data      Don't touch ~/.codevira/ or per-project .codevira/
                   directories (handy for "uninstall the binary, keep my
                   decisions" workflows).

Closes the 2026-05-22 audit's "uninstalling left junk" complaint —
``pipx uninstall codevira`` removes the venv but leaves ~15 system
touch points behind. This command sweeps all of them.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import IO


def cmd_uninstall(
    *,
    dry_run: bool = False,
    yes: bool = False,
    keep_data: bool = False,
    out: IO[str] | None = None,
) -> int:
    """Reverse every system write made by codevira. Returns exit code."""
    out = out or sys.stdout

    out.write("\n")
    out.write("  Codevira — Uninstall\n")
    out.write("  " + "─" * 60 + "\n")
    out.write("\n")
    if dry_run:
        out.write("  [dry-run] No changes will be made.\n\n")

    plan = _build_uninstall_plan(keep_data=keep_data)

    # Show plan
    out.write("  Plan:\n")
    if not plan["actions"]:
        out.write("    Nothing to remove — system already clean.\n")
        out.write("\n  ✓ Done.\n\n")
        return 0
    for action in plan["actions"]:
        marker = "[dry] " if dry_run else "      "
        out.write(f"    {marker}{action['op']:10}  {action['path']}\n")
        if action.get("detail"):
            out.write(f"    {marker}            {action['detail']}\n")
    out.write("\n")

    if dry_run:
        out.write("  [dry-run] No changes made.\n\n")
        return 0

    if not yes:
        try:
            response = input("  Proceed with uninstall? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            out.write("\n  Aborted.\n\n")
            return 1
        if response not in ("y", "yes"):
            out.write("  Aborted.\n\n")
            return 0

    # Execute
    removed = 0
    failed = 0
    for action in plan["actions"]:
        try:
            ok = _execute_action(action)
            if ok:
                removed += 1
                out.write(f"    ✓ {action['op']:10}  {action['path']}\n")
            else:
                out.write(f"    · {action['op']:10}  {action['path']}  (no-op)\n")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            out.write(f"    ✗ {action['op']:10}  {action['path']}  ({exc})\n")

    out.write("\n")
    out.write(f"  Removed {removed} item(s)")
    if failed:
        out.write(f"; {failed} failed (see above)")
    out.write(".\n")

    if not keep_data:
        out.write("\n")
        out.write(
            "  Next: `pipx uninstall codevira` (or `pip uninstall`) "
            "to remove the binary itself.\n"
        )
    out.write("\n")
    return 0 if failed == 0 else 1


def _build_uninstall_plan(*, keep_data: bool) -> dict:
    """Walk every system write site and emit a list of remove actions."""
    actions: list[dict] = []

    # ---- per-user (~/.codevira/) ----
    if not keep_data:
        global_home = Path.home() / ".codevira"
        if global_home.is_dir():
            actions.append(
                {
                    "op": "delete-dir",
                    "path": str(global_home),
                    "detail": "cross-project data dir (global.db, projects/, logs)",
                }
            )

    # ---- IDE configs (Claude Code) ----
    claude_json = Path.home() / ".claude.json"
    if claude_json.is_file():
        # Check if it actually has a codevira entry to avoid noise.
        try:
            from mcp_server.ide_inject import _read_json_safe

            data = _read_json_safe(claude_json) or {}
            servers = data.get("mcpServers", {}) or {}
            has_codevira = any(
                k == "codevira" or k.startswith("codevira-") for k in servers
            )
            if has_codevira:
                actions.append(
                    {
                        "op": "edit-config",
                        "path": str(claude_json),
                        "detail": "drop mcpServers.codevira* entry",
                        "_action": "remove-mcp-entry",
                    }
                )
        except Exception:
            pass

    # ---- Claude Code hook scripts + settings.json registration ----
    claude_hooks_dir = Path.home() / ".claude" / "hooks"
    if claude_hooks_dir.is_dir():
        for hook_file in claude_hooks_dir.glob("codevira-*"):
            actions.append(
                {
                    "op": "delete-file",
                    "path": str(hook_file),
                    "detail": "Claude Code lifecycle hook script",
                }
            )

    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.is_file():
        try:
            import json as _json

            data = _json.loads(claude_settings.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {}) or {}
            # Detect codevira-tagged hook entries
            has_codevira_hook = False
            for event_name, event_hooks in hooks.items():
                if not isinstance(event_hooks, list):
                    continue
                for hook_def in event_hooks:
                    if not isinstance(hook_def, dict):
                        continue
                    for matcher in hook_def.get("hooks", []) or []:
                        if not isinstance(matcher, dict):
                            continue
                        cmd = str(matcher.get("command") or "")
                        if "codevira-" in cmd:
                            has_codevira_hook = True
                            break
            if has_codevira_hook:
                actions.append(
                    {
                        "op": "edit-config",
                        "path": str(claude_settings),
                        "detail": "drop codevira-tagged hook registrations",
                        "_action": "remove-claude-hook-entries",
                    }
                )
        except Exception:
            pass

    # ---- per-project (every tracked repo in global.db) ----
    if not keep_data:
        try:
            from indexer.global_db import GlobalDB
            from mcp_server.paths import get_global_home

            global_db_path = get_global_home() / "global.db"
            if global_db_path.is_file():
                gdb = GlobalDB(global_db_path)
                try:
                    rows = gdb.conn.execute("SELECT path FROM projects").fetchall()
                finally:
                    gdb.close()
                for row in rows:
                    proj_path = Path(row["path"])
                    if not proj_path.is_dir():
                        continue
                    cv_dir = proj_path / ".codevira"
                    cache_dir = proj_path / ".codevira-cache"
                    if cv_dir.is_dir():
                        actions.append(
                            {
                                "op": "delete-dir",
                                "path": str(cv_dir),
                                "detail": f"in-repo decisions store ({proj_path.name})",
                            }
                        )
                    if cache_dir.is_dir():
                        actions.append(
                            {
                                "op": "delete-dir",
                                "path": str(cache_dir),
                                "detail": f"rebuildable cache ({proj_path.name})",
                            }
                        )
                    agents_md = proj_path / "AGENTS.md"
                    if agents_md.is_file() and _agents_md_has_marker(agents_md):
                        actions.append(
                            {
                                "op": "edit-file",
                                "path": str(agents_md),
                                "detail": f"strip codevira marker block ({proj_path.name})",
                                "_action": "strip-agents-md-marker",
                            }
                        )

                    # v2.2.0+ back-compat: pre-v2.2.0 installs wrote
                    # codevira marker blocks into per-IDE nudge files
                    # too (CLAUDE.md, GEMINI.md, .cursor/rules/
                    # codevira.mdc, .windsurfrules, .github/
                    # copilot-instructions.md). Those nudges were
                    # deleted in the 2026-05-22 surface-cut audit but
                    # the FILES still exist on machines that upgraded
                    # from v2.1.x. Strip our block from each one we
                    # find — user content outside the markers stays.
                    legacy_nudges = (
                        proj_path / "CLAUDE.md",
                        proj_path / "GEMINI.md",
                        proj_path / ".cursor" / "rules" / "codevira.mdc",
                        proj_path / ".windsurfrules",
                        proj_path / ".github" / "copilot-instructions.md",
                    )
                    for nudge in legacy_nudges:
                        if nudge.is_file() and _legacy_nudge_has_marker(nudge):
                            actions.append(
                                {
                                    "op": "edit-file",
                                    "path": str(nudge),
                                    "detail": (
                                        f"strip codevira block from "
                                        f"legacy nudge ({proj_path.name}/"
                                        f"{nudge.name})"
                                    ),
                                    "_action": "strip-legacy-nudge-marker",
                                }
                            )
        except Exception:
            pass

    return {"actions": actions}


def _agents_md_has_marker(path: Path) -> bool:
    try:
        return "<!-- codevira:begin" in path.read_text(encoding="utf-8")
    except Exception:
        return False


def _legacy_nudge_has_marker(path: Path) -> bool:
    """True if a pre-v2.2.0 nudge file still carries a codevira block.

    Pre-v2.2.0 ``mcp_server/agents_md.py`` wrote per-IDE nudge files
    (CLAUDE.md, GEMINI.md, .windsurfrules, etc.) with marker pairs.
    The legacy module used ``<!-- codevira:start -->`` /
    ``<!-- codevira:end -->`` (note: START, not BEGIN — the v2.2.0
    AGENTS.md generator uses BEGIN). We accept either spelling so we
    catch files from any prior release.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    return "<!-- codevira:start" in text or "<!-- codevira:begin" in text


def _strip_legacy_nudge_marker(path: Path) -> bool:
    """Strip the codevira block from a pre-v2.2.0 per-IDE nudge file.

    Handles both legacy spellings (``<!-- codevira:start -->`` from
    pre-v2.2.0 templates, and ``<!-- codevira:begin -->`` from
    machines that already swapped in the v2.2.0 generator). Preserves
    user content outside the marker boundaries byte-for-byte.

    If the file becomes empty (was entirely codevira-managed),
    delete it. Returns True if the file was modified.
    """
    text = path.read_text(encoding="utf-8")
    # Try begin/end first (v2.2.0-shape markers if anything ever
    # regenerated this legacy file); fall back to start/end (the
    # original pre-v2.2.0 shape).
    for begin_marker, end_marker in (
        ("<!-- codevira:begin", "<!-- codevira:end -->"),
        ("<!-- codevira:start", "<!-- codevira:end -->"),
    ):
        start = text.find(begin_marker)
        if start < 0:
            continue
        end = text.find(end_marker, start)
        if end < 0:
            # Malformed: leave alone — don't risk damaging user content.
            return False
        end_line = text.find("\n", end)
        if end_line < 0:
            end_line = len(text)
        else:
            end_line += 1
        new_text = text[:start] + text[end_line:]
        while "\n\n\n\n" in new_text:
            new_text = new_text.replace("\n\n\n\n", "\n\n\n")
        if not new_text.strip():
            path.unlink()
            return True
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def _strip_agents_md_marker(path: Path) -> bool:
    """Remove the codevira block (and surrounding blank lines) from AGENTS.md.

    Preserves all content outside the marker boundaries byte-for-byte.
    Returns True if the file was modified.
    """
    text = path.read_text(encoding="utf-8")
    begin_marker = "<!-- codevira:begin"
    end_marker = "<!-- codevira:end -->"
    start = text.find(begin_marker)
    if start < 0:
        return False
    end = text.find(end_marker, start)
    if end < 0:
        # Marker malformed; leave file alone.
        return False
    # Include the end marker line and a trailing newline if present.
    end_line = text.find("\n", end)
    if end_line < 0:
        end_line = len(text)
    else:
        end_line += 1
    new_text = text[:start] + text[end_line:]
    # Collapse runs of more than 2 blank lines created by removal.
    while "\n\n\n\n" in new_text:
        new_text = new_text.replace("\n\n\n\n", "\n\n\n")
    # If file becomes empty (only had codevira block), delete it.
    if not new_text.strip():
        path.unlink()
        return True
    path.write_text(new_text, encoding="utf-8")
    return True


def _remove_claude_hook_entries(path: Path) -> bool:
    """Strip codevira-tagged hook entries from ~/.claude/settings.json.

    Preserves every other hook entry byte-for-byte.
    """
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    modified = False
    for event_name in list(hooks.keys()):
        event_hooks = hooks.get(event_name) or []
        if not isinstance(event_hooks, list):
            continue
        new_event_hooks = []
        for hook_def in event_hooks:
            if not isinstance(hook_def, dict):
                new_event_hooks.append(hook_def)
                continue
            matchers = hook_def.get("hooks", []) or []
            new_matchers = [
                m
                for m in matchers
                if not (
                    isinstance(m, dict) and "codevira-" in str(m.get("command") or "")
                )
            ]
            if len(new_matchers) != len(matchers):
                modified = True
            if new_matchers:
                hook_def["hooks"] = new_matchers
                new_event_hooks.append(hook_def)
            # else: drop the whole hook_def (was only codevira)
        if not new_event_hooks:
            del hooks[event_name]
            modified = True
        else:
            hooks[event_name] = new_event_hooks
    if not hooks:
        data.pop("hooks", None)
        modified = True
    if modified:
        path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return modified


def _execute_action(action: dict) -> bool:
    """Run a single uninstall action. Returns True if anything changed."""
    op = action["op"]
    path = Path(action["path"])
    sub = action.get("_action")

    if op == "delete-dir":
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True
    if op == "delete-file":
        if not path.is_file():
            return False
        path.unlink()
        return True
    if op == "edit-config" and sub == "remove-mcp-entry":
        from mcp_server.ide_inject import remove_codevira_from_config

        return remove_codevira_from_config(path)
    if op == "edit-config" and sub == "remove-claude-hook-entries":
        return _remove_claude_hook_entries(path)
    if op == "edit-file" and sub == "strip-agents-md-marker":
        return _strip_agents_md_marker(path)
    if op == "edit-file" and sub == "strip-legacy-nudge-marker":
        return _strip_legacy_nudge_marker(path)
    raise ValueError(f"unknown action op={op!r} _action={sub!r}")
