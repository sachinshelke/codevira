"""
Phase 27 — two quality fixes for v3.7.0.

Fix C: memory_fanout._build_observation must resolve the edited path from the
FULL key set the editing tools use (NotebookEdit → notebook_path), not just
file_path/path — else it logs "<unknown>" and the activity heatmap goes blind.

Fix D: fix_history.refresh_fix_history_if_stale re-scans git only when HEAD
advanced, so a long-lived server sees fix: commits made after boot (the
startup-only scan alone goes stale).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.memory_fanout import _build_observation
from indexer import fix_history


class TestFanoutPathResolution:
    """Fix C: no more '<unknown>' for tools that name their file differently."""

    def _event(self, tool_name: str, tool_input: dict) -> HookEvent:
        return HookEvent(
            event_type=EventType.POST_TOOL_USE,
            project_root=Path("/tmp"),
            tool_name=tool_name,
            tool_input=tool_input,
        )

    def test_notebook_edit_resolves_notebook_path(self):
        obs = _build_observation(
            self._event("NotebookEdit", {"notebook_path": "/proj/analysis.ipynb"})
        )
        assert obs is not None
        assert "<unknown>" not in obs["content"]
        assert "/proj/analysis.ipynb" in obs["content"]
        # And the activity mirror carries the real path (heatmap not blind).
        assert obs["_activity_file_path"] == "/proj/analysis.ipynb"

    def test_camelcase_filepath_resolved(self):
        obs = _build_observation(self._event("update_node", {"filePath": "/proj/x.py"}))
        assert obs is not None
        assert "<unknown>" not in obs["content"]

    def test_plain_file_path_still_works(self):
        obs = _build_observation(self._event("Edit", {"file_path": "/proj/a.py"}))
        assert obs is not None
        assert "/proj/a.py" in obs["content"]

    def test_truly_missing_path_still_unknown_but_no_crash(self):
        obs = _build_observation(self._event("Edit", {}))
        assert obs is not None
        assert "<unknown>" in obs["content"]
        assert obs["_activity_file_path"] is None  # not mirrored to heatmap


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "Tester")


class TestFixHistoryStalenessRefresh:
    """Fix D: refresh re-scans only when HEAD moved; picks up new fix commits."""

    def test_refresh_rescans_on_head_move_and_skips_when_unchanged(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.py").write_text("x = 1\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fix: correct off-by-one in a.py")

        fix_history._last_scanned_head.clear()

        r1 = fix_history.refresh_fix_history_if_stale(repo)
        assert r1["rescanned"] is True, "first refresh must scan"

        # HEAD unchanged → no rescan (cheap path).
        r2 = fix_history.refresh_fix_history_if_stale(repo)
        assert r2["rescanned"] is False

        # A new fix: commit moves HEAD → rescan picks it up.
        (repo / "b.py").write_text("y = 2\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fix: null-deref in b.py")

        r3 = fix_history.refresh_fix_history_if_stale(repo)
        assert r3["rescanned"] is True

        fixes = fix_history.lookup(repo, "b.py")
        assert any(
            "b.py" in (f.get("file_path") or "") for f in fixes
        ), "the post-boot fix commit must be visible after a HEAD-move refresh"

    def test_refresh_no_git_repo_is_graceful(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        res = fix_history.refresh_fix_history_if_stale(not_a_repo)
        assert res["rescanned"] is False
        assert res["head"] is None

    def test_persistent_scan_error_backs_off(self, tmp_path, monkeypatch):
        """M8: a persistently-failing scan must NOT re-walk git-log every call
        while HEAD is unchanged (this runs on the anti-regression hot path)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.py").write_text("x = 1\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fix: seed")

        fix_history._last_scanned_head.clear()
        fix_history._failed_scan_head.clear()
        calls = {"n": 0}

        def _failing(*a, **k):
            calls["n"] += 1
            return {"error": "boom"}

        monkeypatch.setattr(fix_history, "scan_git_log", _failing)

        r1 = fix_history.refresh_fix_history_if_stale(repo)
        assert r1.get("error") and calls["n"] == 1  # scanned once, errored

        r2 = fix_history.refresh_fix_history_if_stale(repo)  # HEAD unchanged
        assert r2["rescanned"] is False
        assert calls["n"] == 1, "must NOT re-invoke scan_git_log during backoff"
