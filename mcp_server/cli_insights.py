"""
cli_insights.py — Hero 10's `codevira insights` command.

Pretty-prints the project's promotion-score digest:
  • Top stable decisions (high score)
  • Top reverted decisions (low score; "AI keeps trying, you keep undoing")
  • High-confidence learned rules

Reads the same data the SessionStart inject reads — through
``mcp_server.engine.promotion_score`` — but with a longer / configurable
time window and richer formatting.
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------

#: e.g., "7d", "30d", "365d", "14"
_SINCE_RE = re.compile(r"^\s*(\d+)\s*(d|days?)?\s*$", re.IGNORECASE)
_DEFAULT_SINCE_DAYS = 7
_MIN_SINCE_DAYS = 1
_MAX_SINCE_DAYS = 365

_DEFAULT_TOP = 5
_MIN_TOP = 1
_MAX_TOP = 20


def _parse_since(raw: str | None) -> int:
    """Parse `--since` argument like '7d', '30d', '14'.

    Falls back to default + warns on stderr for malformed input. P1-8/P1-10
    (rc.5): also warns when the value is OUT OF RANGE (negative, zero, or
    above max) — previously silently clamped, so users believed their value
    was honoured.
    """
    if raw is None or raw == "":
        return _DEFAULT_SINCE_DAYS
    match = _SINCE_RE.match(raw)
    if not match:
        sys.stderr.write(
            f"warning: ignoring malformed --since={raw!r}; "
            f"using {_DEFAULT_SINCE_DAYS}d\n"
        )
        return _DEFAULT_SINCE_DAYS
    days = int(match.group(1))
    clamped = max(_MIN_SINCE_DAYS, min(days, _MAX_SINCE_DAYS))
    if clamped != days:
        sys.stderr.write(
            f"warning: --since={raw} out of range "
            f"[{_MIN_SINCE_DAYS}d..{_MAX_SINCE_DAYS}d]; "
            f"clamped to {clamped}d\n"
        )
    return clamped


def _clamp_top(value: int | None) -> int:
    """Clamp --top to [_MIN_TOP, _MAX_TOP]. P1-9 (rc.5): warns when the value
    was clamped — previously silent, so users believed a 999 / 0 / -1 value
    was honoured."""
    if value is None:
        return _DEFAULT_TOP
    try:
        v = int(value)
    except (TypeError, ValueError):
        sys.stderr.write(
            f"warning: --top={value!r} is not a valid integer; "
            f"using {_DEFAULT_TOP}\n"
        )
        return _DEFAULT_TOP
    clamped = max(_MIN_TOP, min(v, _MAX_TOP))
    if clamped != v:
        sys.stderr.write(
            f"warning: --top={v} out of range [{_MIN_TOP}..{_MAX_TOP}]; "
            f"clamped to {clamped}\n"
        )
    return clamped


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------


def _term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _fmt_rule(headline: str, *, ascii_mode: bool, width: int) -> str:
    border_char = "=" if ascii_mode else "═"
    return border_char * min(width, 67) + "\n  " + headline + "\n" + border_char * min(width, 67)


def _fmt_stable_section(
    rows: list[dict[str, Any]],
    *,
    ascii_mode: bool,
    width: int,
) -> list[str]:
    if not rows:
        return ["📌 Top stable decisions: (none yet — keep coding!)" if not ascii_mode
                else "[stable] Top stable decisions: (none yet — keep coding!)"]
    out: list[str] = []
    head = "📌 Top stable decisions (kept across multiple subsequent commits):"
    if ascii_mode:
        head = "[stable] Top stable decisions:"
    out.append(head)
    out.append("")
    for i, d in enumerate(rows, 1):
        score = d.get("score", 0.0)
        kept = d.get("kept", 0)
        total = d.get("total", 0)
        reverted = d.get("reverted", 0)
        file_path = d.get("file_path") or "(unknown)"
        decision = _truncate(str(d.get("decision") or ""), max(20, width - 8))
        locked = d.get("locked", 0)
        lock_marker = ("🔒 " if not ascii_mode else "[locked] ") if locked else ""
        out.append(f"  {i}. {lock_marker}{file_path} — \"{decision}\"")
        out.append(
            f"     Score: {score:.2f}  •  {total} outcomes "
            f"({kept} kept, {reverted} reverted)"
        )
    return out


def _fmt_reverted_section(
    rows: list[dict[str, Any]],
    *,
    ascii_mode: bool,
    width: int,
) -> list[str]:
    if not rows:
        return []
    head = "⚠ Top reverted decisions (AI keeps trying, you keep undoing):"
    if ascii_mode:
        head = "[reverted] Top reverted decisions:"
    out = ["", head, ""]
    for i, d in enumerate(rows, 1):
        score = d.get("score", 0.0)
        reverted = d.get("reverted", 0)
        total = d.get("total", 0)
        kept = d.get("kept", 0)
        file_path = d.get("file_path") or "(unknown)"
        decision = _truncate(str(d.get("decision") or ""), max(20, width - 8))
        locked = d.get("locked", 0)
        out.append(f"  {i}. {file_path} — \"{decision}\"")
        out.append(
            f"     Score: {score:.2f}  •  {total} outcomes "
            f"({kept} kept, {reverted} reverted)"
        )
        if not locked:
            out.append(
                "     Suggestion: consider locking this decision "
                "(set do_not_revert) so Hero 1 blocks future reverts."
            )
    return out


def _fmt_rules_section(
    rows: list[dict[str, Any]],
    *,
    ascii_mode: bool,
    width: int,
) -> list[str]:
    if not rows:
        return []
    head = "📈 Emerging patterns (rule_learner confidence ≥ threshold):"
    if ascii_mode:
        head = "[rules] Emerging patterns:"
    out = ["", head, ""]
    for r in rows:
        conf = r.get("confidence", 0.0)
        text = _truncate(str(r.get("rule_text") or ""), max(20, width - 16))
        cat = r.get("category") or ""
        cat_marker = f"[{cat}] " if cat else ""
        out.append(f"  • {cat_marker}{text} (confidence {conf:.2f})")
    return out


# ---------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------


def cmd_insights(
    *,
    since: str | None = None,
    top: int | None = None,
    project: Path | None = None,
    min_outcomes: int = 1,
    min_score: float = 0.7,
    max_score: float = 0.4,
    min_confidence: float = 0.7,
    ascii_mode: bool = False,
    out=None,  # for testability
) -> int:
    """Render the insights digest.

    Returns process exit code: 0 on success, 1 on data-layer failure
    (the digest still prints a friendly message to stdout in that case).
    """
    out = out or sys.stdout

    since_days = _parse_since(since)
    top_n = _clamp_top(top)

    # Resolve project root + open graph DB
    try:
        from mcp_server.paths import (
            get_data_dir, get_project_root, set_project_dir,
            invalidate_data_dir_cache, is_invalid_project_root,
        )
        if project is not None:
            resolved_project = Path(project).resolve()
            # Bug 8 (Week-11 deep re-audit): defense-in-depth parity with
            # the wiring layer. The wiring (claude_code_hooks._build_event +
            # mcp_dispatch._build_pre_event) calls is_invalid_project_root
            # to refuse $HOME / system dirs as project_root (Round-4 HIGH #2
            # + v1.8.1 hotfix). The CLI bypassed this check, so
            # `codevira insights --project $HOME` would silently succeed
            # against a slug-sanitized path. Read-only path so no
            # catastrophic state — but the user gets a confusing
            # "no codevira data" instead of a clear "that's not a valid
            # project root". Now uniform.
            rejection = is_invalid_project_root(resolved_project)
            if rejection:
                out.write(
                    f"Error: --project {project!r} is not a valid project "
                    f"root: {rejection}\n"
                    f"Use a directory with a .git, pyproject.toml, "
                    f"package.json, or similar project marker.\n"
                )
                return 1
            set_project_dir(resolved_project)
            invalidate_data_dir_cache()
        project_root = get_project_root()
        graph_db = get_data_dir() / "graph" / "graph.db"
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not resolve project — {e}\n")
        return 1

    if not graph_db.exists():
        out.write(
            f"No codevira data found at {graph_db}.\n"
            "Run `codevira setup` and use codevira for a few sessions, then try again.\n"
        )
        return 0

    # Open + query
    try:
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine.promotion_score import (
            top_stable_decisions, top_reverted_decisions, top_rules,
        )
        g = SQLiteGraph(graph_db)
    except Exception as e:  # noqa: BLE001
        out.write(f"Error: could not open project DB — {e}\n")
        return 1

    try:
        stable = top_stable_decisions(
            g.conn,
            since_days=since_days,
            min_outcomes=min_outcomes,
            min_score=min_score,
            max_items=top_n,
        )
        reverted = top_reverted_decisions(
            g.conn,
            since_days=since_days,
            min_outcomes=min_outcomes,
            max_score=max_score,
            max_items=top_n,
        )
        rules = top_rules(
            g.conn, min_confidence=min_confidence, max_items=top_n,
        )
    finally:
        g.close()

    # Friendly empty case (P2-11 rc.5: aligned with budget/replay first-run UX)
    if not stable and not reverted and not rules:
        out.write(
            f"Codevira insights for {project_root.name} (last {since_days} days)\n\n"
            "No outcomes recorded yet for this project.\n"
            "\n"
            "How to populate this report:\n"
            "  1. Use codevira from an AI tool (Claude Code, Cursor, etc.) for\n"
            "     a few sessions — every record_decision / write_session_log\n"
            "     call adds an entry.\n"
            "  2. Make a few commits AFTER each session; codevira's outcome\n"
            "     tracker classifies decisions as kept/modified/reverted based\n"
            "     on subsequent git history.\n"
            "  3. Re-run `codevira insights` — entries with at least 1 outcome\n"
            "     appear here.\n"
        )
        return 0

    width = _term_width()
    sections: list[str] = []
    sections.append(_fmt_rule(
        f"Codevira Insights — {project_root.name} — last {since_days} days",
        ascii_mode=ascii_mode, width=width,
    ))
    sections.append("")
    sections.extend(_fmt_stable_section(stable, ascii_mode=ascii_mode, width=width))
    sections.extend(_fmt_reverted_section(reverted, ascii_mode=ascii_mode, width=width))
    sections.extend(_fmt_rules_section(rules, ascii_mode=ascii_mode, width=width))
    sections.append("")
    sections.append(
        f"(Run with --since=30d for a longer window. "
        f"Showing top {top_n} per category.)"
    )

    out.write("\n".join(sections) + "\n")
    return 0
