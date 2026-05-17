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


def cmd_projects(
    *,
    output_json: bool = False,
    ghosts_only: bool = False,
    show_paths: bool = False,
) -> int:
    """Print the project inventory. Returns POSIX exit code 0.

    Parameters
    ----------
    output_json
        If True, emit JSON to stdout instead of the human-readable table.
    ghosts_only
        If True, list only entries with status ``ghost`` (incomplete dirs).
    show_paths
        2026-05-17 Bug G partial fix: print ``<project_path>  →  <data_dir>``
        per line, so users can locate the on-disk data dir for any project
        by name. Replaces the need to manually translate the long hash-based
        slug back to a project basename.
    """
    from mcp_server._project_inventory import enumerate_projects, summarize

    entries = enumerate_projects()
    summary = summarize(entries)

    if ghosts_only:
        entries = [e for e in entries if e.status == "ghost"]

    if output_json:
        out = {
            "summary": summary,
            "projects": [_entry_to_dict(e) for e in entries],
        }
        # 2026-05-17 Bug G: always include data_dir in JSON output (was absent).
        # Now `jq` consumers can locate the on-disk path without re-deriving.
        from mcp_server.paths import get_global_home

        for p in out["projects"]:
            if p.get("slug"):
                p["data_dir"] = str(get_global_home() / "projects" / p["slug"])
        print(json.dumps(out, indent=2, default=str))
        return 0

    if show_paths:
        _print_paths(entries)
        return 0

    _print_table(entries, summary, ghosts_only=ghosts_only)
    return 0


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
    check = lambda b: ("[green]✓[/green]" if b else "[dim]·[/dim]")  # noqa: E731

    for e in entries:
        # Prefer canonical_path (real project location) over slug for display.
        project_label = e.canonical_path or f"(no metadata) {e.slug}"
        table.add_row(
            status_marker.get(e.status, "?"),
            project_label,
            _short_time(e.last_synced_at),
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
