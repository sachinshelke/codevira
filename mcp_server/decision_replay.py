"""
decision_replay.py — Hero 8: Decision Replay timeline + renderers.

Pure data path:
  build_timeline(query, since_days, limit) -> list[dict]
    SQL aggregation joining decisions LEFT JOIN outcomes LEFT JOIN
    sessions LEFT JOIN nodes; returns each decision with outcome
    counts, score (Hero 10's score_decision), session summary, and
    do_not_revert flag.

Three renderers:
  render_terminal(timeline, *, ascii_mode=False) -> list[str]
  render_markdown(timeline) -> str
  render_html(timeline, *, embeddable=False) -> str

All renderers handle the empty-timeline case explicitly with a
friendly placeholder (Lesson #19 — never emit empty section headers).

HTML rendering escapes every untrusted field via html.escape() —
decision text or file paths could conceivably contain ``<script>``
content if a user pasted adversarial input into a prompt that became
a decision. Tested.
"""
from __future__ import annotations

import html
import logging
import shutil
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Defaults + bounds
# ---------------------------------------------------------------------

_DEFAULT_SINCE_DAYS = 30
_MIN_SINCE_DAYS = 1
_MAX_SINCE_DAYS = 365

_DEFAULT_LIMIT = 20
_MIN_LIMIT = 1
_MAX_LIMIT = 200

_TEXT_DISPLAY_CHARS = 200


def _clamp_since_days(value: int | None) -> int:
    if value is None:
        return _DEFAULT_SINCE_DAYS
    try:
        v = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_SINCE_DAYS
    return max(_MIN_SINCE_DAYS, min(v, _MAX_SINCE_DAYS))


def _clamp_limit(value: int | None) -> int:
    if value is None:
        return _DEFAULT_LIMIT
    try:
        v = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(v, _MAX_LIMIT))


def _truncate(text: str | None, n: int = _TEXT_DISPLAY_CHARS) -> str:
    if not text:
        return ""
    text = str(text).strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# ---------------------------------------------------------------------
# Score computation (kept local to avoid hard import dependency on Hero 10)
# ---------------------------------------------------------------------


def _score(kept: int, modified: int, reverted: int) -> float:
    """Identical to ``promotion_score.score_decision`` but inlined to
    avoid coupling Hero 8 to Hero 10's import order. Same formula:
    score = (kept + 0.5 * modified) / max(total, 1).
    """
    k = max(int(kept or 0), 0)
    m = max(int(modified or 0), 0)
    r = max(int(reverted or 0), 0)
    total = k + m + r
    if total == 0:
        return 0.0
    return (k + 0.5 * m) / total


# ---------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------


def build_timeline(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    since_days: int = _DEFAULT_SINCE_DAYS,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Build a per-decision timeline with outcomes attached.

    Pure: takes a `sqlite3.Connection` (caller owns it). Used by
    the MCP resource handler, the CLI, and tests.

    Returns a list of dicts; empty if no decisions match. Never raises
    — Hero 8 is a browse surface; data layer flakiness must yield
    empty + log, not crash.
    """
    since_days = _clamp_since_days(since_days)
    limit = _clamp_limit(limit)

    sql_parts = [
        """
        SELECT
            d.id            AS id,
            d.decision      AS decision,
            d.file_path     AS file_path,
            d.context       AS context,
            d.created_at    AS created_at,
            d.session_id    AS session_id,
            s.summary       AS session_summary,
            COALESCE(n.do_not_revert, 0) AS locked,
            COUNT(o.id)     AS total,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'kept'     THEN 1 ELSE 0 END), 0) AS kept,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'modified' THEN 1 ELSE 0 END), 0) AS modified,
            COALESCE(SUM(CASE WHEN o.outcome_type = 'reverted' THEN 1 ELSE 0 END), 0) AS reverted
        FROM decisions d
        LEFT JOIN outcomes o  ON o.decision_id = d.id
        LEFT JOIN sessions s  ON s.session_id  = d.session_id
        LEFT JOIN nodes    n  ON n.file_path   = d.file_path
        WHERE d.created_at >= datetime('now', ?)
        """,
    ]
    params: list[Any] = [f"-{since_days} days"]
    if query:
        sql_parts.append(
            " AND (d.decision LIKE ? OR d.file_path LIKE ? OR d.context LIKE ?)"
        )
        like = f"%{query}%"
        params.extend([like, like, like])
    sql_parts.append(
        """
        GROUP BY d.id
        ORDER BY d.created_at DESC
        LIMIT ?
        """
    )
    params.append(limit)

    sql = "".join(sql_parts)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning("decision_replay.build_timeline failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["score"] = _score(
            d.get("kept", 0), d.get("modified", 0), d.get("reverted", 0),
        )
        d["locked"] = bool(d.get("locked"))
        out.append(d)
    return out


# ---------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------

_EMPTY_PLACEHOLDER = (
    "No decisions recorded yet. Use codevira for a few sessions and "
    "log decisions via record_decision; outcomes accrue automatically "
    "from git history."
)


# ----- Terminal -----


def render_terminal(
    timeline: list[dict[str, Any]],
    *,
    ascii_mode: bool = False,
    title: str = "Codevira Replay",
) -> list[str]:
    """Pretty-print to terminal. Returns a list of lines (no newlines)."""
    width = _term_width()
    border_char = "=" if ascii_mode else "═"
    out: list[str] = []
    out.append(border_char * min(width, 67))
    out.append(f"  {title}")
    out.append(border_char * min(width, 67))
    out.append("")

    if not timeline:
        # Lesson #19: explicit empty-case handling, NOT a bare header.
        out.append(_EMPTY_PLACEHOLDER)
        return out

    for d in timeline:
        # Per-decision header line
        date = str(d.get("created_at") or "")[:10]
        file_path = d.get("file_path") or "(no file)"
        score = d.get("score", 0.0)
        locked = d.get("locked", False)
        marker = "🔒 " if locked and not ascii_mode else ("[locked] " if locked else "")
        prefix = ("⚠ " if score == 0.0 and d.get("total", 0) > 0 else "📌 ")
        if ascii_mode:
            prefix = "[reverted] " if score == 0.0 and d.get("total", 0) > 0 else "[stable]   "
        out.append(f"{prefix}{date} — {marker}{file_path}")

        # Decision text
        decision = _truncate(d.get("decision"), max(20, width - 4))
        out.append(f"   \"{decision}\"")

        # Outcomes line
        kept = d.get("kept", 0)
        modified = d.get("modified", 0)
        reverted = d.get("reverted", 0)
        total = d.get("total", 0)
        if total > 0:
            out.append(
                f"   score {score:.2f} · {total} outcome(s) "
                f"({kept} kept, {modified} modified, {reverted} reverted)"
            )
        else:
            out.append("   no outcomes recorded yet")

        # Session line
        sid = d.get("session_id") or "(unknown)"
        summary = _truncate(d.get("session_summary"), max(20, width - 24))
        if summary:
            out.append(f"   session {sid}: \"{summary}\"")
        else:
            out.append(f"   session {sid}")

        out.append("")  # blank between decisions

    return out


def _term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


# ----- Markdown -----


def render_markdown(
    timeline: list[dict[str, Any]],
    *,
    title: str = "Codevira Replay",
) -> str:
    """Render the timeline as Markdown. Suitable for clipboard, docs,
    or piping to a markdown viewer."""
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")

    if not timeline:
        lines.append(_EMPTY_PLACEHOLDER)
        return "\n".join(lines)

    for d in timeline:
        date = str(d.get("created_at") or "")[:10]
        file_path = d.get("file_path") or "(no file)"
        decision = _truncate(d.get("decision"))
        locked = d.get("locked", False)
        score = d.get("score", 0.0)
        kept = d.get("kept", 0)
        modified = d.get("modified", 0)
        reverted = d.get("reverted", 0)
        total = d.get("total", 0)
        sid = d.get("session_id") or "(unknown)"
        summary = _truncate(d.get("session_summary"))

        marker = "🔒 " if locked else ""
        lines.append(f"## {date} — {marker}`{file_path}`")
        lines.append("")
        lines.append(f"> {decision}")
        lines.append("")
        if total > 0:
            lines.append(
                f"- **Score**: {score:.2f}"
            )
            lines.append(
                f"- **Outcomes**: {total} total — {kept} kept, "
                f"{modified} modified, {reverted} reverted"
            )
        else:
            lines.append("- _No outcomes recorded yet_")
        lines.append(f"- **Session**: `{sid}`")
        if summary:
            lines.append(f"  - _{summary}_")
        lines.append("")
    return "\n".join(lines)


# ----- HTML -----


def render_html(
    timeline: list[dict[str, Any]],
    *,
    embeddable: bool = False,
    title: str = "Codevira Replay",
) -> str:
    """Render the timeline as standalone HTML. Every untrusted field
    runs through ``html.escape()``.

    ``embeddable=True``: omits the ``<html><head><body>`` shell so the
    output can be embedded in another page (or in an MCP-Apps iframe).
    """
    body_parts: list[str] = []
    body_parts.append(f'<h1>{html.escape(title)}</h1>')

    if not timeline:
        # Lesson #19 + Bug-6 lesson: no empty section headers
        body_parts.append(f'<p class="empty">{html.escape(_EMPTY_PLACEHOLDER)}</p>')
    else:
        body_parts.append('<div class="timeline">')
        for d in timeline:
            date = html.escape(str(d.get("created_at") or "")[:10])
            file_path = html.escape(d.get("file_path") or "(no file)")
            decision = html.escape(_truncate(d.get("decision")))
            locked = bool(d.get("locked"))
            score = float(d.get("score", 0.0))
            kept = int(d.get("kept", 0))
            modified = int(d.get("modified", 0))
            reverted = int(d.get("reverted", 0))
            total = int(d.get("total", 0))
            sid = html.escape(d.get("session_id") or "(unknown)")
            summary = html.escape(_truncate(d.get("session_summary")))

            css_class = "decision"
            if locked:
                css_class += " locked"
            if total > 0 and score == 0.0:
                css_class += " reverted"

            body_parts.append(f'<article class="{css_class}">')
            body_parts.append(f'  <header>')
            body_parts.append(f'    <span class="date">{date}</span>')
            body_parts.append(f'    <span class="file">{file_path}</span>')
            if locked:
                body_parts.append(
                    f'    <span class="lock-marker" title="locked">🔒</span>'
                )
            body_parts.append(f'  </header>')
            body_parts.append(f'  <blockquote>{decision}</blockquote>')
            if total > 0:
                body_parts.append(
                    f'  <footer>'
                    f'<span class="score">score {score:.2f}</span> · '
                    f'<span class="outcomes">{total} outcome(s): '
                    f'{kept} kept, {modified} modified, {reverted} reverted</span> · '
                    f'<span class="session">session <code>{sid}</code></span>'
                    f'</footer>'
                )
            else:
                body_parts.append(
                    f'  <footer>'
                    f'<span class="no-outcomes">no outcomes yet</span> · '
                    f'<span class="session">session <code>{sid}</code></span>'
                    f'</footer>'
                )
            if summary:
                body_parts.append(f'  <p class="session-summary">{summary}</p>')
            body_parts.append(f'</article>')
        body_parts.append('</div>')

    body = "\n".join(body_parts)

    if embeddable:
        return body

    # Standalone HTML with minimal styling
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 800px; margin: 2em auto; padding: 0 1em; color: #333; }}
  h1 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }}
  .empty {{ color: #888; font-style: italic; }}
  article.decision {{ border: 1px solid #e0e0e0; padding: 1em; margin: 1em 0;
                       border-radius: 4px; background: #fafafa; }}
  article.decision.locked {{ border-color: #b8860b; background: #fffaf0; }}
  article.decision.reverted {{ border-color: #c00; background: #fff5f5; }}
  article header .date {{ color: #666; font-family: monospace; }}
  article header .file {{ font-weight: bold; margin-left: 0.5em;
                          font-family: monospace; }}
  article header .lock-marker {{ float: right; font-size: 1.2em; }}
  article blockquote {{ margin: 0.5em 0; padding-left: 1em;
                         border-left: 3px solid #4a90e2; color: #222; }}
  article footer {{ font-size: 0.9em; color: #666; }}
  article footer .score {{ font-weight: bold; color: #2a8000; }}
  article.reverted footer .score {{ color: #c00; }}
  .session-summary {{ font-style: italic; color: #555; margin: 0.5em 0 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
