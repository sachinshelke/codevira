"""4.0 Step 3.2 — session identity must be joinable.

v3.0.1 replaced a literal ``"ad-hoc"`` with ``ad-hoc-<random>`` to stop
concurrent IDEs colliding into one bucket. That fixed collisions and
created the opposite defect: a fresh id was minted on EVERY call.

Measured before this fix, on the flagship dogfood repo:
  754 activity rows -> 754 distinct session ids
  749 working rows  -> 749 distinct
  **0 of 116 decisions shared a session with an edit row**

Every session-scoped feature — working memory, skill induction, anchor
derivation — was joining on a key that was unique by construction.

Done-when for Step 3.2: >0 decisions share a session_id with an edit row.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import activity_store, decisions_store, paths


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    # Reset the process-stable fallback so tests don't leak into each other.
    # raising=False so this fixture also runs against pre-4.0 code, which
    # makes the RED proof an assertion failure rather than a collection error.
    monkeypatch.setattr(decisions_store, "_PROCESS_SESSION_ID", None, raising=False)
    return root


def _mark_session_start(root: Path, session_id: str) -> None:
    """Mimic session_log_enforcer's SESSION_START marker."""
    cache = paths.codevira_cache_dir(root)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "active_sessions.jsonl").write_text(
        json.dumps(
            {"session_id": session_id, "started_at": 1.0, "project_root": str(root)}
        )
        + "\n"
    )


class TestSessionIdIsStable:
    def test_repeated_calls_return_the_same_id(self, project: Path) -> None:
        """The core defect: this was False, so nothing could correlate."""
        assert (
            decisions_store.default_session_id() == decisions_store.default_session_id()
        )

    def test_live_client_session_wins(self, project: Path) -> None:
        _mark_session_start(project, "518d5de8-00ab-4a06-a2c7-b3ef6cce853b")
        assert (
            decisions_store.default_session_id()
            == "518d5de8-00ab-4a06-a2c7-b3ef6cce853b"
        )

    def test_latest_marker_wins_when_a_session_restarts(self, project: Path) -> None:
        cache = paths.codevira_cache_dir(project)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "active_sessions.jsonl").write_text(
            json.dumps({"session_id": "older"})
            + "\n"
            + json.dumps({"session_id": "newer"})
            + "\n"
        )
        assert decisions_store.default_session_id() == "newer"

    def test_falls_back_when_no_marker_exists(self, project: Path) -> None:
        sid = decisions_store.default_session_id()
        assert sid.startswith("ad-hoc-")

    def test_corrupt_marker_does_not_raise(self, project: Path) -> None:
        cache = paths.codevira_cache_dir(project)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "active_sessions.jsonl").write_text("{not json\n")
        assert decisions_store.default_session_id().startswith("ad-hoc-")


class TestDecisionsJoinToEdits:
    def test_a_decision_shares_a_session_with_an_edit_row(self, project: Path) -> None:
        """Step 3.2's done-when. RED before the fix (0/116)."""
        _mark_session_start(project, "sess-abc123")

        did = decisions_store.record("Use SQLite for the index", file_path="a.py")
        activity_store.add("a.py", kind=activity_store.KIND_EDIT)

        decision_sessions = {
            json.loads(x)["session_id"]
            for x in paths.decisions_path(project).read_text().splitlines()
            if x.strip()
        }
        edit_sessions = {
            json.loads(x)["session_id"]
            for x in paths.activity_path(project).read_text().splitlines()
            if x.strip() and json.loads(x).get("kind") == activity_store.KIND_EDIT
        }

        shared = decision_sessions & edit_sessions
        assert shared, f"no join: decisions={decision_sessions} edits={edit_sessions}"
        assert "sess-abc123" in shared
        assert did  # recorded

    def test_join_holds_without_a_marker_too(self, project: Path) -> None:
        """Even with no client session, one process must correlate."""
        decisions_store.record("A decision", file_path="b.py")
        activity_store.add("b.py", kind=activity_store.KIND_EDIT)

        d = {
            json.loads(x)["session_id"]
            for x in paths.decisions_path(project).read_text().splitlines()
            if x.strip()
        }
        e = {
            json.loads(x)["session_id"]
            for x in paths.activity_path(project).read_text().splitlines()
            if x.strip()
        }
        assert d & e
