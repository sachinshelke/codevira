"""Single source of truth for git-derived decision outcomes — Phase 17.

``outcome_tracker`` (SQLite → confidence) and
``mcp_server.storage.outcomes_writer`` (JSONL → digest / replay / skills) used
to run INDEPENDENT git analyses and could label the SAME decision differently
— e.g. the tracker called a revert-message commit ``reverted`` while the
writer (which only checked file deletion) called it ``modified``. Both now
delegate to :func:`classify_outcome` here, so the two surfaces agree by
construction. Lives in ``indexer`` so the ``mcp_server.storage`` writer can
import it in the normal direction (mcp_server → indexer).

Heuristic (merges the best of both prior implementations):

* file gone at HEAD                                  → ``reverted``
* no anchor timestamp / file not yet committed       → ``None`` (can't classify)
* file unchanged since the anchor commit             → ``kept``
* changed, and a later commit subject reads like a
  revert (revert / undo / rollback)                  → ``reverted``
* changed otherwise                                  → ``modified``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REVERT_WORDS = ("revert", "undo", "rollback", "roll back")


def _git(project_root: Path, *args: str) -> str | None:
    """Run a git command in ``project_root``; return stripped stdout or None."""
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(project_root), *args],
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _commit_at_or_before(project_root: Path, file_path: str, iso_ts: str) -> str | None:
    """SHA of the latest commit touching ``file_path`` on or before ``iso_ts``;
    falls back to the file's first commit. The anchor = "when the decision was
    recorded"."""
    out = _git(
        project_root, "log", "--format=%H", f"--before={iso_ts}", "--", file_path
    )
    if out:
        return out.splitlines()[0]
    out = _git(project_root, "log", "--reverse", "--format=%H", "--", file_path)
    return out.splitlines()[0] if out else None


def _commit_subjects_since(
    project_root: Path, file_path: str, anchor: str
) -> list[str] | None:
    """Subjects of commits touching ``file_path`` in ``anchor..HEAD``.
    None on git error; [] when nothing changed."""
    out = _git(project_root, "log", "--format=%s", f"{anchor}..HEAD", "--", file_path)
    if out is None:
        return None
    return [line for line in out.splitlines() if line.strip()]


def classify_outcome(
    project_root: Path, file_path: str | None, anchor_ts: str | None
) -> str | None:
    """Return ``"kept" | "modified" | "reverted"`` for a decision's file, or
    ``None`` when it can't be classified confidently (caller leaves the prior
    outcome untouched). Deterministic given the repo state."""
    if not file_path:
        return None
    if not (Path(project_root) / file_path).is_file():
        return "reverted"  # deleted entirely
    if not anchor_ts:
        return None
    anchor = _commit_at_or_before(project_root, str(file_path), str(anchor_ts))
    if anchor is None:
        return None  # untracked / not yet committed — can't classify yet
    subjects = _commit_subjects_since(project_root, str(file_path), anchor)
    if subjects is None:
        return None  # git error
    if not subjects:
        return "kept"
    blob = "\n".join(s.lower() for s in subjects)
    if any(word in blob for word in _REVERT_WORDS):
        return "reverted"
    return "modified"
