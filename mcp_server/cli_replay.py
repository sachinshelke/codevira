"""
cli_replay.py — Hero 8's `codevira replay` command.

Surfaces the decisions timeline in 3 formats: terminal (default),
markdown, html.

Bug-8 lesson applied: ``--project`` runs through
``is_invalid_project_root()`` for parity with the wiring layer.

v2.2.0+: the `_parse_since` and `_clamp_top` helpers (formerly imported
from cli_insights which was deleted in the surface-cut audit) are
inlined below to keep `codevira replay` self-contained.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([dwhy]?)\s*$", re.IGNORECASE)


def _parse_since(value: str | None, default_days: int = 7) -> int:
    """Parse a since-arg like '7d', '2w', '30d' into integer days.

    On malformed input, logs a warning and returns ``default_days``.
    Bounded to 1..365 days.
    """
    if not value:
        return default_days
    m = _SINCE_RE.match(value)
    if not m:
        logger.warning(
            "codevira replay --since: invalid %r, using %dd", value, default_days
        )
        return default_days
    n = int(m.group(1))
    unit = (m.group(2) or "d").lower()
    multiplier = {"d": 1, "w": 7, "y": 365, "h": 0}.get(unit, 1)
    days = n * multiplier
    if days < 1:
        return 1
    if days > 365:
        return 365
    return days


def _clamp_top(value: int | None, default: int = 5) -> int:
    """Clamp the --top argument to [1, 200]."""
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, 200))


def cmd_replay(
    *,
    query: str | None = None,
    since: str | None = None,
    top: int | None = None,
    format: str = "terminal",  # noqa: A002 — match the CLI flag name
    project: Path | None = None,
    ascii_mode: bool = False,
    out_file: Path | None = None,
    out: IO[str] | None = None,  # for testability
) -> int:
    """Render the decisions timeline. Returns exit code.

    Returns:
      0 on success
      1 on data-layer failure or invalid --project
    """
    out = out or sys.stdout

    since_days = _parse_since(since)
    top_n = _clamp_top(top)

    fmt = (format or "terminal").lower()
    if fmt not in ("terminal", "markdown", "html"):
        out.write(
            f"Error: --format must be one of terminal, markdown, html "
            f"(got {format!r})\n"
        )
        return 1

    # Resolve project root + apply Bug-8 defense
    try:
        from mcp_server.paths import (
            get_project_root,
            set_project_dir,
            invalidate_data_dir_cache,
            is_invalid_project_root,
        )

        if project is not None:
            resolved = Path(project).resolve()
            rejection = is_invalid_project_root(resolved)
            if rejection:
                out.write(
                    f"Error: --project {project!r} is not a valid project "
                    f"root: {rejection}\n"
                    f"Use a directory with a .git, pyproject.toml, "
                    f"package.json, or similar project marker.\n"
                )
                return 1
            set_project_dir(resolved)
            invalidate_data_dir_cache()
        project_root = get_project_root()
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not resolve project — {e}\n")
        return 1

    # v2.2.0+: JSONL is the only storage layer. If `.codevira/` isn't
    # initialized, surface a friendly hint pointing at `codevira init`.
    try:
        from mcp_server.storage import paths as store_paths
        from mcp_server.decision_replay import (
            build_timeline,
            render_terminal,
            render_markdown,
            render_html,
        )
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not import replay module — {e}\n")
        return 1

    if not store_paths.is_initialized():
        out.write(
            f"No codevira data found in {project_root}.\n"
            "Run `codevira init` to bootstrap .codevira/ in this project, "
            "use codevira for a few sessions, then try again.\n"
        )
        return 0

    try:
        timeline = build_timeline(
            query=query,
            since_days=since_days,
            limit=top_n,
        )
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not build timeline — {e}\n")
        return 1

    title = f"Codevira Replay — {project_root.name}"
    if query:
        title += f" — query: {query!r}"
    title += f" — last {since_days} days"

    if fmt == "terminal":
        lines = render_terminal(timeline, ascii_mode=ascii_mode, title=title)
        rendered = "\n".join(lines) + "\n"
    elif fmt == "markdown":
        rendered = render_markdown(timeline, title=title) + "\n"
    else:  # fmt == "html"
        rendered = render_html(timeline, title=title)

    if out_file is not None:
        try:
            from mcp_server.storage.atomic import atomic_write_text

            atomic_write_text(Path(out_file), rendered)
            # P1-7 (rc.5): report BYTES, not character count. The previous
            # code used len(rendered) which is the number of Unicode code
            # points — but multibyte UTF-8 characters (📌 emoji etc. in the
            # HTML output) made the on-disk size larger than the reported
            # size by however many extra bytes those characters needed.
            byte_size = len(rendered.encode("utf-8"))
            out.write(f"Wrote {out_file} ({byte_size} bytes)\n")
        except OSError as e:
            out.write(f"Error: could not write {out_file}: {e}\n")
            return 1
    else:
        out.write(rendered)

    return 0
