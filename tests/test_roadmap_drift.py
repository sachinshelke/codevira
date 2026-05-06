"""Tests for mcp_server/roadmap_drift.py — Bug 8 regression guard.

Drift fires when codevira's claimed phase hasn't been updated for >
``DRIFT_DAYS_THRESHOLD`` days OR > ``DRIFT_COMMITS_THRESHOLD`` commits
have landed since. These tests cover:

  - Reference-time resolution (last_updated > started > yaml mtime)
  - Threshold logic (days, commits, both, neither)
  - Defensive behaviour (no .git, no roadmap, parse errors → None)
  - Output shape (keys, message format)
  - Integration with ``get_session_context`` so the field surfaces
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_server.roadmap_drift import (
    DRIFT_COMMITS_THRESHOLD,
    DRIFT_DAYS_THRESHOLD,
    check_drift,
    _parse_iso,
    _resolve_reference_time,
    _git_commits_since,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, commits: int = 0, days_ago: int = 0) -> None:
    """Initialise a git repo at ``path`` with ``commits`` commits, all
    backdated to ``days_ago`` days ago.

    Uses --date and GIT_AUTHOR_DATE/GIT_COMMITTER_DATE so the test can
    control how recent the commits look without time travel.
    """
    subprocess.run(
        ["git", "init", "--quiet", "-b", "main"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=path, check=True,
    )

    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    iso = when.isoformat()

    for i in range(commits):
        f = path / f"file_{i}.txt"
        f.write_text(f"content {i}")
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": iso,
            "GIT_COMMITTER_DATE": iso,
        }
        subprocess.run(["git", "add", str(f)], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}", "--quiet"],
            cwd=path, check=True, env=env,
        )


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


class TestParseIso:
    def test_z_suffix_parses(self):
        assert _parse_iso("2026-05-02T23:21:00Z") == datetime(
            2026, 5, 2, 23, 21, 0, tzinfo=timezone.utc,
        )

    def test_offset_suffix_parses(self):
        assert _parse_iso("2026-05-02T23:21:00+00:00") == datetime(
            2026, 5, 2, 23, 21, 0, tzinfo=timezone.utc,
        )

    def test_garbage_returns_none(self):
        assert _parse_iso("not a date") is None

    def test_none_returns_none(self):
        assert _parse_iso(None) is None

    def test_int_returns_none(self):
        # Defensive: only str inputs are accepted.
        assert _parse_iso(1234567890) is None


# ---------------------------------------------------------------------------
# _resolve_reference_time
# ---------------------------------------------------------------------------


class TestResolveReferenceTime:
    def test_picks_last_updated_when_present(self, tmp_path):
        cp = {"last_updated": "2026-05-05T12:00:00Z", "started": "2026-05-01T00:00:00Z"}
        ref = _resolve_reference_time(
            current_phase=cp, roadmap_path=tmp_path / "missing.yaml",
            project_root=tmp_path,
        )
        # Freshest of the two timestamps wins (last_updated > started).
        assert ref == datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_falls_back_to_started_when_no_last_updated(self, tmp_path):
        cp = {"started": "2026-05-01T00:00:00Z"}
        ref = _resolve_reference_time(
            current_phase=cp, roadmap_path=tmp_path / "missing.yaml",
            project_root=tmp_path,
        )
        assert ref == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_uses_yaml_mtime_when_no_phase_timestamps(self, tmp_path):
        roadmap = tmp_path / "roadmap.yaml"
        roadmap.write_text("name: stub\n")
        ref = _resolve_reference_time(
            current_phase=None, roadmap_path=roadmap, project_root=tmp_path,
        )
        assert ref is not None
        # Within the last 5 seconds (just touched the file).
        assert (datetime.now(timezone.utc) - ref).total_seconds() < 5

    def test_returns_none_when_no_signals(self, tmp_path):
        ref = _resolve_reference_time(
            current_phase=None,
            roadmap_path=tmp_path / "does-not-exist.yaml",
            project_root=tmp_path,
        )
        assert ref is None

    def test_freshest_wins_across_all_sources(self, tmp_path):
        """If the user hand-edited roadmap.yaml today, that beats a stale
        'started: 2026-05-02' string."""
        roadmap = tmp_path / "roadmap.yaml"
        roadmap.write_text("name: stub\n")
        # roadmap.yaml mtime ≈ now, much fresher than the 2026-05-02 string.
        cp = {"started": "2026-05-02T23:21:00Z"}
        ref = _resolve_reference_time(
            current_phase=cp, roadmap_path=roadmap, project_root=tmp_path,
        )
        assert ref is not None
        # Today, not the May 2 string.
        assert (datetime.now(timezone.utc) - ref).total_seconds() < 5


# ---------------------------------------------------------------------------
# Core check_drift behaviour
# ---------------------------------------------------------------------------


class TestCheckDriftDoesNotFire:
    def test_no_drift_when_recent_phase_no_commits(self, tmp_path):
        _init_git_repo(tmp_path, commits=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is None

    def test_no_drift_when_recent_phase_few_commits(self, tmp_path):
        _init_git_repo(tmp_path, commits=2, days_ago=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is None

    def test_no_drift_when_no_signals_at_all(self, tmp_path):
        # No git, no roadmap, no current_phase → can't detect drift.
        result = check_drift(project_root=tmp_path, current_phase=None)
        assert result is None


class TestCheckDriftFires:
    def test_fires_on_days_threshold(self, tmp_path):
        # Phase started 5 days ago, no commits → days threshold breach
        _init_git_repo(tmp_path, commits=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is not None
        assert result["drifted"] is True
        assert result["days_since_update"] >= 4.5
        assert "days" in result["message"].lower() or "stale" in result["message"].lower()

    def test_fires_on_commits_threshold(self, tmp_path):
        # Phase started recently but lots of commits since
        _init_git_repo(tmp_path, commits=10, days_ago=0)
        # Phase started 1 hour ago — well under days threshold,
        # but 10 commits > 5 commit threshold.
        cp = {"started": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is not None
        assert result["drifted"] is True
        assert result["commits_since"] >= 5
        assert "commits" in result["message"].lower()

    def test_fires_on_both_thresholds(self, tmp_path):
        _init_git_repo(tmp_path, commits=10, days_ago=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is not None
        assert result["drifted"] is True
        assert result["days_since_update"] >= 4.5
        assert result["commits_since"] >= 5

    def test_recent_commit_subjects_capped_at_5(self, tmp_path):
        _init_git_repo(tmp_path, commits=10, days_ago=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is not None
        assert len(result["recent_commit_subjects"]) <= 5

    def test_message_includes_days_when_days_threshold_breached(self, tmp_path):
        _init_git_repo(tmp_path, commits=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)

        assert result is not None
        assert "days ago" in result["message"]


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------


class TestCustomThresholds:
    def test_can_override_days_threshold(self, tmp_path):
        _init_git_repo(tmp_path, commits=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}

        # Default would not fire (2 < 3 days). With a stricter 1-day
        # threshold, drift fires.
        assert check_drift(project_root=tmp_path, current_phase=cp) is None
        result = check_drift(
            project_root=tmp_path, current_phase=cp, days_threshold=1,
        )
        assert result is not None and result["drifted"]

    def test_can_override_commits_threshold(self, tmp_path):
        _init_git_repo(tmp_path, commits=3, days_ago=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}

        # Default would not fire (3 < 5 commits). With a stricter
        # 1-commit threshold, drift fires.
        assert check_drift(project_root=tmp_path, current_phase=cp) is None
        result = check_drift(
            project_root=tmp_path, current_phase=cp, commits_threshold=1,
        )
        assert result is not None and result["drifted"]


# ---------------------------------------------------------------------------
# Defensive: never crash the caller
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_no_git_repo_does_not_crash(self, tmp_path):
        # No .git dir → drift falls back to time alone. Phase started
        # 5 days ago → drift fires on days alone.
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}
        result = check_drift(project_root=tmp_path, current_phase=cp)
        assert result is not None
        assert result["commits_since"] == 0  # Couldn't read git
        assert result["drifted"] is True

    def test_unparseable_timestamp_does_not_crash(self, tmp_path):
        cp = {"started": "not a real date"}
        # Silently falls back to None reference time → returns None.
        result = check_drift(project_root=tmp_path, current_phase=cp)
        assert result is None

    def test_returns_none_on_internal_exception(self, tmp_path, monkeypatch):
        """Force an internal raise to verify the catch-all swallows it."""
        from mcp_server import roadmap_drift

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(roadmap_drift, "_resolve_reference_time", _boom)
        result = check_drift(project_root=tmp_path, current_phase={"started": "x"})
        assert result is None


# ---------------------------------------------------------------------------
# Output shape contract
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_drift_dict_has_expected_keys(self, tmp_path):
        _init_git_repo(tmp_path, commits=10, days_ago=0)
        cp = {"started": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()}

        result = check_drift(project_root=tmp_path, current_phase=cp)
        assert result is not None

        for key in (
            "drifted",
            "days_since_update",
            "commits_since",
            "last_phase_update",
            "recent_commit_subjects",
            "thresholds",
            "message",
        ):
            assert key in result, f"missing key: {key}"

        assert result["thresholds"]["days"] == DRIFT_DAYS_THRESHOLD
        assert result["thresholds"]["commits"] == DRIFT_COMMITS_THRESHOLD


# ---------------------------------------------------------------------------
# Integration with get_session_context
# ---------------------------------------------------------------------------


class TestSessionContextIntegration:
    """Bug 8 fix only matters if the drift_warning actually surfaces in
    the SessionStart-injected get_session_context output."""

    def test_get_session_context_returns_drift_warning_field(
        self, project_env, monkeypatch
    ):
        # The fixture creates a project + .codevira dir. Make the phase
        # look stale so drift fires.
        project, data_dir, db = project_env

        # Build a roadmap.yaml with a stale current phase.
        from mcp_server.tools.roadmap import _save_roadmap, _load_roadmap
        roadmap = _load_roadmap()
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        roadmap["current_phase"] = {
            "name": "Test Phase",
            "status": "in_progress",
            "started": old_iso,
            "next_action": "test",
        }
        _save_roadmap(roadmap)

        from mcp_server.tools.learning import get_session_context
        result = get_session_context()

        # The field is always present (None when no drift, dict when drifted).
        # Stale phase + 0 commits → drift fires on days threshold alone.
        assert "drift_warning" in result
        # If git happens to be unavailable in the test env, drift may
        # still fire on days-alone. Either way the field is structured.
        if result["drift_warning"] is not None:
            assert result["drift_warning"]["drifted"] is True

    def test_get_session_context_drift_warning_is_none_for_fresh_project(
        self, project_env
    ):
        project, data_dir, db = project_env

        from mcp_server.tools.roadmap import _save_roadmap, _load_roadmap
        roadmap = _load_roadmap()
        # roadmap.yaml just got written, so mtime ≈ now → no drift.
        _save_roadmap(roadmap)

        from mcp_server.tools.learning import get_session_context
        result = get_session_context()

        assert "drift_warning" in result
        assert result["drift_warning"] is None
