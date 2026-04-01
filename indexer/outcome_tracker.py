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
import difflib
import logging
import subprocess
from pathlib import Path

from mcp_server.paths import get_data_dir, get_project_root
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)

PROJECT_ROOT = get_project_root()


def _git_cmd(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT)] + list(args),
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
    """Analyze outcomes for a single session's decisions."""
    decisions = db.conn.execute('''
        SELECT id, file_path, decision, created_at FROM decisions
        WHERE session_id = ? AND file_path IS NOT NULL
    ''', (session_id,)).fetchall()

    if not decisions:
        return

    for dec in decisions:
        file_path = dec["file_path"]
        decision_id = dec["id"]
        created_at = dec["created_at"]

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


def _determine_file_outcome(file_path: str, session_date: str) -> dict | None:
    """
    Check git history to see what happened to a file after a session.
    Returns {'type': 'kept'|'modified'|'reverted', 'delta': ...}
    """
    abs_path = PROJECT_ROOT / file_path
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


def get_file_outcome_summary(file_path: str) -> dict:
    """Get a summary of all outcomes for a specific file."""
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    try:
        outcomes = db.get_outcomes_for_file(file_path)
        confidence = db.get_decision_confidence(file_path=file_path)
        return {
            "file_path": file_path,
            "outcomes": outcomes,
            "confidence": confidence,
        }
    finally:
        db.close()
