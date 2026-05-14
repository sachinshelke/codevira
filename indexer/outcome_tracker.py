"""
Outcome Tracker — Git-based feedback loop for Codevira's adaptive memory.

After an agent session ends and changes are committed, this module analyzes
what happened to the agent's changes:
  - 'kept':     Code survived untouched in subsequent commits
  - 'modified': Developer edited the agent's output (correction signal)
  - 'reverted': Code was reverted within N commits (negative signal)

This feedback feeds into confidence scoring, preference learning, and
automatic rule generation.
"""
from __future__ import annotations

import difflib
import logging
import subprocess
from pathlib import Path

from mcp_server.paths import get_data_dir, get_project_root
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)


def _project_root():
    return get_project_root()


def _git_cmd(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(_project_root())] + list(args),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def analyze_session_outcomes(session_id: str | None = None):
    """
    Analyze git history to determine outcomes for recent sessions.
    If session_id is provided, only analyzes that session.
    Otherwise, analyzes all sessions that don't yet have outcomes.
    """
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")

    try:
        if session_id:
            sessions = [{"session_id": session_id}]
        else:
            # Find sessions that have decisions but no outcomes yet
            cur = db.conn.execute('''
                SELECT DISTINCT d.session_id FROM decisions d
                LEFT JOIN outcomes o ON d.session_id = o.session_id
                WHERE o.id IS NULL
                ORDER BY d.created_at DESC LIMIT 20
            ''')
            sessions = [dict(r) for r in cur.fetchall()]

        for sess in sessions:
            sid = sess["session_id"]
            _analyze_single_session(db, sid)
    finally:
        db.close()


def _analyze_single_session(db: SQLiteGraph, session_id: str):
    """Analyze outcomes for a single session's decisions.

    P0-D (rc.5 audit, 2026-05-13): pre-fix this filtered to
    ``WHERE file_path IS NOT NULL`` only, so decisions logged via
    ``record_decision`` without an explicit ``file_path=`` arg never had any
    outcome classified. Result: ``get_decision_confidence`` showed 0/0/0/0
    forever, no matter how many decisions were recorded. Now we ALSO accept
    file-less decisions and extract file mentions from their ``decision`` +
    ``context`` text. The first plausible mention becomes the canonical file
    for outcome classification.
    """
    decisions = db.conn.execute('''
        SELECT id, file_path, decision, context, created_at FROM decisions
        WHERE session_id = ?
    ''', (session_id,)).fetchall()

    if not decisions:
        return

    for dec in decisions:
        decision_id = dec["id"]
        created_at = dec["created_at"]
        file_path = dec["file_path"]

        if file_path is None:
            # P0-D: try to extract a file mention from the decision text.
            extracted = _extract_file_mentions(
                (dec["decision"] or "") + "\n" + (dec["context"] or "")
            )
            if not extracted:
                # Genuinely no file reference — skip classification.
                # `get_decision_confidence` surfaces this case via the
                # `decisions_total - decisions_eligible_for_outcomes` gap.
                continue
            file_path = extracted[0]

        outcome = _determine_file_outcome(file_path, created_at)
        if outcome:
            db.record_outcome(
                session_id=session_id,
                file_path=file_path,
                outcome_type=outcome["type"],
                decision_id=decision_id,
                delta_summary=outcome.get("delta"),
            )

            # If modified, try to learn preferences from the diff
            if outcome["type"] == "modified" and outcome.get("delta"):
                _learn_from_modification(db, file_path, outcome["delta"])


def _extract_file_mentions(text: str) -> list[str]:
    """P0-D (rc.5): extract file paths mentioned in decision/context text.

    Returns the list of plausible file paths in the order they appear.
    Filters out obvious false positives (urls, capitalised proper nouns).

    Recognises:
      * ``src/foo.py``  (path with directory)
      * ``mcp_server/cli.py:315``  (file:line)
      * `` `file.ext` ``  (backticked filenames)
      * Bare ``file.ext`` ONLY if it has an extension we know about

    Conservative on purpose: outcome tracker is git-driven, so we only want
    to nominate paths that could plausibly exist in the project tree.
    """
    import re

    if not text:
        return []

    # Known source/config/docs extensions we'll accept (matches the rc.5
    # auto_detect_project default set, kept inline to avoid an import cycle).
    # IMPORTANT: sort by length DESCENDING in the alternation so longer
    # extensions match before their prefixes — otherwise `.md` regex-matches
    # only `.m` (Objective-C) and we lose the trailing `d`.
    _ext_list = [
        "py", "pyi", "ipynb", "js", "jsx", "ts", "tsx", "mjs", "cjs",
        "go", "rs", "rb", "php", "java", "kt", "scala", "swift",
        "c", "cc", "cpp", "h", "hpp", "cs", "m", "mm",
        "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "r", "jl",
        "dart", "ex", "exs", "erl", "clj", "cljs", "elm", "hs", "ml", "mli", "v",
        "yaml", "yml", "toml", "json", "jsonl", "xml", "ini", "env",
        "proto", "graphql", "prisma", "sql", "thrift", "cap",
        "md", "mdx", "rst", "adoc", "txt",
        "html", "htm", "css", "scss", "sass", "less",
        "vue", "svelte", "astro", "dockerfile", "gradle", "bazel",
    ]
    _exts = "|".join(sorted(_ext_list, key=len, reverse=True))

    found: list[str] = []
    seen: set[str] = set()

    # Pattern 1: path/with/slash/file.ext optionally followed by :line
    # The trailing word-boundary prevents partial matches like .m from .md.
    pattern_path = re.compile(rf"[\w.\-/]+\.(?:{_exts})(?::\d+)?\b")
    for match in pattern_path.finditer(text):
        token = match.group(0)
        # Strip :line suffix for the path key.
        path = token.split(":", 1)[0]
        # Skip URLs (e.g. https://example.com/foo.json — match contains ://).
        if "//" in path:
            continue
        # Skip absolute paths outside the project (we can't validate without
        # project_root here; absolute paths like /usr/lib are noise).
        if path.startswith("/"):
            continue
        if path not in seen:
            seen.add(path)
            found.append(path)

    return found


def _determine_file_outcome(file_path: str, session_date: str) -> dict | None:
    """
    Check git history to see what happened to a file after a session.
    Returns {'type': 'kept'|'modified'|'reverted', 'delta': ...}
    """
    abs_path = _project_root() / file_path
    if not abs_path.exists():
        return {"type": "reverted", "delta": "File no longer exists"}

    # Normalize date to ISO 8601 for git --since compatibility
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(session_date.replace(" ", "T"))
        since_date = dt.isoformat()
    except (ValueError, AttributeError):
        since_date = session_date

    # Get commits touching this file after the session date
    log_output = _git_cmd(
        "log", "--oneline", "--follow", f"--since={since_date}",
        "--", file_path
    )

    if not log_output:
        return {"type": "kept", "delta": None}

    commits = log_output.split("\n")
    if not commits or commits == [""]:
        return {"type": "kept", "delta": None}

    # Check if any commit message suggests a revert
    for commit_line in commits:
        lower = commit_line.lower()
        if any(word in lower for word in ["revert", "undo", "rollback", "roll back"]):
            return {"type": "reverted", "delta": commit_line}

    # If there are subsequent commits but no revert, it was modified
    if len(commits) >= 1:
        # Get a summary of changes
        diff_stat = _git_cmd("diff", "--stat", f"HEAD~{min(len(commits), 5)}", "--", file_path)
        if not diff_stat:
            logger.debug("Could not get diff stats for %s, using commit count", file_path)
        return {"type": "modified", "delta": diff_stat or f"{len(commits)} subsequent commits"}

    return {"type": "kept", "delta": None}


def _learn_from_modification(db: SQLiteGraph, file_path: str, delta: str):
    """
    When a developer modifies agent output, try to extract preference signals.
    This is a lightweight heuristic — not perfect, but builds up over time.
    """
    # Detect naming convention changes
    if "camelCase" in delta or "snake_case" in delta:
        db.record_preference("naming", "Prefers consistent naming convention", example=file_path)

    # Detect structural patterns from file extension
    ext = Path(file_path).suffix
    if ext in ('.py', '.ts', '.tsx', '.go', '.rs'):
        db.record_preference("structure", f"Developer modifies AI output in {ext} files", example=file_path)


