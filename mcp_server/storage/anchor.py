"""
anchor.py — resolve an edit location to the symbol that contains it.

4.0 Step 7. Decisions carry ``file_path`` on ~45% of records and
``symbol`` on 1%. The symbol field has been available since v3.6.0 and is
what makes region-level locking possible — ``decision_lock`` blocks only
edits INSIDE the named function rather than anywhere in the file — but
almost nothing sets it, because doing so requires the agent to type it.

The fix is derivation, not discipline. Every field a machine writes gets
populated (``outcome`` 21%, ``origin`` 68%); every field an agent must
type stays empty (``symbol`` 1%, ``alternatives_considered`` 0%). So the
symbol is resolved from the edit the session actually made.

Resolution is best-effort by design: an unresolved anchor leaves
``symbol`` as ``None`` and the decision stays file-scoped, exactly as it
does today. A WRONG symbol would be worse than none — it would scope a
lock to a region the decision has nothing to do with — so every
ambiguous case returns ``None`` rather than guessing.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _graph_db(project_root: Path | None = None) -> Path | None:
    """Locate ``project_root``'s code graph, or None.

    ``project_root`` is honoured, not decorative. It used to be accepted and
    dropped — ``get_data_dir()`` takes no argument, so every caller that
    carefully threaded a root through resolved the graph AMBIENTLY instead.
    ``decisions_store.record()`` derives ``symbol`` on this path, so a
    process whose ambient root was a different project would name a function
    from ANOTHER repo and then scope a region lock to it. This module's own
    contract is that a wrong symbol is worse than none, and that was the one
    way to produce a confidently wrong one. Same cross-project bleed shape as
    D00011U / D00012O.
    """
    try:
        from mcp_server.paths import _resolve_data_dir, get_data_dir

        data_dir = (
            _resolve_data_dir(Path(project_root).resolve())
            if project_root is not None
            else get_data_dir()
        )
        p = data_dir / "graph" / "graph.db"
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def symbol_at(
    file_path: str,
    line: int,
    *,
    project_root: Path | None = None,
) -> str | None:
    """Return the symbol containing ``line`` in ``file_path``, or None.

    Picks the INNERMOST enclosing symbol — a method inside a class must
    resolve to the method, not the class, or a region lock would cover the
    whole class and block unrelated edits.

    Returns None when: the graph is missing, the file is unknown, the line
    is outside every symbol (module-level code), or the resolution is
    ambiguous. Never raises — this runs on the write path.
    """
    if not file_path or line is None or line < 1:
        return None
    db = _graph_db(project_root)
    if db is None:
        return None

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT name, start_line, end_line
                FROM symbols
                WHERE file_node_id = ?
                  AND start_line <= ?
                  AND end_line   >= ?
                """,
                (f"file:{file_path}", line, line),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("anchor.symbol_at: graph read failed: %s", exc)
        return None

    if not rows:
        return None

    # Innermost = smallest span. A tie means two symbols claim the exact
    # same range, which we cannot disambiguate — return None rather than
    # pick one arbitrarily.
    rows = sorted(rows, key=lambda r: r["end_line"] - r["start_line"])
    if len(rows) > 1:
        a, b = rows[0], rows[1]
        if (a["end_line"] - a["start_line"]) == (b["end_line"] - b["start_line"]):
            return None
    name = rows[0]["name"]
    return str(name) if name else None


def line_of_edit(old_string: str, file_text: str) -> int | None:
    """1-indexed line where ``old_string`` begins in ``file_text``, or None.

    Returns None when the match is absent OR appears more than once — an
    ambiguous location must not produce a confident symbol.
    """
    if not old_string or not file_text:
        return None
    first = old_string.split("\n", 1)[0].strip()
    if not first:
        return None

    hits = [i for i, ln in enumerate(file_text.split("\n"), 1) if first in ln]
    if len(hits) != 1:
        return None
    return hits[0]


def resolve_from_edit(
    file_path: str,
    old_string: str,
    *,
    project_root: Path | None = None,
) -> str | None:
    """Full path: an Edit's ``old_string`` -> the enclosing symbol name.

    Reads the file from disk to locate the edit, then maps that line onto
    the graph. Any failure yields None.
    """
    if not file_path or not old_string:
        return None
    try:
        root = project_root or Path.cwd()
        target = Path(file_path)
        if not target.is_absolute():
            target = root / target
        if not target.is_file():
            return None
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    line = line_of_edit(old_string, text)
    if line is None:
        return None

    # The graph keys files project-relative.
    try:
        rel = str(Path(file_path))
        if Path(file_path).is_absolute() and project_root:
            rel = str(Path(file_path).relative_to(project_root))
    except ValueError:
        rel = str(file_path)

    return symbol_at(rel, line, project_root=project_root)


def symbol_for_session_edit(
    file_path: str,
    session_id: str,
    *,
    project_root: Path | None = None,
) -> str | None:
    """Symbol touched by THIS session's most recent edit to ``file_path``.

    This is the join Step 3.2 made possible. Before that fix every write
    minted its own session id, so 0 of 116 decisions shared a session with
    an edit row and this lookup could never have matched anything.

    Returns None when the session made no line-tagged edit to that file —
    including for every activity row written before 4.0, which are
    per-file only. Callers leave ``symbol`` unset in that case.
    """
    if not file_path or not session_id:
        return None
    try:
        from mcp_server.storage import activity_store

        rows = activity_store.list_recent(
            limit=200,
            kind=activity_store.KIND_EDIT,
            project_root=project_root,
        )
    except Exception:  # noqa: BLE001
        return None

    target = str(file_path)
    best: int | None = None
    for r in rows:  # newest-first
        if r.get("session_id") != session_id:
            continue
        node = str(r.get("node_id") or "")
        if node != target and not node.endswith("/" + target.lstrip("/")):
            continue
        line = r.get("line")
        if isinstance(line, int) and line > 0:
            best = line
            break

    if best is None:
        return None
    return symbol_at(target, best, project_root=project_root)
