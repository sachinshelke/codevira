"""Phase 17 — the two outcome surfaces must AGREE.

outcome_tracker (SQLite → confidence) and storage/outcomes_writer (JSONL →
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
from indexer import outcome_tracker
from mcp_server.storage.outcomes_writer import _classify_decision


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


def _both_labels(
    repo: Path, file_path: str, ts: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[str | None, str | None]:
    """(writer_label, tracker_label) for the same (file, ts)."""
    writer = _classify_decision(repo, {"file_path": file_path, "ts": ts})
    # The tracker resolves its repo via get_project_root().
    monkeypatch.setattr(outcome_tracker, "get_project_root", lambda: repo)
    tracker_res = outcome_tracker._determine_file_outcome(file_path, ts)
    tracker = tracker_res["type"] if tracker_res else None
    return writer, tracker


class TestSurfacesAgree:
    def test_kept(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _commit(git_repo, "f.py", "v1\n", "add f", "2020-01-01T00:00:00")
        w, t = _both_labels(git_repo, "f.py", "2020-02-01T00:00:00", monkeypatch)
        assert w == t == "kept"

    def test_modified(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _commit(git_repo, "f.py", "v1\n", "add f", "2020-01-01T00:00:00")
        _commit(git_repo, "f.py", "v2\n", "tweak f", "2020-06-01T00:00:00")
        # ts between the two commits → anchor is commit 1, commit 2 is "since".
        w, t = _both_labels(git_repo, "f.py", "2020-03-01T00:00:00", monkeypatch)
        assert w == t == "modified"

    def test_reverted_by_commit_message(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _commit(git_repo, "f.py", "v1\n", "add f", "2020-01-01T00:00:00")
        _commit(git_repo, "f.py", "v0\n", "Revert f change", "2020-06-01T00:00:00")
        w, t = _both_labels(git_repo, "f.py", "2020-03-01T00:00:00", monkeypatch)
        # Merged heuristic: a revert-message commit → reverted on BOTH surfaces.
        assert w == t == "reverted"

    def test_reverted_by_deletion(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _commit(git_repo, "f.py", "v1\n", "add f", "2020-01-01T00:00:00")
        (git_repo / "f.py").unlink()
        w, t = _both_labels(git_repo, "f.py", "2020-02-01T00:00:00", monkeypatch)
        assert w == t == "reverted"


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
