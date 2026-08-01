"""Phase 17 — the two outcome surfaces must AGREE.

storage/outcomes_writer (JSONL →
digest/replay/skills) used to run independent git analyses and could label the
same decision differently. Both now delegate to
``indexer.outcome_classifier.classify_outcome``; these tests pin that they
return the SAME kept/modified/reverted label across the four scenarios, and
that the shared classifier behaves correctly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from indexer import outcome_classifier


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "-C", str(repo), *args], env=env, check=True, capture_output=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: Path, name: str, body: str, msg: str, date: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name, date=date)
    _git(repo, "commit", "-q", "-m", msg, date=date)


class TestClassifier:
    def test_no_file_path_is_none(self, git_repo: Path) -> None:
        assert outcome_classifier.classify_outcome(git_repo, None, "2020-01-01") is None

    def test_untracked_file_is_none(self, git_repo: Path) -> None:
        (git_repo / "new.py").write_text("x\n", encoding="utf-8")  # never committed
        assert (
            outcome_classifier.classify_outcome(git_repo, "new.py", "2020-01-01")
            is None
        )

    def test_no_anchor_ts_is_none(self, git_repo: Path) -> None:
        _commit(git_repo, "f.py", "v1\n", "add f", "2020-01-01T00:00:00")
        assert outcome_classifier.classify_outcome(git_repo, "f.py", None) is None
