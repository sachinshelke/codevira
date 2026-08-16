"""
cli_memory.py — ``codevira memory snapshot | list | undo``.

The user-facing half of :mod:`mcp_server.memory_undo`. See that module for
why this exists at all; the short version is that ``.codevira/`` is
gitignored, so the memory store is the one directory in the repo that
``git revert`` cannot help with.

Design notes that are really safety notes:

* ``undo`` CONFIRMS by default. It replaces a store that may hold months
  of a team's decisions, and the person running it is usually already
  having a bad day. ``--yes`` exists for scripts.
* ``--all-projects`` is offered because a bad migration is rarely
  confined to one project — but it prints the full list and confirms
  once, rather than prompting eleven times and training people to say
  yes without reading.
* Every failure is per-project. One project failing to restore must not
  abort the rest, or a partial recovery leaves the user worse off than
  when they started.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp_server import memory_undo


def _projects() -> list[Path]:
    """Every registered project root that still exists on disk."""
    try:
        from mcp_server._project_inventory import enumerate_projects

        roots: list[Path] = []
        for entry in enumerate_projects():
            # canonical_path_valid already excludes deleted dirs and
            # refused roots ($HOME, system top-levels) — restoring into
            # one of those is how ghost dirs got created in v1.8.0.
            if not getattr(entry, "canonical_path_valid", False):
                continue
            p = getattr(entry, "canonical_path", None)
            if p and Path(p).is_dir():
                roots.append(Path(p))
        return sorted(set(roots))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"  (could not enumerate projects: {exc})\n")
        return []


def _human(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1_048_576 else f"{n / 1_048_576:.1f} MB"


def _cmd_snapshot(args: Any) -> int:
    note = getattr(args, "note", "") or ""
    if getattr(args, "all_projects", False):
        roots = _projects()
        if not roots:
            sys.stderr.write("No registered projects found.\n")
            return 1
        ok = 0
        for root in roots:
            snap = memory_undo.take(root, note=note)
            if snap:
                ok += 1
                print(f"  ✓ {root.name:24s} {snap.name}  ({snap.files} files)")
            else:
                print(f"  – {root.name:24s} nothing to snapshot")
        print(f"\n{ok}/{len(roots)} project(s) snapshotted.")
        return 0 if ok else 1

    snap = memory_undo.take(None, note=note)
    if snap is None:
        sys.stderr.write(
            "Nothing to snapshot: this project has no .codevira/ directory.\n"
            "  Fix: run `codevira init` first.\n"
        )
        return 1
    print(
        f"✓ Snapshot {snap.name}\n"
        f"  {snap.files} files, {_human(snap.bytes)}\n"
        f"  {snap.path}\n"
        f"\nRestore it with: codevira memory undo --snapshot {snap.name}"
    )
    return 0


def _cmd_list(args: Any) -> int:
    roots = _projects() if getattr(args, "all_projects", False) else [None]
    found = 0
    for root in roots:
        snaps = memory_undo.list_snapshots(root)
        label = Path(root).name if root else "this project"
        if not snaps:
            if getattr(args, "all_projects", False):
                continue
            print(
                f"No snapshots for {label}.\n  Take one with: codevira memory snapshot"
            )
            return 0
        found += len(snaps)
        print(f"\n{label}:")
        for s in snaps:
            note = f"  — {s.note}" if s.note else ""
            print(f"  {s.name}  {s.files:3d} files  {_human(s.bytes):>9s}{note}")
    if getattr(args, "all_projects", False):
        print(f"\n{found} snapshot(s) across {len(roots)} project(s).")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _cmd_undo(args: Any) -> int:
    name = getattr(args, "snapshot", None)
    assume_yes = getattr(args, "yes", False)

    if getattr(args, "all_projects", False):
        roots = [r for r in _projects() if memory_undo.list_snapshots(r)]
        if not roots:
            sys.stderr.write("No project has a snapshot to restore.\n")
            return 1
        print("This will replace .codevira/ in:")
        for r in roots:
            snaps = memory_undo.list_snapshots(r)
            print(f"  {r.name:24s} -> {snaps[0].name}")
        # One prompt showing the whole list, rather than eleven prompts
        # that teach people to answer without reading.
        if not assume_yes and not _confirm(f"\nRestore {len(roots)} project(s)?"):
            print("Aborted. Nothing changed.")
            return 1

        failures = 0
        for r in roots:
            res = memory_undo.restore(r, snapshot=None)
            if res.get("success"):
                print(f"  ✓ {r.name:24s} restored {res['restored']}")
            else:
                failures += 1
                # Keep going: a partial recovery that stops halfway leaves
                # the user worse off than when they started.
                print(f"  ✗ {r.name:24s} {res.get('error')}")
        print(f"\n{len(roots) - failures}/{len(roots)} restored.")
        return 1 if failures else 0

    snaps = memory_undo.list_snapshots(None)
    if not snaps:
        sys.stderr.write(
            "No snapshots exist for this project, so there is nothing to undo.\n"
            "  Take one before your next risky operation:\n"
            "    codevira memory snapshot\n"
        )
        return 1

    target = next((s for s in snaps if s.name == name), None) if name else snaps[0]
    if target is None:
        sys.stderr.write(f"No snapshot named {name!r}.\n  See: codevira memory list\n")
        return 1

    print(
        f"This will replace .codevira/ with snapshot {target.name}\n"
        f"  taken {target.taken_at}  ({target.files} files)"
        + (f"\n  note: {target.note}" if target.note else "")
    )
    if not assume_yes and not _confirm("\nProceed?"):
        print("Aborted. Nothing changed.")
        return 1

    res = memory_undo.restore(None, snapshot=target.name)
    if not res.get("success"):
        sys.stderr.write(f"Restore failed: {res.get('error')}\n")
        if res.get("safety_snapshot"):
            sys.stderr.write(
                f"  Your prior state is safe in snapshot {res['safety_snapshot']}.\n"
            )
        return 1

    print(
        f"\n✓ Restored {res['restored']} ({res['files']} files)\n"
        f"  Your previous state was captured first as {res.get('safety_snapshot')},\n"
        f"  so this undo is itself undoable.\n"
        f"\nRebuild derived indexes with: codevira sync"
    )
    return 0


def cmd_memory(args: Any) -> int:
    action = getattr(args, "memory_action", None)
    if action == "snapshot":
        return _cmd_snapshot(args)
    if action == "list":
        return _cmd_list(args)
    if action == "undo":
        return _cmd_undo(args)
    sys.stderr.write(
        "codevira memory: missing subcommand.\n"
        "  codevira memory snapshot [--note TEXT] [--all-projects]\n"
        "  codevira memory list [--all-projects]\n"
        "  codevira memory undo [--snapshot NAME] [--all-projects] [--yes]\n"
    )
    return 2
