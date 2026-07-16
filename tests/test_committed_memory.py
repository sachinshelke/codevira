"""
test_committed_memory.py — v3.7.1 fix E: cross-project memory bleed via git.

Root cause (reproduced from the user's real projects): older codevira versions
committed ``.codevira/decisions.jsonl`` into git BEFORE the ``.codevira/``
gitignore rule existed. ``.gitignore`` never untracks an already-tracked file,
so those decisions stay in git history and travel to any clone/copy of the
repo — an unrelated new project then inherits the source project's memory.

These tests pin the detect + untrack helpers that init/doctor use to close it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.paths import git_tracked_memory_files, untrack_git_memory_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo_with_committed_memory(tmp_path: Path) -> Path:
    """A repo that committed .codevira/decisions.jsonl (the leak scenario)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    cv = repo / ".codevira"
    cv.mkdir()
    (cv / "decisions.jsonl").write_text('{"id":"D1","decision":"secret"}\n')
    (cv / "sessions.jsonl").write_text('{"session_id":"s1"}\n')
    (cv / "config.yaml").write_text("schema_version: 1\n")
    # Commit the memory (as an old codevira version would have).
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init with committed memory")
    return repo


def test_detects_committed_memory(repo_with_committed_memory: Path):
    tracked = git_tracked_memory_files(repo_with_committed_memory)
    assert ".codevira/decisions.jsonl" in tracked
    assert ".codevira/sessions.jsonl" in tracked


def test_untracks_but_keeps_local_file(repo_with_committed_memory: Path):
    repo = repo_with_committed_memory
    untracked = untrack_git_memory_files(repo)
    assert ".codevira/decisions.jsonl" in untracked
    # No longer tracked by git...
    assert git_tracked_memory_files(repo) == []
    # ...but the local memory file is preserved (not deleted).
    assert (repo / ".codevira" / "decisions.jsonl").is_file()
    assert "secret" in (repo / ".codevira" / "decisions.jsonl").read_text()


def test_no_git_repo_is_safe(tmp_path: Path):
    # Not a git repo — helpers must no-op, never raise.
    assert git_tracked_memory_files(tmp_path) == []
    assert untrack_git_memory_files(tmp_path) == []


def test_clean_repo_reports_nothing(tmp_path: Path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".codevira/\n")
    cv = repo / ".codevira"
    cv.mkdir()
    (cv / "decisions.jsonl").write_text('{"id":"D1"}\n')  # gitignored, never added
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "clean")
    assert git_tracked_memory_files(repo) == []
