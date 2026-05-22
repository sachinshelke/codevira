"""``codevira hooks list / uninstall`` — P2-6 (rc.5 audit, 2026-05-13).

Pre-fix, ``codevira hooks`` only supported ``install``. Removing hooks meant
deleting files in ``~/.claude/hooks/`` AND hand-editing
``~/.claude/settings.json``. These admin subcommands close that gap.

Kept in its own module so additions don't inflate the public surface of
``mcp_server.cli`` (high blast radius).
"""

from __future__ import annotations

import json
from pathlib import Path

# All five hook events codevira installs. Kept in sync with
# mcp_server/data/hooks/codevira-*.sh templates.
_HOOK_NAMES: tuple[str, ...] = (
    "session_start",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "stop",
)


def _hook_dir() -> Path:
    return Path.home() / ".claude" / "hooks"


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def cmd_hooks_list() -> int:
    """Print one row per installed codevira-* hook script.

    Columns: script path, exists?, size (bytes), registered in settings.json?
    """
    print()
    print("  Codevira — Installed Claude Code Hooks")
    print("  " + "─" * 40)

    hooks_dir = _hook_dir()
    settings_path = _settings_path()

    registered: set[str] = set()
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text())
            hooks_block = data.get("hooks") or {}
            for event_list in hooks_block.values():
                for entry in event_list or []:
                    for h in entry.get("hooks", []) or []:
                        cmd = h.get("command", "")
                        # cmd looks like "bash /path/to/codevira-<event>.sh"
                        for n in _HOOK_NAMES:
                            if f"codevira-{n}" in cmd:
                                registered.add(n)
        except Exception:
            pass

    any_present = False
    for name in _HOOK_NAMES:
        path = hooks_dir / f"codevira-{name}.sh"
        present = path.is_file()
        any_present = any_present or present
        size = path.stat().st_size if present else 0
        reg = name in registered
        marker_present = "✓" if present else "✗"
        marker_reg = "✓" if reg else "✗"
        print(
            f"    {marker_present} script    {marker_reg} registered    "
            f"{size:>5} B   {path}"
        )

    print()
    if not any_present:
        print("  No codevira hooks installed. Run `codevira hooks install`.")
    elif len(registered) < len(_HOOK_NAMES) and any_present:
        print(
            "  ⚠  Some scripts are present but not registered in "
            "settings.json — run `codevira hooks install` to re-register."
        )
    return 0


def cmd_hooks_uninstall(*, dry_run: bool = False, yes: bool = False) -> int:
    """Remove every codevira-* hook script and unregister from settings.json.

    Preserves other entries in both ``~/.claude/hooks/`` and
    ``~/.claude/settings.json`` — only codevira-owned content is touched.
    """
    hooks_dir = _hook_dir()
    settings_path = _settings_path()

    targets = [hooks_dir / f"codevira-{n}.sh" for n in _HOOK_NAMES]
    existing = [t for t in targets if t.is_file()]

    print()
    print("  Codevira — Uninstall Claude Code Hooks")
    print("  " + "─" * 40)
    print()
    if not existing and not settings_path.is_file():
        print(
            "  Nothing to remove — no codevira hook scripts found, " "no settings.json."
        )
        return 0

    print(f"  Would remove {len(existing)} hook script(s):")
    for t in existing:
        print(f"    • {t}")

    settings_will_change = False
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text())
            hooks_block = data.get("hooks") or {}
            for entries in hooks_block.values():
                for entry in entries or []:
                    if any(
                        "codevira-" in h.get("command", "")
                        for h in entry.get("hooks", []) or []
                    ):
                        settings_will_change = True
                        break
                if settings_will_change:
                    break
        except Exception:
            pass
    if settings_will_change:
        print(f"    • drop codevira entries from {settings_path}")

    if dry_run:
        print()
        print("  [dry-run] No changes made.")
        return 0

    if not yes:
        from mcp_server._prompts import confirm

        if not confirm("Proceed with uninstall?", default=False):
            print("  Aborted.")
            return 0

    print()
    removed = 0
    for t in existing:
        try:
            t.unlink()
            print(f"  ✓ removed {t.name}")
            removed += 1
        except Exception as exc:
            print(f"  ✗ {t.name}: {exc}")

    if settings_path.is_file() and settings_will_change:
        try:
            data = json.loads(settings_path.read_text())
            hooks_block = data.get("hooks") or {}
            new_hooks: dict = {}
            for event, entries in hooks_block.items():
                kept_entries = []
                for entry in entries or []:
                    kept_inner = [
                        h
                        for h in (entry.get("hooks") or [])
                        if "codevira-" not in h.get("command", "")
                    ]
                    if kept_inner:
                        new_entry = dict(entry)
                        new_entry["hooks"] = kept_inner
                        kept_entries.append(new_entry)
                if kept_entries:
                    new_hooks[event] = kept_entries
            if new_hooks:
                data["hooks"] = new_hooks
            elif "hooks" in data:
                del data["hooks"]
            # v3.0.0 round-3: shared atomic-write helper (was a fixed
            # ``.json.tmp`` suffix — race shape if two unregister
            # commands ran concurrently).
            from mcp_server.storage.atomic import atomic_write_text

            atomic_write_text(settings_path, json.dumps(data, indent=2))
            print(f"  ✓ unregistered codevira from {settings_path.name}")
        except Exception as exc:
            print(f"  ✗ failed to update {settings_path}: {exc}")

    print()
    print(f"  Done: removed {removed} hook script(s).")
    return 0
