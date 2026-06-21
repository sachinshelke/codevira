"""
cli_search.py — the `codevira search` command.

Brings decision search to the terminal. Until v3.6.0, `search_decisions` was
MCP-only (AI agents could search; humans couldn't from a shell). This wraps the
same tool so you can:

    codevira search "retry policy"
    codevira search "retry policy" --all-projects     # every registered repo
    codevira search "auth" --json                      # machine-readable

``--all-projects`` (v3.6.0) merges BM25-ranked matches from every registered
project, each row tagged with the project it came from.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _clamp_limit(value: int | None, default: int = 10) -> int:
    try:
        return max(1, min(int(value), 50)) if value is not None else default
    except (TypeError, ValueError):
        return default


def cmd_search(
    *,
    query: str | None,
    all_projects: bool = False,
    limit: int | None = 10,
    full: bool = False,
    output_json: bool = False,
) -> int:
    """Search decisions and print the results. Returns a process exit code.

    Args:
        query: search terms (FTS5/BM25). Empty → usage error (exit 2).
        all_projects: search every registered project, not just the current.
        limit: max rows (clamped to [1, 50]; default 10).
        full: include untruncated decision text + snippet/origin.
        output_json: emit the raw tool payload as JSON instead of a table.

    Returns:
        0 on success (including zero matches — not an error), 2 on empty query.
    """
    q = (query or "").strip()
    if not q:
        print(
            "Usage: codevira search <query> [--all-projects] [--limit N] "
            "[--full] [--json]",
            file=sys.stderr,
        )
        return 2

    limit = _clamp_limit(limit)

    from mcp_server.tools.search import search_decisions

    result = search_decisions(q, limit=limit, full=full, all_projects=all_projects)
    rows: list[dict[str, Any]] = result.get("results", [])

    if output_json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    _render_table(q, rows, all_projects=all_projects)
    return 0


def _render_table(
    query: str, rows: list[dict[str, Any]], *, all_projects: bool
) -> None:
    """Pretty-print results as a rich table, with a plain-text fallback."""
    scope = "all projects" if all_projects else "this project"
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        if not rows:
            console.print(f"No decisions matched [bold]{query!r}[/bold] in {scope}.")
            return

        table = Table(
            title=f"Decisions matching {query!r} ({scope})",
            title_style="bold green",
            show_lines=False,
        )
        table.add_column("ID", style="cyan", no_wrap=True)
        if all_projects:
            table.add_column("Project", style="magenta", no_wrap=True)
        table.add_column("Decision")
        table.add_column("File", style="dim", no_wrap=True)
        table.add_column("🔒", justify="center", no_wrap=True)

        for r in rows:
            cells = [str(r.get("id") or "?")]
            if all_projects:
                cells.append(str(r.get("project") or "?"))
            cells.append(str(r.get("decision") or ""))
            cells.append(str(r.get("file_path") or ""))
            cells.append("🔒" if r.get("do_not_revert") else "")
            table.add_row(*cells)
        console.print(table)
    except Exception:  # noqa: BLE001 — never let rendering break the command
        # Plain fallback (also covers environments without a real rich).
        if not rows:
            print(f"No decisions matched {query!r} in {scope}.")
            return
        for r in rows:
            prefix = f"[{r.get('project')}] " if all_projects else ""
            lock = " (locked)" if r.get("do_not_revert") else ""
            print(f"  #{r.get('id')}: {prefix}{r.get('decision')}{lock}")
