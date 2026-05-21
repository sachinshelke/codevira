"""
cli_budget.py — `codevira budget` subcommand.

Reads ``token_budget.jsonl`` (populated by Hero 6's STOP policy) and
surfaces session-level token spend for the user.

Three modes:
  codevira budget                  → most-recent session summary
  codevira budget history          → last 10 sessions, one per line
  codevira budget history --last N → last N sessions (clamped 1-100)

Implementation reuses the Week-2 ``read_session_history(project_root,
limit=N)`` helper, which already caps tail-window reads at 16 MiB to
prevent OOM on huge logs.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------
# Public entry — called by mcp_server.cli
# ---------------------------------------------------------------------


def cmd_budget(
    *,
    show_history: bool = False,
    last: int = 10,
    full: bool = False,
    project: Path | None = None,
) -> int:
    """`codevira budget` orchestrator. Returns POSIX exit code:
        0 on success / empty state
        1 on bad project root (no codevira project at cwd / --project)
    """
    project_root = _resolve_project(project)
    if project_root is None:
        return 1

    last = _clamp_last(last)

    from mcp_server.engine.token_meter import read_session_history
    sessions = read_session_history(project_root, limit=last)

    if not sessions:
        print(_empty_state_message(project_root))
        return 0

    if show_history:
        _print_history(sessions, full=full)
    else:
        # Default: show most-recent session in detail
        _print_recent(sessions[0], full=full)

    return 0


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _resolve_project(explicit: Path | None) -> Path | None:
    """Resolve which project to read. ``--project /path`` overrides
    auto-detection from cwd. Returns None on rejection (with a
    user-visible error printed to stderr).
    """
    if explicit is not None:
        if not explicit.exists() or not explicit.is_dir():
            print(
                f"Error: --project {explicit} is not a directory.",
                file=sys.stderr,
            )
            print(
                "  → pass an existing project directory, "
                "or omit --project to use the current directory.",
                file=sys.stderr,
            )
            return None
        return explicit.resolve()

    # Auto-detect from cwd via the existing project-root resolver
    from mcp_server.paths import get_project_root, is_invalid_project_root
    project = get_project_root()
    rejection = is_invalid_project_root(project)
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into your project, or use `codevira budget --project /path`",
            file=sys.stderr,
        )
        return None
    return project


def _clamp_last(n: int) -> int:
    """Clamp --last to [1, 100]."""
    if n < 1:
        return 1
    if n > 100:
        return 100
    return n


def _empty_state_message(project_root: Path) -> str:
    return (
        f"No sessions recorded yet for {project_root.name}.\n"
        f"Codevira persists session token totals at session end "
        f"(STOP hook). Open Claude Code (or any AI tool with codevira "
        f"hooks installed) and complete a session to populate this log."
    )


# ---------------------------------------------------------------------
# Most-recent session detail view
# ---------------------------------------------------------------------


def _print_recent(session: dict[str, Any], *, full: bool = False) -> None:
    sid = session.get("session_id", "unknown")
    ended_at = _format_timestamp(session.get("ended_at"))
    injected = int(session.get("injected_total", 0))
    used = int(session.get("used_total", 0))
    eff_pct = float(session.get("efficiency", 0.0)) * 100.0
    wasted_sources = session.get("top_wasted_sources", []) or []

    print()
    print(f"  Session {sid}  ({ended_at})")
    print("  " + "─" * 60)
    print(f"  Injected:   {injected:>10,} tokens")
    print(f"  Used:       {used:>10,} tokens")
    print(f"  Efficiency: {eff_pct:>10.1f}%")

    if wasted_sources:
        print()
        print("  Top wasted sources:")
        limit = len(wasted_sources) if full else 3
        for src in wasted_sources[:limit]:
            name = src.get("source", "?")
            wasted = int(src.get("wasted", 0))
            inj = int(src.get("injected", 0))
            pct = (wasted / inj * 100.0) if inj else 0.0
            print(
                f"    {name:30s} "
                f"{inj:>7,} injected, {wasted:>7,} wasted ({pct:.0f}%)"
            )
        if not full and len(wasted_sources) > 3:
            print(f"    ... and {len(wasted_sources) - 3} more (use --full to see all)")
    else:
        print()
        print("  No wasted sources recorded.")
    print()


# ---------------------------------------------------------------------
# History listing view
# ---------------------------------------------------------------------


def _print_history(sessions: list[dict[str, Any]], *, full: bool = False) -> None:
    print()
    print(f"  Last {len(sessions)} session(s):")
    print("  " + "─" * 60)
    print(f"  {'Date':<19}  {'Session':<24}  {'Injected':>9}  {'Used':>9}  {'Eff':>5}")
    for s in sessions:
        ended_at = _format_timestamp(s.get("ended_at"))
        sid = (s.get("session_id") or "?")[:24]
        injected = int(s.get("injected_total", 0))
        used = int(s.get("used_total", 0))
        eff_pct = float(s.get("efficiency", 0.0)) * 100.0
        print(
            f"  {ended_at:<19}  {sid:<24}  "
            f"{injected:>9,}  {used:>9,}  {eff_pct:>4.0f}%"
        )

    if full:
        # Per-session full breakdown
        print()
        print("  Per-source breakdown:")
        for s in sessions:
            wasted = s.get("top_wasted_sources", []) or []
            if not wasted:
                continue
            sid = s.get("session_id", "?")
            print(f"  {sid}:")
            for src in wasted:
                name = src.get("source", "?")
                w = int(src.get("wasted", 0))
                i = int(src.get("injected", 0))
                print(f"    {name:30s}  {i:>7,} inj, {w:>7,} wasted")
    print()


def _format_timestamp(ts: Any) -> str:
    """Format epoch seconds (or ISO string) as `YYYY-MM-DD HH:MM`.
    Returns ``????-??-??`` on unparseable input.
    """
    if ts is None:
        return "????-??-??-?:?:?"
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(
                float(ts), tz=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M")
        return datetime.fromisoformat(
            str(ts).replace("Z", "+00:00"),
        ).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "????-??-?? ??:??"
