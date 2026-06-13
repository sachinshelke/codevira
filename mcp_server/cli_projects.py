"""``codevira projects`` — inventory of every project tracked on this machine.

rc.5 (P0-2/3/4 + P2-2/3/4): now reads from the shared
:mod:`mcp_server._project_inventory` helper, so this surface and
``status --global`` and ``clean --dry-run`` all report the SAME numbers.
JSON output and the rendered table both expose ``status`` and ``last_synced_at``.

Subcommands:
  * ``codevira projects`` — list everything (tracked, ghost, orphan).
  * ``codevira projects --json`` — machine-readable.
  * ``codevira projects --ghosts-only`` — pair with ``codevira clean --ghosts``.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


def cmd_projects(
    *,
    output_json: bool = False,
    ghosts_only: bool = False,
    show_paths: bool = False,
    show_all: bool = False,
) -> int:
    """Print the project inventory. Returns POSIX exit code 0.

    Parameters
    ----------
    output_json
        If True, emit JSON to stdout instead of the human-readable table.
        JSON is full-fidelity: every row is included and tagged with an
        ``ephemeral`` flag (rather than hidden).
    ghosts_only
        If True, list only entries with status ``ghost`` (incomplete dirs).
    show_paths
        2026-05-17 Bug G partial fix: print ``<project_path>  →  <data_dir>``
        per line, so users can locate the on-disk data dir for any project
        by name. Replaces the need to manually translate the long hash-based
        slug back to a project basename.
    show_all
        If True, include ephemeral test/scratch paths (pytest tmp dirs,
        ``/tmp`` scratch) in the human views. They are hidden by default
        with a one-line note (v3.4.0).
    """
    from mcp_server._project_inventory import enumerate_projects, summarize

    all_entries = enumerate_projects()

    if output_json:
        # Full fidelity: include everything, tag ephemeral rows so jq
        # consumers can filter without re-deriving the rule.
        json_entries = (
            all_entries
            if not ghosts_only
            else [e for e in all_entries if e.status == "ghost"]
        )
        out = {
            "summary": summarize(all_entries),
            "projects": [
                {**_entry_to_dict(e), "ephemeral": _is_ephemeral_entry(e)}
                for e in json_entries
            ],
        }
        # 2026-05-17 Bug G: always include data_dir in JSON output (was absent).
        # Now `jq` consumers can locate the on-disk path without re-deriving.
        from mcp_server.paths import get_global_home

        for p in out["projects"]:
            if p.get("slug"):
                p["data_dir"] = str(get_global_home() / "projects" / p["slug"])
        print(json.dumps(out, indent=2, default=str))
        return 0

    # Human views hide ephemeral test/scratch rows by default (v3.4.0).
    entries = all_entries
    ephemeral_hidden = 0
    if not show_all:
        kept = [e for e in all_entries if not _is_ephemeral_entry(e)]
        ephemeral_hidden = len(all_entries) - len(kept)
        entries = kept

    # Summary reflects the real (non-ephemeral) project set.
    summary = summarize(entries)

    if ghosts_only:
        entries = [e for e in entries if e.status == "ghost"]

    if show_paths:
        _print_paths(entries)
        return 0

    _print_table(entries, summary, ghosts_only=ghosts_only)
    if ephemeral_hidden:
        plural = "s" if ephemeral_hidden != 1 else ""
        print(
            f"  ({ephemeral_hidden} ephemeral test/scratch path{plural} hidden "
            f"— run `codevira projects --all` to show)"
        )
    return 0


def _is_ephemeral_entry(entry) -> bool:
    """True if a ProjectEntry's canonical path is ephemeral test/scratch
    space (pytest tmp dir, /tmp scratch). Best-effort — never raises."""
    cp = getattr(entry, "canonical_path", None)
    if not cp:
        return False
    try:
        from mcp_server.paths import is_ephemeral_project_path

        return is_ephemeral_project_path(Path(cp))
    except Exception:  # noqa: BLE001
        return False


def _print_paths(entries) -> None:
    """2026-05-17 Bug G partial fix: per-line ``project → data_dir`` output.

    Users with the long-form slug on disk (``Users_sachin_..._6d2f5d4d``)
    can now grep:
        codevira projects --paths | grep lh-interface

    and see the data dir absolute path for that project.
    """
    from mcp_server.paths import get_global_home

    projects_root = get_global_home() / "projects"
    if not entries:
        print("  (no projects tracked)")
        return
    # Compute column widths for alignment.
    max_src = max(
        (len(e.canonical_path or e.slug or "?") for e in entries),
        default=1,
    )
    max_src = min(max_src, 80)  # cap to keep terminal-friendly
    for e in entries:
        src = e.canonical_path or e.slug or "?"
        data = str(projects_root / e.slug) if e.slug else "(no data dir)"
        print(f"  {src:<{max_src}}  →  {data}")


def _entry_to_dict(entry) -> dict:
    """Serialise a ProjectEntry dataclass to a flat dict for JSON output."""
    d = dataclasses.asdict(entry)
    d["status"] = entry.status  # @property — not in asdict
    return d


def _human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}T"


def _short_time(iso: str | None) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD`` for the table."""
    if not iso:
        return "—"
    # SQLite CURRENT_TIMESTAMP yields ``YYYY-MM-DD HH:MM:SS``; just take the date.
    return iso.split(" ", 1)[0].split("T", 1)[0]


def _relative_age(iso: str | None) -> str:
    """Human relative age for a last-sync timestamp (v3.4.0).

    Returns ``today`` / ``yesterday`` / ``Nd ago`` (under 30 days) /
    ``stale Nd`` (30 days or more), or ``—`` when unknown. Parses
    SQLite's ``YYYY-MM-DD HH:MM:SS`` (UTC) form; falls back to the bare
    date if parsing fails.
    """
    if not iso:
        return "—"
    from datetime import datetime, timezone

    try:
        s = iso.strip().replace("T", " ").split(".", 1)[0]
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _short_time(iso)
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    return f"stale {days}d"


def cmd_projects_archive(name: str | None) -> int:
    """Remove a project from the cross-machine registry (``global.db``).

    Resolves ``name`` against the registered project's full path, then its
    name / basename (case-insensitive). Only the registry row is deleted —
    the project's files and any ``~/.codevira/projects/<slug>`` data dir
    are left untouched, so this is a safe "stop listing this" action.

    Args:
        name: Project name, basename, or full path to remove.

    Returns:
        POSIX exit code: 0 removed, 1 not found / nothing removed,
        2 usage error or ambiguous match.
    """
    if not name or not name.strip():
        sys.stderr.write(
            "codevira projects archive: missing <name>.\n"
            "  Usage: codevira projects archive <project-name-or-path>\n"
        )
        return 2

    from mcp_server._project_inventory import enumerate_projects

    entries = [e for e in enumerate_projects() if e.canonical_path]
    targets = _resolve_archive_targets(entries, name.strip())

    if not targets:
        sys.stderr.write(
            f"codevira projects archive: no registered project matches "
            f"{name!r}.\n  Run `codevira projects` to see tracked names / paths.\n"
        )
        return 1
    if len(targets) > 1:
        sys.stderr.write(
            f"codevira projects archive: {name!r} is ambiguous — matches "
            f"{len(targets)} projects:\n"
        )
        for p in targets:
            sys.stderr.write(f"    {p}\n")
        sys.stderr.write("  Re-run with the full path to disambiguate.\n")
        return 2

    path = targets[0]
    if _delete_project_row(path):
        sys.stdout.write(f"✓ Archived {path} (removed from the registry).\n")
        return 0
    sys.stderr.write(
        f"codevira projects archive: {path} was not in the registry "
        f"(nothing to remove).\n"
    )
    return 1


def _resolve_archive_targets(entries: list, name: str) -> list[str]:
    """Resolve ``name`` to a list of matching canonical paths.

    Exact full-path match wins outright; otherwise case-insensitive match
    on the registered name or the path basename. De-duplicated, order
    preserved.
    """
    exact = [e.canonical_path for e in entries if e.canonical_path == name]
    if exact:
        return list(dict.fromkeys(exact))

    name_l = name.lower()
    out: list[str] = []
    for e in entries:
        cp = e.canonical_path
        if not cp:
            continue
        if name_l in (Path(cp).name.lower(), (e.name or "").lower()):
            out.append(cp)
    return list(dict.fromkeys(out))


def _delete_project_row(path: str) -> bool:
    """Delete a project row from ``global.db``. Returns True if a row was
    removed. Best-effort: a missing / unreadable DB returns False."""
    import sqlite3

    from mcp_server.paths import get_global_db_path

    db_path = get_global_db_path()
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute("DELETE FROM projects WHERE path = ?", (path,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _print_table(entries: list, summary: dict, *, ghosts_only: bool = False) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not entries:
        if ghosts_only:
            console.print("[green]✓ No ghost projects on this machine.[/green]")
        else:
            console.print(
                "No projects tracked yet — run a codevira command from a "
                "project to register it, or `codevira setup`."
            )
        return

    # P2-2 (rc.5): cap project column width and use ellipsis instead of
    # character-by-character wrapping for long slug names.
    table = Table(title="Codevira projects", show_lines=False)
    table.add_column("status", justify="center", no_wrap=True)
    table.add_column("project", overflow="ellipsis", max_width=44)
    table.add_column("last sync", justify="center", no_wrap=True)  # P2-3
    table.add_column("config", justify="center", no_wrap=True)
    table.add_column("metadata", justify="center", no_wrap=True)
    table.add_column("graph", justify="center", no_wrap=True)
    table.add_column("index", justify="center", no_wrap=True)
    table.add_column("global.db", justify="center", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)

    status_marker = {
        "tracked": "[green]✓[/green]",
        "ghost": "[yellow]⚠[/yellow]",
        "orphan": "[red]✗[/red]",
        "stale": "[dim]·[/dim]",
    }
    check = lambda b: "[green]✓[/green]" if b else "[dim]·[/dim]"  # noqa: E731

    for e in entries:
        # Prefer canonical_path (real project location) over slug for display.
        project_label = e.canonical_path or f"(no metadata) {e.slug}"
        table.add_row(
            status_marker.get(e.status, "?"),
            project_label,
            _relative_age(e.last_synced_at),
            check(e.has_config),
            check(e.has_metadata),
            check(e.has_graph),
            check(e.has_codeindex),
            check(e.in_global_db),
            _human_size(e.size_bytes),
        )

    console.print(table)
    # Summary uses the canonical "tracked / ghost / orphan / stale" names
    # so it matches `status --global` and `clean --dry-run`.
    parts = [
        f"[green]{summary['tracked']} tracked[/green]",
        f"[yellow]{summary['ghost']} ghost[/yellow]",
        f"[red]{summary['orphan']} orphan[/red]",
    ]
    if summary["stale"]:
        parts.append(f"[dim]{summary['stale']} stale[/dim]")
    console.print("  " + " · ".join(parts))
    if summary["ghost"] or summary["orphan"]:
        console.print(
            "  → [bold]codevira clean --ghosts[/bold] removes ghost dirs; "
            "[bold]--orphans[/bold] removes orphans."
        )
