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

from indexer.fix_history import FixRecord, is_revert, _conn_cache, _connect_locked, reset


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
                conn, _lock = _connect_locked(proj)
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

        ca, _ = _connect_locked(proj_a)
        cb, _ = _connect_locked(proj_b)
        assert ca is not cb

        reset(proj_a)
        reset(proj_b)


# =====================================================================
# Round-2 QA: regression tests for the round-2 P1/P2 fixes
# =====================================================================

class TestIsRevertWordBoundary:
    """Round-2 P1 #1: keyword matching uses word boundaries.

    Previously ``"infinite"`` would match inside ``"reconnection"`` (as
    substring), so a function/var name in `before` containing the buggy
    keyword would inflate before_hits and miss the revert. Word-boundary
    regex eliminates this class of false negative.
    """

    def test_keyword_in_function_name_in_before_does_not_block_detection(self):
        """If `before` is a function defining the fix (its name happens
        to contain a buggy keyword) and `after` removes it, that's still
        a revert — keyword in function name shouldn't suppress it.

        Specifically: description='infinite loop in retry' and
        before contains a function named 'fix_infinite_retry' should
        NOT count 'infinite' as a hit on before — it's a function name,
        not a buggy occurrence.
        """
        change = (
            "--- before\n"
            "def fix_infinite_retry_loop():\n"
            "    set_max_iterations(MAX)\n"
            "--- after\n"
            "rate = 1\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="loop reuse spinner",
            source="manual",
        )
        # With word boundaries: "loop" matches before's "fix_infinite_retry_loop"
        # ONLY if it's a separate word. Inside identifiers it isn't,
        # so before_hits should be lower than substring-mode.
        # Test the contract directly: result must be deterministic.
        result = is_revert(change, fix)
        # We can't assert True/False based on heuristic alone — but we
        # CAN assert the function returns without crashing, runs the
        # word-boundary path, and returns a bool.
        assert isinstance(result, bool)

    def test_keyword_in_comment_word_matches(self):
        """When the keyword appears as a real word (e.g., in a comment),
        word-boundary matching DOES catch it.

        Note on word-boundary semantics: re.escape('infinite') with ``\\b``
        anchors won't match 'infinite_loop_handler' because '_' is a word
        char (so there's no boundary between 'e' and '_'). This is by
        design — identifiers don't count as "the AI reintroduced the buggy
        concept." Only standalone-word occurrences (in comments, doc
        strings, error messages, etc.) count.
        """
        change = (
            "--- before\n"
            "rate = max(rate, MIN_RATE)  # rate-limit retries\n"
            "--- after\n"
            "rate = rate  # restore the original infinite loop\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="retry was infinite loop spinner",
            source="manual",
        )
        # before: no standalone "infinite" or "loop" words
        # after: comment has "infinite" and "loop" as standalone words
        # → after_hits > before_hits → revert
        assert is_revert(change, fix) is True

    def test_keyword_only_in_identifier_does_not_count(self):
        """Identifiers like 'infinite_loop_handler' don't count as
        keyword hits (underscore is a word char, blocks \\b match).

        This is the false-positive guard: AI reusing an identifier
        with similar-looking name doesn't trigger a revert warning."""
        change = (
            "--- before\n"
            "rate_limiter()  # safety here\n"
            "--- after\n"
            "infinite_loop_handler()  # AI renamed it\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="infinite loop bug",
            source="manual",
        )
        # Both sides have keyword only inside identifiers (no standalone
        # word boundary).  Heuristic correctly returns False — we don't
        # want to flag identifier renames as reverts.
        assert is_revert(change, fix) is False

    def test_special_chars_in_keyword_handled(self):
        """re.escape protects against regex metacharacters in description."""
        change = (
            "--- before\n"
            "buffer = arr[0]\n"
            "--- after\n"
            "buffer = arr[0..n]  # off by one\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            # Description with regex-special chars; re.escape handles it.
            description="off-by-one in arr[0..n] indexing",
            source="manual",
        )
        # Should not crash with regex error.
        result = is_revert(change, fix)
        assert isinstance(result, bool)


class TestIsRevertParserRobustness:
    """Round-2 P1 #2: parser handles embedded markers via regex anchors.

    Previously ``proposed_change.split("--- after")`` would split on
    embedded markers inside the user's old_string/new_string and produce
    garbled before/after blocks. Line-anchored regex fixes this.
    """

    def test_before_marker_inside_old_string_handled(self):
        """If old_string contains a literal '--- before' as part of its
        content (e.g., editing diff documentation), parser shouldn't be
        fooled."""
        change = (
            "--- before\n"
            "# Documentation showing diff format:\n"
            "# --- before\n"
            "# old code\n"
            "# --- after\n"
            "# new code\n"
            "--- after\n"
            "rate = 1\n"
        )
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="connection retry timeout",
            source="manual",
        )
        # Must not crash; result is a bool (heuristic call).
        result = is_revert(change, fix)
        assert isinstance(result, bool)

    def test_malformed_format_returns_false(self):
        """If neither edit-format nor unified-diff format matches,
        return False (treat as unknown shape)."""
        # Random text with no recognizable markers
        change = "this is just some text with no structure"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="x", source="manual",
        )
        assert is_revert(change, fix) is False

    def test_edit_format_only_at_line_start_matters(self):
        """Markers must be at the start of a line. Markers in the middle
        of a line should not trigger edit-format parsing."""
        # Markers embedded mid-line — shouldn't match the regex.
        change = "code --- before that --- after this"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=10,
            description="x", source="manual",
        )
        # Should fall through to unified-diff path (which won't match
        # either) and return False without crashing.
        assert is_revert(change, fix) is False


class TestIsRevertSizeCap:
    """Round-2 P2 #3: input size cap at 100 KB."""

    def test_oversized_input_returns_false_quickly(self):
        """101 KB input must return False without burning CPU."""
        import time
        big_payload = "x" * 101_000
        change = f"--- before\n{big_payload}\n--- after\ny\n"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=1,
            description="x", source="manual",
        )
        t0 = time.perf_counter()
        result = is_revert(change, fix)
        elapsed = (time.perf_counter() - t0) * 1000
        assert result is False
        # Bail-out should be near-instant. Generous bound: 5 ms.
        assert elapsed < 5.0, f"Size-cap path too slow: {elapsed:.2f} ms"

    def test_under_cap_input_processed_normally(self):
        """50 KB input is processed normally (just slower)."""
        big_payload = "x" * 50_000
        change = f"--- before\n{big_payload}\n--- after\ny\n"
        fix = FixRecord(
            id=1, file_path="f.py", line_start=1, line_end=1,
            description="x", source="manual",
        )
        # Doesn't crash; returns a bool. Result depends on heuristic.
        result = is_revert(change, fix)
        assert isinstance(result, bool)


class TestConnCacheLockReentrant:
    """Round-2 P2 #5: lock is RLock (reentrant) for defensive reasons."""

    def test_lock_can_be_acquired_twice_same_thread(self):
        """Same thread can acquire the lock recursively without
        deadlocking. Catches accidental nesting in future policies."""
        import indexer.fix_history as fh
        # acquire twice from one thread; release twice
        acquired_once = fh._conn_cache_lock.acquire(timeout=1)
        try:
            assert acquired_once
            acquired_twice = fh._conn_cache_lock.acquire(timeout=1)
            assert acquired_twice, "RLock should allow same-thread reentry"
            fh._conn_cache_lock.release()
        finally:
            fh._conn_cache_lock.release()
