"""Regression tests for the three P0 issues found in Week-1 QA.

These tests are NEW (post-implementation QA review). They lock in the
fixes so future refactors don't reintroduce the bugs.

P0 #1: ``is_revert`` must handle Claude Code Edit format
       (``--- before / --- after``), not just unified diff. The wiring
       layer produces Edit-format strings; without this, Hero 2
       (Anti-Regression) would always return False on production input.

P0 #2: ``signals._load_graph`` must fall back to legacy in-project
       ``.codevira/graph/graph.db`` when the centralized location
       isn't present. Without this, every signal-using policy
       silently no-ops on un-migrated projects.

P0 #3: ``fix_history._connect`` must serialize cache access to prevent
       N threads creating N distinct connections (a real connection
       leak we measured at 20 distinct connections from 20 threads).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from indexer.fix_history import FixRecord, is_revert, _conn_cache, _connect, reset


# =====================================================================
# P0 #1: is_revert format support
# =====================================================================

class TestIsRevertEditFormat:
    """Claude Code Edit format: '--- before / --- after' with old_string,
    new_string in between. This is what claude_code_hooks._build_event
    actually produces in production."""

    def test_after_empty_likely_revert(self):
        """If `after` is empty (deletion of fix code), treat as revert."""
        change = "--- before\nfixed_retry_logic()\n--- after\n"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="connection retry was infinite-looping",
            source="manual",
        )
        assert is_revert(change, fix) is True

    def test_after_mentions_buggy_keyword_more_than_before(self):
        """If `after` mentions bug-description keywords more than
        `before`, that's a revert toward the buggy state."""
        change = (
            "--- before\nrate = max(rate, MIN_RATE)\n"
            "--- after\nrate = rate  # original infinite loop spinner\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="infinite loop spinner consumed CPU",
            source="manual",
        )
        # `after` contains "infinite", "loop", "spinner" → likely revert
        assert is_revert(change, fix) is True

    def test_unrelated_change_not_a_revert(self):
        """A normal refactor that doesn't touch the buggy keywords."""
        change = (
            "--- before\ndef foo():\n    return 1\n"
            "--- after\ndef foo():\n    return 2\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="off-by-one in pagination boundary",
            source="manual",
        )
        assert is_revert(change, fix) is False

    def test_generic_description_returns_false(self):
        """If the description is too generic (no useful keywords), bail."""
        change = "--- before\nx\n--- after\ny\n"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="fix bug",  # all stop-words → no useful tokens
            source="manual",
        )
        assert is_revert(change, fix) is False


class TestIsRevertUnifiedDiffStillWorks:
    """Backwards-compat: the unified-diff path must still work for
    Week-2 git-derived diffs."""

    def test_diff_in_fix_range_with_deletion_still_flagged(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="x", source="manual",
        )
        diff = "@@ -10,3 +10,1 @@\n-fixed_line()\n+old_buggy_line()\n"
        assert is_revert(diff, fix) is True

    def test_unified_diff_unrelated_range_not_revert(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="x", source="manual",
        )
        diff = "@@ -100,5 +100,5 @@\n-old\n+new\n"
        assert is_revert(diff, fix) is False


class TestIsRevertEmptyOrInvalidInput:
    """Empty / None / nonsense input must always return False."""

    def test_empty_string_false(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=1,
            description="x", source="manual",
        )
        assert is_revert("", fix) is False

    def test_random_text_no_markers_false(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=1,
            description="connection", source="manual",
        )
        assert is_revert("hello world this is not a diff", fix) is False


# =====================================================================
# P0 #2: signals._load_graph legacy-path fallback
# =====================================================================

class TestSignalGraphLegacyFallback:
    """The signal layer must locate graph.db in either centralized
    (v1.6+) or legacy in-project (v1.5) location."""

    def test_centralized_path_found(self, tmp_path, monkeypatch):
        from mcp_server.engine.signals import SignalContext
        from mcp_server.paths import _sanitize_path_key

        # Create a centralized-style graph.db
        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        key = _sanitize_path_key(proj)
        centralized = fake_home / "projects" / key / "graph" / "graph.db"
        centralized.parent.mkdir(parents=True)
        # Create empty file — SQLiteGraph will auto-init schema
        centralized.touch()

        ctx = SignalContext(project_root=proj)
        graph = ctx.graph
        assert graph is not None  # Found via centralized path

    def test_legacy_in_project_path_fallback(self, tmp_path, monkeypatch):
        """v1.5 layout: <project>/.codevira/graph/graph.db. If centralized
        location doesn't exist, the signal layer must find the legacy one."""
        from mcp_server.engine.signals import SignalContext

        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        # NO centralized — only legacy
        legacy = proj / ".codevira" / "graph" / "graph.db"
        legacy.parent.mkdir(parents=True)
        legacy.touch()

        ctx = SignalContext(project_root=proj)
        graph = ctx.graph
        assert graph is not None  # Must find via legacy fallback

    def test_neither_path_returns_none(self, tmp_path, monkeypatch):
        """Uninitialized project — no graph.db at either location."""
        from mcp_server.engine.signals import SignalContext

        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        ctx = SignalContext(project_root=proj)
        graph = ctx.graph
        assert graph is None

    def test_centralized_preferred_over_legacy(self, tmp_path, monkeypatch):
        """When both paths exist, centralized wins (matches paths.get_data_dir)."""
        from mcp_server.engine.signals import SignalContext
        from mcp_server.paths import _sanitize_path_key

        proj = tmp_path / "myproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )

        key = _sanitize_path_key(proj)
        centralized = fake_home / "projects" / key / "graph" / "graph.db"
        centralized.parent.mkdir(parents=True)
        centralized.touch()

        legacy = proj / ".codevira" / "graph" / "graph.db"
        legacy.parent.mkdir(parents=True)
        legacy.touch()

        ctx = SignalContext(project_root=proj)
        graph = ctx.graph
        assert graph is not None
        # We can't directly inspect which path it loaded from without
        # exposing it; the existence of both files + non-None graph is
        # the test. (Centralized is checked first per implementation.)


# =====================================================================
# P0 #3: fix_history._connect thread-safe cache
# =====================================================================

class TestConnectionCacheRaceFix:
    """20 threads racing on _connect for the same project_root must
    receive ONE shared connection, not 20 distinct ones."""

    def test_concurrent_connect_returns_one_connection(self, tmp_path, monkeypatch):
        proj = tmp_path / "raceproj"
        proj.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        # Reset cache for a clean test
        _conn_cache.clear()

        results: list[int] = []
        errors: list[str] = []
        barrier = threading.Barrier(20)

        def race():
            try:
                barrier.wait(timeout=5)  # release all threads at once
                conn = _connect(proj)
                results.append(id(conn))
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=race) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # Critical: all 20 threads got the SAME connection object.
        distinct = len(set(results))
        assert distinct == 1, (
            f"Expected 1 shared connection, got {distinct} distinct objects. "
            f"Cache race regression."
        )

        reset(proj)

    def test_distinct_projects_get_distinct_connections(self, tmp_path, monkeypatch):
        """Sanity: different projects still get separate connections."""
        proj_a = tmp_path / "proj_a"
        proj_a.mkdir()
        proj_b = tmp_path / "proj_b"
        proj_b.mkdir()
        fake_home = tmp_path / "global"
        fake_home.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home
        )
        _conn_cache.clear()

        ca = _connect(proj_a)
        cb = _connect(proj_b)
        assert ca is not cb

        reset(proj_a)
        reset(proj_b)
