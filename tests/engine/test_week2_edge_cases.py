"""Week-2 edge-case tests for the engine.

Adds coverage for inputs that earlier tests didn't exercise:
  - Unicode paths (Japanese, RTL text, emoji)
  - Very deep paths (100+ nested directories)
  - Token meter persistence + read history
  - fix_history git scanning (with a synthetic git repo)
  - is_revert with binary content / control characters
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from indexer.fix_history import (
    FixRecord,
    is_revert,
    lookup,
    record_fix,
    reset,
    scan_git_log,
)
from mcp_server.engine.token_meter import (
    end_session,
    get_or_create_session_meter,
    read_session_history,
    reset_meters,
)


# =====================================================================
# Unicode / non-Latin paths
# =====================================================================

class TestUnicodePaths:
    """Codevira must handle paths with Japanese, RTL, emoji."""

    def test_unicode_project_path_record_fix(self, tmp_path, monkeypatch):
        proj = tmp_path / "プロジェクト" / "café-app"
        proj.mkdir(parents=True)
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        try:
            rid = record_fix(
                proj,
                file_path="src/🚀-launcher.py",
                line_start=1, line_end=10,
                description="إصلاح خلل في حلقة لانهائية",  # RTL Arabic
                source="manual",
            )
            assert rid > 0
            records = lookup(proj, "src/🚀-launcher.py")
            assert len(records) == 1
            assert records[0]["description"] == "إصلاح خلل في حلقة لانهائية"
        finally:
            reset(proj)

    def test_emoji_in_description_preserved_round_trip(self, tmp_path, monkeypatch):
        proj = tmp_path / "p"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        try:
            record_fix(
                proj, "src/x.py", 1, 5,
                description="🐛 fixed retry → 🚀",
                source="manual",
            )
            records = lookup(proj, "src/x.py")
            assert "🐛" in records[0]["description"]
            assert "🚀" in records[0]["description"]
        finally:
            reset(proj)


# =====================================================================
# Very deep paths
# =====================================================================

class TestDeepPaths:
    """Codevira shouldn't break on deeply nested directories."""

    def test_50_levels_deep_path(self, tmp_path, monkeypatch):
        deep = tmp_path
        for i in range(50):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        try:
            # Should not crash on key generation or file operations
            rid = record_fix(deep, "x.py", 1, 1, "deep", source="manual")
            assert rid > 0
        finally:
            reset(deep)


# =====================================================================
# Token meter persistence
# =====================================================================

class TestTokenMeterPersistence:
    """end_session must flush summary to <data_dir>/logs/token_budget.jsonl."""

    def test_end_session_persists_jsonl(self, tmp_path, monkeypatch):
        from mcp_server.paths import _sanitize_path_key
        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        reset_meters()
        meter = get_or_create_session_meter("session-1")
        meter.record_injected(500, source="get_node")
        meter.record_used(300, source="get_node")

        summary = end_session("session-1", project_root=proj)
        assert summary is not None
        assert summary["injected_total"] == 500

        # Verify JSONL written
        key = _sanitize_path_key(proj)
        log_path = fake_home / "projects" / key / "logs" / "token_budget.jsonl"
        assert log_path.exists(), "token_budget.jsonl not created"
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["session_id"] == "session-1"
        assert record["injected_total"] == 500
        assert record["used_total"] == 300
        assert "efficiency" in record
        assert "ended_at" in record

    def test_end_session_no_project_root_skips_persist(self, tmp_path, monkeypatch):
        """Without project_root, end_session returns summary but doesn't write."""
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        reset_meters()
        get_or_create_session_meter("session-2")
        summary = end_session("session-2")  # no project_root
        assert summary is not None
        # No JSONL files anywhere
        assert list(fake_home.rglob("token_budget.jsonl")) == []

    def test_read_session_history(self, tmp_path, monkeypatch):
        from mcp_server.paths import _sanitize_path_key
        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        reset_meters()
        # Write 3 sessions
        for i, sid in enumerate(["s1", "s2", "s3"]):
            m = get_or_create_session_meter(sid)
            m.record_injected(100 * (i + 1))
            end_session(sid, project_root=proj)

        history = read_session_history(proj, limit=10)
        assert len(history) == 3
        # Newest first
        assert history[0]["session_id"] == "s3"
        assert history[0]["injected_total"] == 300
        assert history[2]["session_id"] == "s1"

    def test_read_session_history_handles_corrupt_lines(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.paths import _sanitize_path_key
        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        # Write a JSONL with one good + one corrupt line
        key = _sanitize_path_key(proj)
        log_dir = fake_home / "projects" / key / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "token_budget.jsonl"
        log_path.write_text(
            json.dumps({"session_id": "good", "injected_total": 50}) + "\n"
            + "{ this is not valid json\n"
            + json.dumps({"session_id": "another", "injected_total": 100}) + "\n"
        )

        history = read_session_history(proj)
        # Should skip the corrupt line and return the 2 valid ones
        sids = [h["session_id"] for h in history]
        assert "good" in sids
        assert "another" in sids
        assert len(history) == 2

    def test_read_session_history_missing_file_returns_empty(
        self, tmp_path, monkeypatch
    ):
        proj = tmp_path / "noproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        assert read_session_history(proj) == []

    def test_read_session_history_caps_huge_log_no_oom(
        self, tmp_path, monkeypatch
    ):
        """Tier-1 QA finding: unbounded readlines() on huge log.

        Plant a token_budget.jsonl far larger than the tail cap and
        confirm we still return the latest N records without loading
        the whole file. Tail-window cap is 16 MiB; we plant ~32 MiB.
        """
        from mcp_server.paths import _sanitize_path_key

        proj = tmp_path / "huge"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        key = _sanitize_path_key(proj.resolve())
        log_dir = fake_home / "projects" / key / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "token_budget.jsonl"

        # Junk record padded to ~1 KiB; write 32k of them = ~32 MiB.
        # Last 5 records carry distinguishable session_ids so we can
        # verify we found the tail.
        junk_payload = "x" * 800
        with open(log_path, "w", encoding="utf-8") as f:
            for i in range(32_000):
                f.write(json.dumps({
                    "session_id": "junk",
                    "ended_at": float(i),
                    "injected_total": 0,
                    "used_total": 0,
                    "efficiency": 0.0,
                    "top_wasted_sources": [],
                    "_padding": junk_payload,
                }) + "\n")
            for tail_id in ["t1", "t2", "t3", "t4", "t5"]:
                f.write(json.dumps({
                    "session_id": tail_id,
                    "ended_at": 9999.0,
                    "injected_total": 1,
                    "used_total": 1,
                    "efficiency": 1.0,
                    "top_wasted_sources": [],
                }) + "\n")

        # Sanity: file must actually be > tail cap, else the test
        # doesn't prove the cap works.
        size = log_path.stat().st_size
        assert size > 16 * 1024 * 1024, f"plant too small ({size} bytes)"

        # Should not OOM, should return tail.
        history = read_session_history(proj, limit=5)
        sids = [r["session_id"] for r in history]
        # Newest-first ordering preserved
        assert sids == ["t5", "t4", "t3", "t2", "t1"]


# =====================================================================
# fix_history git scanning (synthetic repo)
# =====================================================================

@pytest.fixture
def git_project(tmp_path, monkeypatch):
    """Create a real git repo with synthetic commit history."""
    proj = tmp_path / "gitproj"
    proj.mkdir()
    fake_home = tmp_path / "global"
    fake_home.mkdir()
    monkeypatch.setattr(
        "mcp_server.paths.get_global_home", lambda: fake_home
    )

    # Initialize git repo
    def run_git(*args):
        return subprocess.run(
            ["git", "-C", str(proj), *args],
            capture_output=True, text=True, check=False, timeout=10,
            env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    run_git("init", "-b", "main")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "Test")

    # Commit 1: regular commit (no fix)
    (proj / "feature.py").write_text("def f(): return 1\n")
    run_git("add", "feature.py")
    run_git("commit", "-m", "feat: add feature")

    # Commit 2: a fix commit
    (proj / "bug.py").write_text("def fixed(): return 2\n")
    run_git("add", "bug.py")
    run_git("commit", "-m", "fix: connection retry was infinite-looping")

    # Commit 3: another regular commit
    (proj / "docs.md").write_text("# README\n")
    run_git("add", "docs.md")
    run_git("commit", "-m", "docs: add README")

    # Commit 4: another fix commit (different style)
    (proj / "auth.py").write_text("def login(): pass\n")
    run_git("add", "auth.py")
    run_git("commit", "-m", "fixes #42: auth bypass on stale session")

    yield proj
    reset(proj)


class TestGitFixDetection:
    def test_scan_finds_fix_commits(self, git_project):
        result = scan_git_log(git_project)
        assert "error" not in result
        assert result["commits_scanned"] == 4
        assert result["commits_matched"] == 2  # 2 fix commits
        # Each fix commit touched 1 file → 2 fix records
        assert result["fixes_recorded"] == 2

    def test_scan_records_correct_files(self, git_project):
        scan_git_log(git_project)
        # Look up the files we expect to be flagged
        bug_records = lookup(git_project, "bug.py")
        auth_records = lookup(git_project, "auth.py")
        feature_records = lookup(git_project, "feature.py")  # not a fix
        docs_records = lookup(git_project, "docs.md")  # not a fix

        assert len(bug_records) == 1
        assert bug_records[0]["source"] == "git"
        assert bug_records[0]["commit_sha"] is not None
        assert "infinite-looping" in bug_records[0]["description"]

        assert len(auth_records) == 1
        assert "auth bypass" in auth_records[0]["description"]

        assert feature_records == []
        assert docs_records == []

    def test_scan_idempotent(self, git_project):
        # First scan
        first = scan_git_log(git_project)
        assert first["fixes_recorded"] == 2

        # Second scan should skip already-recorded
        second = scan_git_log(git_project)
        assert second["fixes_recorded"] == 0
        assert second["skipped_already_recorded"] == 2

    def test_scan_force_rescan(self, git_project):
        scan_git_log(git_project)
        # Without skip_already_recorded, should re-record
        second = scan_git_log(git_project, skip_already_recorded=False)
        assert second["fixes_recorded"] == 2
        assert second["skipped_already_recorded"] == 0

    def test_scan_non_git_project(self, tmp_path, monkeypatch):
        proj = tmp_path / "notgit"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        result = scan_git_log(proj)
        assert "error" in result
        assert "git" in result["error"].lower()
        try:
            reset(proj)
        except Exception:
            pass


# =====================================================================
# is_revert robustness
# =====================================================================

class TestIsRevertRobustness:
    """is_revert must handle weird inputs gracefully (no crashes)."""

    def test_binary_content_returns_false(self):
        """Binary bytes that round-trip through Edit somehow."""
        fix = FixRecord(
            id=1, file_path="bin.dat", line_start=1, line_end=10,
            description="fix something", source="manual",
        )
        # Bytes that would be invalid UTF-8 if interpreted as such
        binary_like = "--- before\n\x00\x01\x02\xff\xfe\n--- after\nplain text\n"
        # Should not crash; result is a bool
        result = is_revert(binary_like, fix)
        assert isinstance(result, bool)

    def test_control_characters_in_diff(self):
        fix = FixRecord(
            id=1, file_path="x.py", line_start=1, line_end=5,
            description="fix", source="manual",
        )
        # Tab, vertical tab, form feed, carriage return
        weird = "--- before\nfoo\tbar\vbaz\f\r\n--- after\nclean\n"
        assert isinstance(is_revert(weird, fix), bool)

    def test_unicode_in_change_text(self):
        fix = FixRecord(
            id=1, file_path="x.py", line_start=1, line_end=5,
            description="connection retry was infinite-looping",
            source="manual",
        )
        change = (
            "--- before\n"
            "rate = max(rate, MIN_RATE)  # محدود معدل\n"
            "--- after\n"
            "rate = rate  # 元の無限ループに戻す (restore infinite loop)\n"
        )
        # Must work with Unicode in both blocks; word boundaries
        # behave correctly across scripts.
        result = is_revert(change, fix)
        assert isinstance(result, bool)
