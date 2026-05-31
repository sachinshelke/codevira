"""
test_session_log_enforcer.py — v3.2.0 enforcement of write_session_log.

These tests are deliberately built against REAL filesystems + git repos
(no mocks) so a future schema drift in sessions.jsonl or active_sessions.jsonl
fails fast.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.session_log_enforcer import (
    SessionLogEnforcer,
    _active_path,
    _count_commits_since,
    _lookup_active,
    _session_log_written,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEVIRA_SESSION_LOG_ENFORCER_MODE", raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A tmp project with .codevira/ + .codevira-cache/ pre-created."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".codevira").mkdir()
    (root / ".codevira-cache").mkdir()
    return root


@pytest.fixture
def git_project(project_root: Path) -> Path:
    """Project with an initialized git repo + a baseline commit."""
    subprocess.run(["git", "init", "-q"], cwd=str(project_root), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@codevira.local"],
        cwd=str(project_root),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(project_root),
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(project_root),
        check=True,
    )
    (project_root / "README.md").write_text("baseline\n")
    subprocess.run(["git", "add", "."], cwd=str(project_root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=str(project_root),
        check=True,
    )
    return project_root


def _make_event(
    event_type: EventType,
    project_root: Path,
    *,
    session_id: str | None = "session-uuid-abc",
    timestamp: float | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        project_root=project_root,
        session_id=session_id,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


def _git_commit(project_root: Path, msg: str, *, at_epoch: float | None = None) -> None:
    """Add an empty commit so we can advance HEAD without churning files.

    ``at_epoch``: override commit timestamp via GIT_COMMITTER_DATE +
    GIT_AUTHOR_DATE. Necessary because git's --since is 1s-resolution;
    pinning explicit timestamps avoids same-second collisions on fast
    test fixtures.
    """
    import os as _os

    env = _os.environ.copy()
    if at_epoch is not None:
        date_str = f"@{int(at_epoch)} +0000"
        env["GIT_COMMITTER_DATE"] = date_str
        env["GIT_AUTHOR_DATE"] = date_str
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=str(project_root),
        check=True,
        env=env,
    )


def _head_commit_epoch(project_root: Path) -> float:
    """Epoch seconds of HEAD's commit timestamp."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _ts_after_head(project_root: Path) -> float:
    """Anchor ``started_at`` 1s after HEAD — git's --since is second-resolution."""
    return _head_commit_epoch(project_root) + 1.0


def _write_session_log_entry(project_root: Path, *, ts_epoch: float) -> None:
    """Append a sessions.jsonl entry stamped at ``ts_epoch``."""
    from datetime import datetime, timezone

    ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat()
    entry = {
        "ts": ts_iso,
        "session_id": "user-slug",
        "task": "test task",
        "phase": "test phase",
        "summary": None,
        "decision_ids": [],
        "outcome": None,
        "id": "S999999",
    }
    sessions_path = project_root / ".codevira" / "sessions.jsonl"
    with sessions_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# =====================================================================
# SESSION_START stage
# =====================================================================


class TestSessionStart:
    def test_no_session_id_allows_no_record(self, project_root: Path) -> None:
        policy = SessionLogEnforcer()
        event = _make_event(
            EventType.SESSION_START,
            project_root,
            session_id=None,
        )
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert not _active_path(project_root).exists()

    def test_records_marker(self, project_root: Path) -> None:
        policy = SessionLogEnforcer()
        event = _make_event(
            EventType.SESSION_START,
            project_root,
            session_id="abc-123",
            timestamp=1_000_000.0,
        )
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert verdict.metadata.get("recorded") is True

        path = _active_path(project_root)
        assert path.exists()
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        assert len(rows) == 1
        assert rows[0]["session_id"] == "abc-123"
        assert rows[0]["started_at"] == 1_000_000.0
        assert rows[0]["project_root"] == str(project_root)

    def test_creates_cache_dir_when_missing(self, tmp_path: Path) -> None:
        # No .codevira-cache/ pre-created — policy should create it.
        root = tmp_path / "fresh"
        root.mkdir()
        policy = SessionLogEnforcer()
        event = _make_event(EventType.SESSION_START, root, session_id="s1")
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert (root / ".codevira-cache").is_dir()
        assert _active_path(root).exists()

    def test_multiple_session_starts_returns_latest(self, project_root: Path) -> None:
        policy = SessionLogEnforcer()
        for ts in (100.0, 200.0, 300.0):
            policy.evaluate(
                _make_event(
                    EventType.SESSION_START,
                    project_root,
                    session_id="dup",
                    timestamp=ts,
                ),
                None,
            )
        latest = _lookup_active(project_root, "dup")
        assert latest is not None
        assert latest["started_at"] == 300.0


# =====================================================================
# STOP stage — happy paths
# =====================================================================


class TestStopNoOp:
    def test_no_session_id_allows(self, project_root: Path) -> None:
        policy = SessionLogEnforcer()
        event = _make_event(EventType.STOP, project_root, session_id=None)
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    def test_no_active_record_allows(self, project_root: Path) -> None:
        """Cached/restored session w/o SESSION_START: don't warn."""
        policy = SessionLogEnforcer()
        event = _make_event(EventType.STOP, project_root, session_id="ghost")
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()
        assert verdict.metadata.get("reason") == "no_active_record"

    def test_non_git_project_allows(self, project_root: Path) -> None:
        """No git repo → commit count is 0 → no warn."""
        policy = SessionLogEnforcer()
        ts = time.time() - 60
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, project_root, session_id="s1", timestamp=ts
            ),
            None,
        )
        verdict = policy.evaluate(
            _make_event(EventType.STOP, project_root, session_id="s1"),
            None,
        )
        assert verdict.is_allowing()
        assert verdict.metadata.get("commit_count") == 0

    def test_git_no_commits_since_start_allows(self, git_project: Path) -> None:
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.is_allowing()
        assert verdict.metadata.get("commit_count") == 0


# =====================================================================
# STOP stage — enforcement paths
# =====================================================================


class TestStopEnforcement:
    def test_commits_without_log_warns(self, git_project: Path) -> None:
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: in-session work", at_epoch=ts + 10)
        _git_commit(git_project, "fix: more work", at_epoch=ts + 20)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.action == "warn"
        assert "2 commits" in (verdict.message or "")
        assert "write_session_log" in (verdict.message or "")
        assert verdict.metadata["commit_count"] == 2
        assert verdict.metadata["log_present"] is False

    def test_commits_with_log_allows(self, git_project: Path) -> None:
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: shipped", at_epoch=ts + 10)
        # AI called write_session_log — entry lands after session start
        _write_session_log_entry(git_project, ts_epoch=ts + 5)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.is_allowing()
        assert verdict.metadata.get("log_present") is True

    def test_stale_log_before_session_does_not_satisfy(
        self,
        git_project: Path,
    ) -> None:
        """A pre-session log doesn't count — must be in window."""
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)

        # Stale log written WAY before this session started
        _write_session_log_entry(git_project, ts_epoch=ts - 3600)

        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: stuff", at_epoch=ts + 10)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.action == "warn"

    def test_single_commit_uses_singular(self, git_project: Path) -> None:
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "fix: one thing", at_epoch=ts + 10)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.action == "warn"
        assert "1 commit " in (verdict.message or "")
        assert "1 commits" not in (verdict.message or "")


# =====================================================================
# Mode switching
# =====================================================================


class TestMode:
    def test_block_mode_blocks(
        self,
        git_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_SESSION_LOG_ENFORCER_MODE", "block")
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: a", at_epoch=ts + 10)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.is_blocking()
        assert verdict.metadata["mode"] == "block"

    def test_off_mode_allows_even_with_gap(
        self,
        git_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_SESSION_LOG_ENFORCER_MODE", "off")
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: untracked", at_epoch=ts + 10)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.is_allowing()

    def test_unknown_mode_defaults_to_warn(
        self,
        git_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_SESSION_LOG_ENFORCER_MODE", "nonsense")
        policy = SessionLogEnforcer()
        ts = _ts_after_head(git_project)
        policy.evaluate(
            _make_event(
                EventType.SESSION_START, git_project, session_id="s1", timestamp=ts
            ),
            None,
        )
        _git_commit(git_project, "feat: x", at_epoch=ts + 10)

        verdict = policy.evaluate(
            _make_event(EventType.STOP, git_project, session_id="s1"),
            None,
        )
        assert verdict.action == "warn"


# =====================================================================
# Direct helper coverage
# =====================================================================


class TestHelpers:
    def test_count_commits_non_git_returns_zero(self, project_root: Path) -> None:
        assert _count_commits_since(project_root, 0.0) == 0

    def test_count_commits_baseline_only(self, git_project: Path) -> None:
        # threshold AFTER baseline → 0 commits since
        assert _count_commits_since(git_project, _ts_after_head(git_project)) == 0

    def test_count_commits_counts_after_threshold(self, git_project: Path) -> None:
        ts = _ts_after_head(git_project)
        _git_commit(git_project, "feat: post-threshold", at_epoch=ts + 10)
        assert _count_commits_since(git_project, ts) == 1

    def test_session_log_written_missing_file(self, project_root: Path) -> None:
        # remove the sessions file the fixture pre-created
        (project_root / ".codevira" / "sessions.jsonl").unlink(missing_ok=True)
        assert _session_log_written(project_root, time.time()) is False

    def test_session_log_written_in_window(self, project_root: Path) -> None:
        threshold = time.time() - 100
        _write_session_log_entry(project_root, ts_epoch=threshold + 50)
        assert _session_log_written(project_root, threshold) is True

    def test_session_log_written_only_before_threshold(
        self,
        project_root: Path,
    ) -> None:
        threshold = time.time()
        _write_session_log_entry(project_root, ts_epoch=threshold - 1000)
        assert _session_log_written(project_root, threshold) is False


# =====================================================================
# Cross-tool registration — confirms register_default_policies wires us in
# =====================================================================


class TestRegistration:
    def test_session_log_enforcer_registered_by_default(self) -> None:
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "session_log_enforcer" in names

    def test_register_is_idempotent(self) -> None:
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        assert names.count("session_log_enforcer") == 1
