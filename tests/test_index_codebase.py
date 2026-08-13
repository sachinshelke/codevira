"""
Tests for indexer/index_codebase.py

Covers:
  - _load_config(): reads config.yaml, returns project sub-dict, handles missing
  - _check_search_deps(): constant False; must stay cheap (server.py calls it
    on every tools/list)
  - _compute_hash(): SHA256 of file contents, deterministic
  - _get_changed_files(): compares file hashes against stored
  - _get_requested_files(): validates paths, computes hashes
  - get_indexing_status(): returns current background status dict
  - start_background_full_index(): starts daemon thread

4.0.1 removed the tests for the ChromaDB / semantic-index path — TestChunkToDocument,
TestGetChromaClientAndEmbedFn, TestGetEmbeddingFnExit, TestChromaCorruptionSignatureP6,
TestChromaSelfHealP2, the cmd_incremental circuit-breaker test, and the chunk-indexing
halves of TestCmdFullRebuild / TestCmdIncremental / TestCmdIncrementalLoop. Every one
reached its subject only through `_check_search_deps()`, which has returned a hardcoded
False since v2.2.0; the code they covered (_chunk_to_document, _get_chroma_client,
_get_embedding_fn, _check_chroma_health, ChromaCorrupted, _looks_like_chroma_corruption)
was unreachable and has been deleted. The graph-building assertions are all kept.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import indexer.index_codebase as idx_mod
from indexer.index_codebase import (
    _load_config,
    _check_search_deps,
    _compute_hash,
    _get_changed_files,
    _get_requested_files,
    get_indexing_status,
    start_background_full_index,
)


@contextlib.contextmanager
def _mock_rich():
    """Swap rich's Console / Table / Panel for MagicMocks — reversibly.

    ``rich`` is a hard runtime dependency (pyproject: ``rich>=13.0.0``), so
    ``rich.console`` & co. are always importable. The pre-2026-08-02 shape of
    this helper predated that: it built fake modules for ``patch.dict(
    sys.modules, ...)`` and, when the real module was already imported (always,
    in practice), assigned ``Console``/``Table``/``Panel`` straight onto it.

    That LEAKED. ``patch.dict`` restores the ``sys.modules`` *mapping*; it
    cannot undo an in-place attribute mutation on a module object it never
    owned. So once any test here had run, every later test in the process
    rendered through a MagicMock — ``cmd_status`` output came back empty (mock
    Console) or as ``<MagicMock id=...>`` (mock Table inside a real Panel).
    Invisible under collection order, because these classes sit last in this
    file; under ``-p randomly`` it broke whatever happened to run after them
    (5 failures across this file and ``test_doctor.py``).

    ``patch.object`` undoes the swap on exit, so ordering stops mattering.
    """
    import rich.console
    import rich.panel
    import rich.table

    with patch.object(
        rich.console, "Console", MagicMock(return_value=MagicMock())
    ), patch.object(
        rich.table, "Table", MagicMock(return_value=MagicMock())
    ), patch.object(rich.panel, "Panel", MagicMock(return_value=MagicMock())):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bg_globals():
    """Reset background indexing globals between tests."""
    with idx_mod._bg_lock:
        idx_mod._bg_status = "idle"
        idx_mod._bg_files_indexed = 0
        idx_mod._bg_total_files = 0
    yield
    with idx_mod._bg_lock:
        idx_mod._bg_status = "idle"
        idx_mod._bg_files_indexed = 0
        idx_mod._bg_total_files = 0


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Load .codevira/config.yaml and return the project sub-dict."""

    def test_reads_project_section(self, project_env):
        """Normal config with a 'project' key returns its contents."""
        _project, _data_dir, _db = project_env
        config = _load_config()
        assert config["name"] == "test"
        assert config["language"] == "python"
        assert "src" in config["watched_dirs"]
        assert ".py" in config["file_extensions"]

    def test_missing_config_returns_empty(self, project_env, monkeypatch):
        """If config.yaml is missing, return an empty dict."""
        project, data_dir, _db = project_env
        config_path = data_dir / "config.yaml"
        config_path.unlink()
        config = _load_config()
        assert config == {}

    def test_corrupt_yaml_returns_empty(self, project_env):
        """If config.yaml is invalid YAML, return an empty dict."""
        _project, data_dir, _db = project_env
        (data_dir / "config.yaml").write_text("{{invalid yaml: [")
        config = _load_config()
        assert config == {}


# ---------------------------------------------------------------------------
# _check_search_deps
# ---------------------------------------------------------------------------


class TestCheckSearchDeps:
    """Constant False since v2.2.0 (chromadb / sentence-transformers / torch
    were deleted along with semantic code search).

    The paired ``test_returns_true_when_available`` was dropped in 4.0.1: it
    had been ``@pytest.mark.skip``-ed since v2.2.0 and asserted a return value
    the function can no longer produce.
    """

    def test_returns_false_when_missing(self):
        """When chromadb import raises ImportError."""
        with patch.dict("sys.modules", {"chromadb": None}):
            assert _check_search_deps() is False

    def test_returns_false_even_when_chromadb_importable(self):
        """Constant False — availability of the module is irrelevant now."""
        with patch.dict("sys.modules", {"chromadb": MagicMock()}):
            assert _check_search_deps() is False


# ---------------------------------------------------------------------------
# _compute_hash
# ---------------------------------------------------------------------------


class TestComputeHash:
    """SHA256 hash of file contents."""

    def test_deterministic(self, tmp_path):
        """Same content gives the same hash every time."""
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        h1 = _compute_hash(f)
        h2 = _compute_hash(f)
        assert h1 == h2

    def test_matches_manual_sha256(self, tmp_path):
        """Hash matches a manually computed SHA256."""
        content = b"deterministic content for hashing"
        f = tmp_path / "test.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _compute_hash(f) == expected

    def test_different_content_different_hash(self, tmp_path):
        """Different file contents produce different hashes."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("alpha")
        f2.write_text("beta")
        assert _compute_hash(f1) != _compute_hash(f2)

    def test_empty_file(self, tmp_path):
        """Empty file produces the SHA256 of empty bytes."""
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert _compute_hash(f) == expected


# ---------------------------------------------------------------------------
# _get_changed_files
# ---------------------------------------------------------------------------


class TestGetChangedFiles:
    """Compare on-disk file hashes against stored hashes in SQLiteGraph."""

    def test_new_file_detected_as_changed(self, project_env):
        """A file with no stored hash is considered changed."""
        project, _data_dir, db = project_env

        # Create a watched source file
        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "new_module.py").write_text("x = 1\n")

        changed = _get_changed_files(db)
        rel_paths = [rel for rel, _hash in changed]
        assert "src/new_module.py" in rel_paths

    def test_unchanged_file_not_in_list(self, project_env):
        """A file whose hash matches the stored hash is not changed."""
        project, _data_dir, db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f = src_dir / "stable.py"
        f.write_text("y = 2\n")

        # Store the current hash
        file_hash = _compute_hash(f)
        db.update_file_hash("src/stable.py", file_hash)

        changed = _get_changed_files(db)
        rel_paths = [rel for rel, _hash in changed]
        assert "src/stable.py" not in rel_paths

    def test_modified_file_detected(self, project_env):
        """A file whose content changed since last index is detected."""
        project, _data_dir, db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f = src_dir / "modified.py"
        f.write_text("z = 3\n")

        # Store old hash then modify the file
        db.update_file_hash("src/modified.py", "stale_hash_value")

        changed = _get_changed_files(db)
        rel_paths = [rel for rel, _hash in changed]
        assert "src/modified.py" in rel_paths

    def test_nonexistent_watch_dir_is_skipped(self, project_env):
        """If a watched dir doesn't exist, it's silently skipped."""
        _project, _data_dir, db = project_env
        # Default config watches "src" but we never create it
        changed = _get_changed_files(db)
        assert changed == []

    # ------------------------------------------------------------------
    # v1.8.1 — rglob OSError tolerance (fixes 41 production crashes)
    # ------------------------------------------------------------------

    def test_tolerates_eintr_on_one_watch_dir(self, project_env, monkeypatch):
        """An InterruptedError (EINTR) raised while iterating one watch_dir
        must NOT crash the whole reindex.

        Regression for crash-log analysis 2026-04-24: 41 InterruptedError
        crashes in this exact loop. Each watcher thread (one per
        watch_dir under watchdog.Observer) must recover independently.
        """
        project, _data_dir, db = project_env
        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "good.py").write_text("ok=1\n")

        original_rglob = Path.rglob

        def boom_on_first(self, *args, **kwargs):
            # Fail only on src/, not on any internal Path.rglob the
            # codebase uses elsewhere.
            if str(self).endswith("/src"):
                raise InterruptedError(4, "Interrupted system call")
            return original_rglob(self, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", boom_on_first)
        # No exception should propagate — the wrap absorbs it.
        result = _get_changed_files(db)
        # We didn't crash; result is a list (possibly empty).
        assert isinstance(result, list)

    def test_tolerates_runtime_error_dir_changed(self, project_env, monkeypatch):
        """RuntimeError ('directory changed during iteration') is also
        absorbed — same defensive scope."""
        project, _data_dir, db = project_env
        (project / "src").mkdir(parents=True, exist_ok=True)

        original_rglob = Path.rglob

        def boom(self, *args, **kwargs):
            if str(self).endswith("/src"):
                raise RuntimeError("directory changed during iteration")
            return original_rglob(self, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", boom)
        result = _get_changed_files(db)
        assert isinstance(result, list)

    def test_isolates_one_bad_watch_dir(self, project_env, monkeypatch):
        """When two watch_dirs are configured and one fails, the other still
        produces results. Models the parallel-thread isolation pattern from
        log analysis (microsecond-spaced parallel-thread crashes)."""
        import yaml

        project, data_dir, db = project_env
        # Reconfigure watched_dirs to include both 'src' and 'lib'
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        cfg["project"]["watched_dirs"] = ["src", "lib"]
        (data_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "lib").mkdir(parents=True, exist_ok=True)
        (project / "lib" / "ok.py").write_text("a=1\n")

        original_rglob = Path.rglob

        def boom_on_src(self, *args, **kwargs):
            if str(self).endswith("/src"):
                raise OSError("permission denied")
            return original_rglob(self, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", boom_on_src)
        result = _get_changed_files(db)
        rel_paths = [rel for rel, _h in result]
        # The good watch_dir 'lib' produced its file even though 'src' raised.
        assert "lib/ok.py" in rel_paths


# ---------------------------------------------------------------------------
# _get_requested_files
# ---------------------------------------------------------------------------


class TestGetRequestedFiles:
    """Validate requested file paths, filter by extension, compute hashes."""

    def test_valid_relative_path(self, project_env):
        """A relative path to an existing .py file is accepted."""
        project, _data_dir, _db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f = src_dir / "module.py"
        f.write_text("a = 1\n")

        result = _get_requested_files(["src/module.py"])
        assert len(result) == 1
        assert result[0][0] == "src/module.py"

    def test_nonexistent_file_skipped(self, project_env):
        """A path that does not exist is silently skipped."""
        _project, _data_dir, _db = project_env
        result = _get_requested_files(["src/nonexistent.py"])
        assert result == []

    def test_wrong_extension_skipped(self, project_env):
        """A file with a non-matching extension is skipped."""
        project, _data_dir, _db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "readme.md").write_text("# hello")

        result = _get_requested_files(["src/readme.md"])
        assert result == []

    def test_deduplication(self, project_env):
        """The same file specified twice only appears once."""
        project, _data_dir, _db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "dup.py").write_text("b = 2\n")

        result = _get_requested_files(["src/dup.py", "src/dup.py"])
        assert len(result) == 1

    def test_absolute_path(self, project_env):
        """An absolute path that resolves inside the project is accepted."""
        project, _data_dir, _db = project_env

        src_dir = project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f = src_dir / "abs.py"
        f.write_text("c = 3\n")

        result = _get_requested_files([str(f)])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_indexing_status
# ---------------------------------------------------------------------------


class TestGetIndexingStatus:
    """Return current background indexing progress dict."""

    def test_default_idle(self):
        status = get_indexing_status()
        assert status["status"] == "idle"
        assert status["files_indexed"] == 0
        assert status["total_files"] == 0

    def test_reflects_global_state(self):
        with idx_mod._bg_lock:
            idx_mod._bg_status = "running"
            idx_mod._bg_files_indexed = 5
            idx_mod._bg_total_files = 10

        status = get_indexing_status()
        assert status["status"] == "running"
        assert status["files_indexed"] == 5
        assert status["total_files"] == 10


# ---------------------------------------------------------------------------
# start_background_full_index
# ---------------------------------------------------------------------------


class TestStartBackgroundFullIndex:
    """Start a full index rebuild in a background daemon thread."""

    @pytest.fixture(autouse=True)
    def _reset_chroma_write_lock(self):
        """``start_background_full_index`` acquires the module-level
        ``_chroma_write_lock`` before calling ``cmd_full_rebuild``. If a
        prior test in this session leaked the lock (held it without
        releasing), the background thread blocks indefinitely and the
        mock never gets called → ``assert_called_once`` fails after 5s
        join timeout. Force-release here so this test is robust to
        upstream pollution."""
        if idx_mod._chroma_write_lock.locked():
            try:
                idx_mod._chroma_write_lock.release()
            except RuntimeError:
                # Lock held by another thread — replace it
                idx_mod._chroma_write_lock = threading.Lock()
        yield

    def test_starts_daemon_thread(self):
        """The thread is started and is a daemon."""
        with patch.object(idx_mod, "cmd_full_rebuild") as mock_rebuild:
            t = start_background_full_index()
            t.join(timeout=5)
            assert t.daemon is True
            assert t.name == "codevira-bg-index"
            mock_rebuild.assert_called_once()

    def test_status_transitions_to_done(self):
        """After successful rebuild, status is 'done'."""
        with patch.object(idx_mod, "cmd_full_rebuild"):
            t = start_background_full_index()
            t.join(timeout=5)

        status = get_indexing_status()
        assert status["status"] == "done"

    def test_status_transitions_to_error_on_failure(self):
        """If cmd_full_rebuild raises, status becomes 'error'."""
        with patch.object(
            idx_mod, "cmd_full_rebuild", side_effect=RuntimeError("boom")
        ):
            t = start_background_full_index()
            t.join(timeout=5)

        status = get_indexing_status()
        assert status["status"] == "error"

    def test_callback_invoked_on_success(self):
        """Optional callback receives the final status string."""
        callback = MagicMock()
        with patch.object(idx_mod, "cmd_full_rebuild"):
            t = start_background_full_index(callback=callback)
            t.join(timeout=5)

        callback.assert_called_once_with("done")

    def test_callback_invoked_on_error(self):
        """Callback is called with 'error' when rebuild fails."""
        callback = MagicMock()
        with patch.object(
            idx_mod, "cmd_full_rebuild", side_effect=RuntimeError("fail")
        ):
            t = start_background_full_index(callback=callback)
            t.join(timeout=5)

        callback.assert_called_once_with("error")

    def test_rebuild_is_invoked_quiet(self):
        """The background thread must ask cmd_full_rebuild for SILENCE.

        This thread runs inside the MCP server process, where sys.stdout is
        the JSON-RPC transport — any write from here lands mid-protocol.
        ``start_background_watcher`` already passes ``quiet=True`` into
        ``cmd_incremental``; this is the missing half of that pair.
        """
        with patch.object(idx_mod, "cmd_full_rebuild") as mock_rebuild:
            t = start_background_full_index()
            t.join(timeout=5)

        mock_rebuild.assert_called_once_with(quiet=True)

    def test_background_rebuild_writes_nothing_to_stdout(self, project_env, capsys):
        """End-to-end: a background rebuild leaves stdout untouched.

        Regression for the 2026-08-01 CI failure. The real
        ``cmd_full_rebuild`` printed ``✓ Graph built: N nodes, M edges.`` via
        a stdout rich Console. On a daemon thread that line escapes into
        whatever is reading stdout at the time — in CI it prefixed an
        unrelated test's captured output and broke its ``json.loads``; in a
        live stdio client it corrupts the JSON-RPC stream.
        """
        with patch(
            "indexer.index_codebase._check_search_deps", return_value=False
        ), patch(
            "indexer.graph_generator.generate_graph_sqlite",
            return_value={"nodes_total": 304, "edges_added": 0},
        ), patch("indexer.index_codebase.SQLiteGraph"):
            t = start_background_full_index()
            t.join(timeout=10)
            assert not t.is_alive(), "background index thread did not finish"

        assert get_indexing_status()["status"] == "done"
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# cmd_full_rebuild
# ---------------------------------------------------------------------------


class TestCmdFullRebuild:
    """4.0.1: the companion ``test_with_chromadb_indexes_chunks`` is gone — it
    forced ``_check_search_deps`` True to reach a branch the real function
    cannot take. Graph-only is the only path, so it is the only path tested.
    """

    def test_builds_graph_only(self, project_env):
        """cmd_full_rebuild builds the graph and closes the db."""
        _project, data_dir, _db = project_env
        mock_result = {"nodes_total": 5, "edges_added": 3}
        with patch(
            "indexer.graph_generator.generate_graph_sqlite", return_value=mock_result
        ) as mock_graph, patch("indexer.index_codebase.SQLiteGraph") as mock_db_cls:
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            from indexer.index_codebase import cmd_full_rebuild

            cmd_full_rebuild()
        mock_graph.assert_called_once()
        # full=True is what makes `index --full` a real rebuild rather than
        # an incremental top-up reporting "0 new nodes".
        assert mock_graph.call_args.kwargs.get("full") is True
        mock_db.close.assert_called_once()


class TestVerboseActuallyPrints:
    """`--verbose` reaches cmd_full_rebuild / cmd_incremental as a parameter
    both functions accepted and ignored. Wiring the collector is only half the
    fix — the flag is only honoured once a user can SEE the decisions."""

    def _decisions(self):
        return {
            "nodes_total": 1,
            "edges_added": 0,
            "decisions": [
                {
                    "path": "notes.txt",
                    "verdict": "skipped",
                    "reason": "unsupported extension: .txt",
                },
                {
                    "path": "app/main.py",
                    "verdict": "indexed",
                    "reason": "added as a file node",
                },
            ],
        }

    def test_full_rebuild_verbose_prints_each_decision(self, project_env, capsys):
        with patch(
            "indexer.graph_generator.generate_graph_sqlite",
            return_value=self._decisions(),
        ) as mock_graph, patch("indexer.index_codebase.SQLiteGraph"):
            from indexer.index_codebase import cmd_full_rebuild

            cmd_full_rebuild(verbose=True)

        assert (
            mock_graph.call_args.kwargs.get("collect_decisions") is True
        ), "the walker only records decisions when asked"
        out = capsys.readouterr().out
        assert "notes.txt" in out and "unsupported extension" in out
        assert "app/main.py" in out

    def test_without_verbose_nothing_extra_is_printed(self, project_env, capsys):
        with patch(
            "indexer.graph_generator.generate_graph_sqlite",
            return_value=self._decisions(),
        ) as mock_graph, patch("indexer.index_codebase.SQLiteGraph"):
            from indexer.index_codebase import cmd_full_rebuild

            cmd_full_rebuild()

        assert not mock_graph.call_args.kwargs.get("collect_decisions")
        assert "notes.txt" not in capsys.readouterr().out

    def test_quiet_wins_over_verbose_for_the_background_thread(
        self, project_env, capsys
    ):
        """quiet=True exists because start_background_full_index runs this on a
        daemon thread where stdout IS the JSON-RPC transport. A verbose print
        there would corrupt a live stdio client, so quiet must dominate."""
        with patch(
            "indexer.graph_generator.generate_graph_sqlite",
            return_value=self._decisions(),
        ), patch("indexer.index_codebase.SQLiteGraph"):
            from indexer.index_codebase import cmd_full_rebuild

            cmd_full_rebuild(verbose=True, quiet=True)

        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# cmd_incremental
# ---------------------------------------------------------------------------


class TestCmdIncremental:
    def test_no_changed_files_returns_zero(self, project_env):
        _project, data_dir, db = project_env
        with patch("indexer.index_codebase._get_changed_files", return_value=[]), patch(
            "indexer.index_codebase.SQLiteGraph", return_value=db
        ):
            from indexer.index_codebase import cmd_incremental

            result = cmd_incremental()
        assert result == 0

    def test_explicit_files_no_match_returns_zero(self, project_env):
        _project, data_dir, db = project_env
        # file_paths given but no actual matching files exist
        with patch(
            "indexer.index_codebase._get_requested_files", return_value=[]
        ), patch("indexer.index_codebase.SQLiteGraph", return_value=db):
            from indexer.index_codebase import cmd_incremental

            result = cmd_incremental(file_paths=["nonexistent.py"])
        assert result == 0


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatusIndexCb:
    def test_cmd_status_shows_panel(self, project_env, capsys):
        _project, data_dir, db = project_env
        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._get_changed_files", return_value=[]
        ):
            from indexer.index_codebase import cmd_status

            cmd_status()  # Should not raise


class TestCheckSearchDepsIsCheap:
    """2026-05-17 Bug J/K fix (P9 graceful degradation): _check_search_deps
    must NOT import sentence_transformers (which triggers ~5s of PyTorch
    tensor init). list_tools() in server.py calls _check_search_deps on
    every MCP request — slow import here caused Claude Desktop renderer
    timeouts and silent connection drops. That call site is still live, so
    these guards stay.

    4.0.1: the class was ``TestIncrementalCircuitBreakerP5`` and also held
    ``test_per_file_chroma_failures_halt_after_limit``, covering the
    cmd_incremental per-file Chroma circuit breaker. The breaker sat inside
    ``if collection is not None:`` — unreachable since v2.2.0 — and is
    deleted, so the test is too.
    """

    def test_check_search_deps_is_fast(self):
        """Sanity check: 100 calls complete in well under 1 second."""
        from indexer.index_codebase import _check_search_deps

        start = time.time()
        for _ in range(100):
            _check_search_deps()
        elapsed = time.time() - start
        # Generous: 100 calls in 0.5s = 5ms each. The actual import-based
        # implementation would have been 5s * 100 = 500 seconds — this
        # test would have taken longer than pytest's default timeout.
        assert elapsed < 0.5, (
            f"Bug J/K regression: _check_search_deps took {elapsed:.2f}s "
            f"for 100 calls — likely re-introduced a heavy import."
        )

    def test_check_search_deps_uses_find_spec_not_import(self):
        """Behavioral check: function must complete in <50ms even when the
        modules are NOT pre-loaded in sys.modules. The old import-based
        approach took ~5s in that case (cold PyTorch init).

        This is a behavioral guard, not a static-source check — static
        source checks are brittle against well-meaning refactors that
        keep the contract but rephrase the implementation.
        """
        import sys
        from indexer.index_codebase import _check_search_deps

        # Save & clear sys.modules entries so find_spec actually runs.
        # If we left them in sys.modules, the fast `sys.modules.get` path
        # would short-circuit and we wouldn't be measuring find_spec.
        saved = {}
        for module_name in ("chromadb", "sentence_transformers"):
            if module_name in sys.modules:
                saved[module_name] = sys.modules.pop(module_name)
        try:
            start = time.time()
            for _ in range(50):
                _check_search_deps()
            elapsed = time.time() - start
            # 50 calls in 0.5s = 10ms each. Old import-based path would
            # have been 5s × 50 = 250 seconds (test would have hung).
            assert elapsed < 0.5, (
                f"Bug J/K regression: 50 calls to _check_search_deps "
                f"took {elapsed:.2f}s — likely re-introduced the slow "
                f"ML-import path. Critical for Claude Desktop tools/list."
            )
        finally:
            # Restore — don't pollute the test session.
            for k, v in saved.items():
                sys.modules[k] = v


class TestCmdIndexVerboseBugH:
    """2026-05-17 Bug H fix (P10): `--verbose` on `codevira index` must
    emit per-file decisions so users can diagnose silent 0-chunks results.
    """

    def test_verbose_signature_accepted(self):
        """cmd_full_rebuild + cmd_incremental must accept verbose kwarg."""
        from indexer.index_codebase import cmd_full_rebuild, cmd_incremental
        import inspect

        full_sig = inspect.signature(cmd_full_rebuild)
        inc_sig = inspect.signature(cmd_incremental)
        assert "verbose" in full_sig.parameters, "cmd_full_rebuild needs verbose"
        assert "verbose" in inc_sig.parameters, "cmd_incremental needs verbose"
        # Default must be False (preserves silent behavior for git hook).
        assert full_sig.parameters["verbose"].default is False
        assert inc_sig.parameters["verbose"].default is False


class TestCmdStatusBugC:
    """2026-05-17 Bug C fix (P1 + P10): `codevira status` was showing
    'Graph Nodes: 0 / ChromaDB Chunks: 0' with no actionable signal
    when the graph existed but was empty. Now distinguishes three states.
    """

    def test_state2_graph_empty_config_matches_warns_run_full(
        self, project_env, capsys
    ):
        """State 2: graph empty + project HAS matching files.
        Status must warn AND suggest `codevira index --full`."""
        project, data_dir, db = project_env
        # Create a Python file matching the fixture's config (watched_dirs=src, ext=.py).
        src = project / "src"
        src.mkdir()
        (src / "main.py").write_text("x = 1\n")

        # Force chroma.sqlite3 to exist so the chunk-count path runs.
        index_dir = data_dir / "codeindex"
        index_dir.mkdir()
        (index_dir / "chroma.sqlite3").write_text("")

        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._index_dir", return_value=index_dir
        ), patch("indexer.index_codebase._get_changed_files", return_value=[]):
            from indexer.index_codebase import cmd_status

            cmd_status()

        out = capsys.readouterr().out
        # Must NOT lie about the state.
        assert "Graph Nodes" in out and "0" in out
        # Must emit actionable guidance (the fix).
        assert "index --full" in out, (
            f"Bug C regression: state 2 (config matches files, graph empty) "
            f"must suggest `index --full`. Got:\n{out}"
        )
        # And NOT the misconfiguration hint — config IS fine.
        assert (
            "matches NO files" not in out
        ), f"State 2 must NOT fire the misconfig hint. Got:\n{out}"

    def test_state3_graph_empty_config_matches_nothing_warns_configure(
        self, project_env, capsys
    ):
        """State 3: graph empty + config matches NO files on disk.
        Status must point user at `codevira configure`."""
        project, data_dir, db = project_env
        # Don't create any .py files — the fixture's config (src/*.py)
        # will match nothing.

        index_dir = data_dir / "codeindex"
        index_dir.mkdir()
        (index_dir / "chroma.sqlite3").write_text("")

        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._index_dir", return_value=index_dir
        ), patch("indexer.index_codebase._get_changed_files", return_value=[]):
            from indexer.index_codebase import cmd_status

            cmd_status()

        out = capsys.readouterr().out
        assert "Graph Nodes" in out and "0" in out
        # Must point at configure — config is broken.
        assert "configure" in out.lower(), (
            f"Bug C regression: state 3 (config matches nothing) "
            f"must suggest `codevira configure`. Got:\n{out}"
        )

    def test_state1_graph_populated_no_warning(self, populated_db, capsys):
        """State 1: graph has nodes — no warning, just show the table.
        Verifies my fix doesn't add false warnings on healthy projects."""
        project, data_dir, db = populated_db

        index_dir = data_dir / "codeindex"
        index_dir.mkdir()
        (index_dir / "chroma.sqlite3").write_text("")

        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._index_dir", return_value=index_dir
        ), patch("indexer.index_codebase._get_changed_files", return_value=[]):
            from indexer.index_codebase import cmd_status

            cmd_status()

        out = capsys.readouterr().out
        # State 1 must NOT emit either of the warnings.
        assert (
            "index --full" not in out
        ), f"State 1 (graph populated) must NOT suggest --full. Got:\n{out}"
        assert (
            "matches NO files" not in out
        ), f"State 1 must NOT fire misconfig hint. Got:\n{out}"


class TestGlobalStatusRendersRealNumbers:
    """Regression guard for Bug 19 (rc.4 dogfood, 2026-05-13) — adapted
    for v3.0.0.

    The original bug: `codevira status --global` rendered "Projects
    Tracked: 0 / Global Preferences: 0 / Global Rules: 0" even on
    heavily-indexed projects because the renderer read wrong keys.

    v3.0.0 (2026-05-22 surface-cut audit): the "Global Preferences"
    and "Global Rules" rows were REMOVED — they always read zero
    after the preferences / learned-rules MCP tools were deleted
    in the audit. The "Projects Tracked" row stays. The original
    keys-mismatch bug regression-guard now lives on that single row.
    """

    def test_projects_tracked_reads_from_inventory_not_stats(self, project_env, capsys):
        """v3.0.0: "Projects Tracked" reads from the canonical project
        inventory (`mcp_server._project_inventory`), not from any
        global-stats dict. Mock the inventory and confirm the row
        renders the right number."""
        _project, _data_dir, db = project_env
        inv_summary = {
            "tracked": 7,
            "ghost": 0,
            "orphan": 0,
            "stale": 0,
            "total": 7,
        }
        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._get_changed_files", return_value=[]
        ), patch(
            "mcp_server._project_inventory.enumerate_projects", return_value=[]
        ), patch("mcp_server._project_inventory.summarize", return_value=inv_summary):
            from indexer.index_codebase import cmd_status

            cmd_status(show_global=True)
        out = capsys.readouterr().out
        assert (
            "Projects Tracked" in out and " 7 " in out
        ), f"Expected 'Projects Tracked: 7 tracked', got:\n{out}"
        # v3.0.0: the Global Preferences + Global Rules rows are gone.
        # If they ever reappear (regression), surface it loudly.
        assert "Global Preferences" not in out, (
            "v3.0.0 audit removed the Global Preferences row — its "
            "reappearance is a regression of the 2026-05-22 surface cut."
        )
        assert "Global Rules" not in out, (
            "v3.0.0 audit removed the Global Rules row — its reappearance "
            "is a regression of the 2026-05-22 surface cut."
        )

    def test_global_status_handles_inventory_failure_gracefully(
        self, project_env, capsys
    ):
        """When the inventory helper raises, fall back to showing an
        error row rather than crashing the status command."""
        _project, _data_dir, db = project_env
        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), patch(
            "indexer.index_codebase._get_changed_files", return_value=[]
        ), patch(
            "mcp_server._project_inventory.enumerate_projects",
            side_effect=RuntimeError("inventory fail"),
        ):
            from indexer.index_codebase import cmd_status

            cmd_status(show_global=True)
        out = capsys.readouterr().out
        assert "Project Inventory" in out
        assert "inventory fail" in out


# ---------------------------------------------------------------------------
# cmd_generate_graph
# ---------------------------------------------------------------------------


class TestCmdGenerateGraph:
    def test_generates_graph(self, project_env):
        _project, data_dir, _db = project_env
        mock_result = {"files_processed": 5, "nodes_added": 10, "nodes_skipped": 2}
        # generate_graph_sqlite is imported locally inside cmd_generate_graph
        with patch(
            "indexer.graph_generator.generate_graph_sqlite", return_value=mock_result
        ) as mock_gen, patch(
            "indexer.index_codebase.get_data_dir", return_value=data_dir
        ), patch("indexer.index_codebase.get_project_root", return_value=_project):
            from indexer.index_codebase import cmd_generate_graph

            cmd_generate_graph()  # Should not raise
        mock_gen.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_bootstrap_roadmap
# ---------------------------------------------------------------------------


class TestCmdBootstrapRoadmap:
    def test_creates_roadmap_if_not_exists(self, project_env):
        _project, data_dir, _db = project_env
        # generate_roadmap_stub is imported locally inside cmd_bootstrap_roadmap
        with patch("indexer.graph_generator.generate_roadmap_stub") as mock_stub, patch(
            "indexer.index_codebase.get_data_dir", return_value=data_dir
        ), patch("indexer.index_codebase.get_project_root", return_value=_project):
            from indexer.index_codebase import cmd_bootstrap_roadmap

            cmd_bootstrap_roadmap()
        mock_stub.assert_called_once()

    def test_skips_if_roadmap_exists(self, project_env, capsys):
        _project, data_dir, _db = project_env
        roadmap_file = data_dir / "roadmap.yaml"
        roadmap_file.write_text("phases: []")
        with patch("indexer.graph_generator.generate_roadmap_stub") as mock_stub, patch(
            "indexer.index_codebase.get_data_dir", return_value=data_dir
        ):
            from indexer.index_codebase import cmd_bootstrap_roadmap

            cmd_bootstrap_roadmap()
        mock_stub.assert_not_called()


# ---------------------------------------------------------------------------
# start_background_watcher
# ---------------------------------------------------------------------------


class TestStartBackgroundWatcher:
    def test_watcher_starts_with_valid_dirs(self, project_env):
        _project, data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": []}
        with patch("indexer.index_codebase._load_config", return_value=config), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("watchdog.observers.Observer") as mock_observer_cls:
            mock_observer = MagicMock()
            mock_observer_cls.return_value = mock_observer
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)
        mock_observer.start.assert_called_once()

    def test_watcher_does_not_start_for_missing_dirs(self, project_env):
        _project, _data_dir, _db = project_env
        config = {
            "watched_dirs": ["nonexistent"],
            "file_extensions": [".py"],
            "skip_dirs": [],
        }
        with patch("indexer.index_codebase._load_config", return_value=config), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("watchdog.observers.Observer") as mock_observer_cls:
            mock_observer = MagicMock()
            mock_observer_cls.return_value = mock_observer
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)
        mock_observer.start.assert_not_called()

    # v1.8.1 round-3 hardening: start_background_watcher refuses $HOME.
    # This is the defense-in-depth guard. Even if all upstream entry-point
    # guards (server.main, run_http_server, cmd_serve) somehow miss, the
    # watcher itself cannot start with an invalid project root.
    def test_watcher_refuses_home_root(self, tmp_path, monkeypatch):
        """Watcher returns None and never schedules an Observer when
        project_root is $HOME."""
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        monkeypatch.setattr(
            "indexer.index_codebase.get_project_root", lambda: fake_home
        )

        with patch("watchdog.observers.Observer") as mock_observer_cls:
            from indexer.index_codebase import start_background_watcher

            result = start_background_watcher(quiet=True)

        assert result is None
        # Observer was never even constructed.
        mock_observer_cls.assert_not_called()

    def test_watcher_refuses_root_slash(self, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(
            "indexer.index_codebase.get_project_root", lambda: Path("/")
        )

        with patch("watchdog.observers.Observer") as mock_observer_cls:
            from indexer.index_codebase import start_background_watcher

            result = start_background_watcher(quiet=True)

        assert result is None
        mock_observer_cls.assert_not_called()


# ===================================================================
# v1.8.1 round-4 hardening: `python -m indexer.index_codebase` __main__
# ===================================================================


class TestIndexerMainEntry:
    """Direct module invocation (`python -m indexer.index_codebase --full`)
    bypasses the cli.cmd_index guard. v1.8.1 round-4 adds a parallel guard
    at the __main__ block. We verify by running the module as a subprocess."""

    def _project_root(self) -> Path:
        """Return the agent-mcp project root (where pyproject.toml lives)."""
        return Path(__file__).parent.parent

    def test_main_refuses_home_for_full(self, tmp_path):
        """`python -m indexer.index_codebase --full` from $HOME exits 1.
        The subprocess inherits PYTHONPATH so it can find the module."""
        import subprocess
        import sys

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        # PYTHONPATH so subprocess can import indexer / mcp_server modules.
        proj_root = self._project_root()
        env["PYTHONPATH"] = str(proj_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "indexer.index_codebase", "--full"],
            capture_output=True,
            text=True,
            cwd=str(fake_home),
            env=env,
            timeout=30,
        )
        assert result.returncode == 1, (
            f"Expected exit 1 from $HOME, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "$HOME" in result.stderr or "is not a project" in result.stderr

    def test_main_status_works_anywhere(self, tmp_path):
        """`--status` is exempt from the guard (read-only, bails early on
        missing graph.db). From $HOME with no initialized project, it
        prints 'Not initialized' and exits 0."""
        import subprocess
        import sys

        fake_home = tmp_path / "fake-home-status"
        fake_home.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        proj_root = self._project_root()
        env["PYTHONPATH"] = str(proj_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "indexer.index_codebase", "--status"],
            capture_output=True,
            text=True,
            cwd=str(fake_home),
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"--status from $HOME should be 0, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Not initialized" in result.stdout


# ---------------------------------------------------------------------------
# cmd_incremental — actual indexing loop (changed files)
# ---------------------------------------------------------------------------


class TestCmdIncrementalLoop:
    """cmd_incremental — the per-changed-file loop.

    4.0.1: previously asserted the ChromaDB delete/add calls (and a companion
    ``test_chunk_error_continues_to_next_file`` around ``chunk_file``). Both
    are gone with that path; what survives — and is what the loop actually
    does — is stamping the new hash and regenerating the graph.
    """

    def test_indexes_changed_file_successfully(self, project_env):
        """cmd_incremental processes changed files and updates the hash."""
        _project, data_dir, db = project_env

        src = _project / "src"
        src.mkdir(exist_ok=True)
        (src / "api.py").write_text("def hello(): pass")

        # cmd_incremental closes the db before returning, so spy on the write
        # rather than reading the hash back afterwards.
        with _mock_rich(), patch(
            "indexer.index_codebase._get_changed_files",
            return_value=[("src/api.py", "newhash123")],
        ), patch(
            "indexer.graph_generator.generate_graph_sqlite", return_value={}
        ) as mock_graph, patch(
            "indexer.index_codebase.SQLiteGraph", return_value=db
        ), patch.object(
            db, "update_file_hash", wraps=db.update_file_hash
        ) as mock_update:
            from indexer.index_codebase import cmd_incremental

            result = cmd_incremental()
        assert result == 0
        mock_update.assert_called_once_with("src/api.py", "newhash123")
        mock_graph.assert_called_once()


# ---------------------------------------------------------------------------
# DebouncedHandler event methods (via start_background_watcher)
# ---------------------------------------------------------------------------


class TestDebouncedHandlerEvents:
    """DebouncedHandler event methods (lines 358-396, 406-411).

    watchdog is an optional runtime dependency not present in the CI test
    environment, so we inject fake modules into sys.modules before every test
    that triggers the `from watchdog.observers import Observer` import path
    inside start_background_watcher.
    """

    @staticmethod
    def _fake_watchdog_mods():
        """Return a dict of fake watchdog sub-modules for patch.dict."""
        import sys
        import types

        mods = {}
        for name in ("watchdog", "watchdog.observers", "watchdog.events"):
            if name not in sys.modules:
                mods[name] = types.ModuleType(name)
        # Observer must be a class
        mock_observer_cls = MagicMock()
        mods.get(
            "watchdog.observers", sys.modules.get("watchdog.observers")
        ).Observer = mock_observer_cls

        # FileSystemEventHandler must be a real class (DebouncedHandler inherits it)
        class _FakeHandler:
            pass

        mods.get(
            "watchdog.events", sys.modules.get("watchdog.events")
        ).FileSystemEventHandler = _FakeHandler
        return mods

    def _start_watcher_and_get_handler(self, project_env):
        """Inject watchdog stubs, start watcher, return (src_dir, handler, timer_cls)."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }

        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()
        # v3.0.0 cleanup: previously kept a `mock_timer = MagicMock()`
        # local that nothing referenced. The real timer mock is the
        # `mock_timer_cls` patch context manager below — that's what
        # the assertion actually inspects.

        # Patch watchdog modules into sys.modules so the local import inside
        # start_background_watcher succeeds, then patch the Observer class
        # and threading.Timer to intercept calls.
        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            # Override the Observer class inside the fake watchdog.observers mod
            fake_obs_mod = sys.modules["watchdog.observers"]
            fake_obs_mod.Observer = MagicMock(return_value=mock_obs)

            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            handler = (
                mock_obs.schedule.call_args[0][0] if mock_obs.schedule.called else None
            )
            return src_dir, handler, mock_timer_cls, mock_obs

    def test_on_modified_triggers_schedule(self, project_env):
        """DebouncedHandler.on_modified calls _schedule_reindex for .py files."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(
                return_value=mock_obs
            )
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            assert mock_obs.schedule.called
            handler = mock_obs.schedule.call_args[0][0]
            mock_event = MagicMock()
            mock_event.is_directory = False
            mock_event.src_path = str(src_dir / "app.py")
            handler.on_modified(mock_event)

        mock_timer_cls.assert_called()

    def test_on_created_triggers_schedule(self, project_env):
        """DebouncedHandler.on_created calls _schedule_reindex for .py files."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(
                return_value=mock_obs
            )
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            assert mock_obs.schedule.called
            handler = mock_obs.schedule.call_args[0][0]
            mock_event = MagicMock()
            mock_event.is_directory = False
            mock_event.src_path = str(src_dir / "new.py")
            handler.on_created(mock_event)

        mock_timer_cls.assert_called()

    def test_on_deleted_triggers_schedule(self, project_env):
        """DebouncedHandler.on_deleted calls _schedule_reindex for .py files."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(
                return_value=mock_obs
            )
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            assert mock_obs.schedule.called
            handler = mock_obs.schedule.call_args[0][0]
            mock_event = MagicMock()
            mock_event.is_directory = False
            mock_event.src_path = str(src_dir / "removed.py")
            handler.on_deleted(mock_event)

        mock_timer_cls.assert_called()

    def test_directory_event_ignored(self, project_env):
        """Directory events should not trigger reindex."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(
                return_value=mock_obs
            )
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            assert mock_obs.schedule.called
            handler = mock_obs.schedule.call_args[0][0]
            mock_timer_cls.reset_mock()

            mock_event = MagicMock()
            mock_event.is_directory = True  # directory event — should be skipped
            mock_event.src_path = str(src_dir)
            handler.on_modified(mock_event)

        mock_timer_cls.assert_not_called()

    def test_wrong_extension_not_scheduled(self, project_env):
        """Files with non-matching extension don't trigger reindex."""
        import sys

        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
            "skip_dirs": ["__pycache__"],
        }
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), patch(
            "indexer.index_codebase._load_config", return_value=config
        ), patch(
            "indexer.index_codebase.get_project_root", return_value=_project
        ), patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(
                return_value=mock_obs
            )
            from indexer.index_codebase import start_background_watcher

            start_background_watcher(quiet=True)

            assert mock_obs.schedule.called
            handler = mock_obs.schedule.call_args[0][0]
            mock_timer_cls.reset_mock()

            mock_event = MagicMock()
            mock_event.is_directory = False
            mock_event.src_path = str(src_dir / "README.md")  # not .py
            handler.on_modified(mock_event)

        mock_timer_cls.assert_not_called()


# ---------------------------------------------------------------------------
# start_background_full_index — callback behaviour (lines 481-488)
# ---------------------------------------------------------------------------


class TestBackgroundFullIndexCallback:
    def test_callback_called_on_success(self, project_env):
        """start_background_full_index calls callback with 'done' on success."""
        callback_results = []

        with patch("indexer.index_codebase.cmd_full_rebuild"):
            from indexer.index_codebase import start_background_full_index

            t = start_background_full_index(callback=callback_results.append)
            t.join(timeout=5.0)

        assert callback_results == ["done"]

    def test_callback_called_on_error(self, project_env):
        """start_background_full_index calls callback with 'error' on exception."""
        callback_results = []

        with patch(
            "indexer.index_codebase.cmd_full_rebuild",
            side_effect=RuntimeError("rebuild failed"),
        ):
            from indexer.index_codebase import start_background_full_index

            t = start_background_full_index(callback=callback_results.append)
            t.join(timeout=5.0)

        assert callback_results == ["error"]

    def test_callback_exception_does_not_crash(self, project_env):
        """If the callback itself raises, the background thread still completes."""

        def bad_callback(status):
            raise RuntimeError("callback crashed")

        with patch("indexer.index_codebase.cmd_full_rebuild"):
            from indexer.index_codebase import start_background_full_index

            t = start_background_full_index(callback=bad_callback)
            t.join(timeout=5.0)
        # No unhandled exception — thread finished cleanly


# ---------------------------------------------------------------------------
# cmd_status — stale file display (lines 530-535)
# ---------------------------------------------------------------------------


class TestCmdStatusStaleFiles:
    """cmd_status stale file display (lines 530-535).

    Renders through mocked rich primitives (see ``_mock_rich``) so the
    assertions don't depend on terminal width or ANSI styling.
    """

    def test_cmd_status_shows_stale_files(self, project_env):
        """cmd_status prints stale file list when files need reindexing."""
        _project, data_dir, db = project_env
        stale = [(f"src/file_{i}.py", f"hash{i}") for i in range(3)]

        with _mock_rich(), patch(
            "indexer.index_codebase.SQLiteGraph", return_value=db
        ), patch("indexer.index_codebase._get_changed_files", return_value=stale):
            from indexer.index_codebase import cmd_status

            cmd_status(check_stale=True)  # opt-in to stale check

    def test_cmd_status_many_stale_files_truncated(self, project_env):
        """cmd_status truncates stale file list after 10 items."""
        _project, data_dir, db = project_env
        # 15 stale files — display first 10 then "and N more"
        stale = [(f"src/file_{i}.py", f"hash{i}") for i in range(15)]

        with _mock_rich(), patch(
            "indexer.index_codebase.SQLiteGraph", return_value=db
        ), patch("indexer.index_codebase._get_changed_files", return_value=stale):
            from indexer.index_codebase import cmd_status

            cmd_status(check_stale=True)  # opt-in to stale check


class TestRichMockingIsReversible:
    """Regression guard (2026-08-02) — the rich mocks must not outlive the
    ``with`` block.

    The old helpers assigned ``Console``/``Table``/``Panel`` onto the real,
    already-imported rich modules and relied on ``patch.dict(sys.modules, ...)``
    to undo it. It can't: it restores the mapping, never an in-place attribute
    mutation on a module it didn't create. Every later test in the process then
    rendered through a MagicMock, which is what made 5 tests here and in
    ``test_doctor.py`` order-dependent (green in collection order, red under
    ``-p randomly``).

    Reproduce the pre-fix failures with::

        pytest tests/test_index_codebase.py -p randomly --randomly-seed=1
    """

    def test_mock_rich_restores_the_real_classes(self):
        import rich.console
        import rich.panel
        import rich.table

        before = (rich.console.Console, rich.table.Table, rich.panel.Panel)
        with _mock_rich():
            assert isinstance(rich.console.Console, MagicMock)
            assert isinstance(rich.table.Table, MagicMock)
            assert isinstance(rich.panel.Panel, MagicMock)
        assert (rich.console.Console, rich.table.Table, rich.panel.Panel) == before

    def test_cmd_status_renders_through_real_rich_after_the_stale_tests(
        self, project_env, capsys
    ):
        """Runs the mocking tests' own scenario, then asserts an unmocked
        ``cmd_status`` still prints a real table. Pre-fix this saw
        ``<MagicMock id=...>`` where the table should be."""
        _project, _data_dir, db = project_env

        def _patches():
            return (
                patch("indexer.index_codebase.SQLiteGraph", return_value=db),
                patch("indexer.index_codebase._get_changed_files", return_value=[]),
            )

        from indexer.index_codebase import cmd_status

        with _mock_rich(), contextlib.ExitStack() as stack:
            for cm in _patches():
                stack.enter_context(cm)
            cmd_status()
        capsys.readouterr()  # discard the mocked run's (empty) output

        with contextlib.ExitStack() as stack:
            for cm in _patches():
                stack.enter_context(cm)
            cmd_status()
        out = capsys.readouterr().out
        assert "Graph Nodes" in out, f"rich stayed mocked; got:\n{out}"
        assert "MagicMock" not in out, f"rich stayed mocked; got:\n{out}"


# ============================================================================
# v1.8: Zero-chunks safety hint — _warn_zero_chunks + _any_files_match +
# integration with cmd_incremental.
# ============================================================================


class TestWarnZeroChunks:
    """Unit tests for the dual stdout + logger helper."""

    def test_fires_on_stderr_when_not_quiet(self, capsys):
        """Hint must go to STDERR, never stdout — stdout is the MCP wire."""
        from indexer.index_codebase import _warn_zero_chunks

        _warn_zero_chunks(["src"], [".py"], quiet=False)
        captured = capsys.readouterr()
        assert captured.out == "", "stdout must stay empty to protect MCP stdio"
        assert "No files matched" in captured.err
        assert "codevira configure" in captured.err
        assert "src" in captured.err
        assert ".py" in captured.err

    def test_silent_on_stderr_when_quiet(self, capsys):
        from indexer.index_codebase import _warn_zero_chunks

        _warn_zero_chunks(["src"], [".py"], quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_also_logged_regardless_of_quiet(self, caplog):
        from indexer.index_codebase import _warn_zero_chunks
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="indexer.index_codebase"):
            _warn_zero_chunks(["src"], [".py"], quiet=True)
        messages = [r.getMessage() for r in caplog.records]
        assert any("No files matched" in m for m in messages)
        assert any("codevira configure" in m for m in messages)


class TestAnyFilesMatch:
    """Unit tests for the project-wide presence check used by cmd_incremental."""

    def test_returns_true_when_matching_file_exists(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        (project / "src" / "a.py").write_text("x=1")
        monkeypatch.setattr("indexer.index_codebase._project_root", lambda: project)
        from indexer.index_codebase import _any_files_match

        assert _any_files_match(["src"], [".py"], ["node_modules"]) is True

    def test_returns_false_when_no_matches(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        (project / "src" / "a.txt").write_text("x=1")  # wrong extension
        monkeypatch.setattr("indexer.index_codebase._project_root", lambda: project)
        from indexer.index_codebase import _any_files_match

        assert _any_files_match(["src"], [".py"], ["node_modules"]) is False

    def test_returns_false_when_dir_missing(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr("indexer.index_codebase._project_root", lambda: project)
        from indexer.index_codebase import _any_files_match

        assert _any_files_match(["src"], [".py"], ["node_modules"]) is False

    def test_skip_dirs_suppresses_match(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        (project / "src" / "vendor").mkdir(parents=True)
        (project / "src" / "vendor" / "lib.py").write_text("x=1")
        monkeypatch.setattr("indexer.index_codebase._project_root", lambda: project)
        from indexer.index_codebase import _any_files_match

        assert _any_files_match(["src"], [".py"], ["vendor"]) is False


class TestCmdIncrementalHint:
    """Verify the hint fires from cmd_incremental ONLY for a project-wide scan
    that matches nothing — not for caller-scoped incremental or for
    files-exist-but-unchanged."""

    def _run(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        files,
        config,
        file_paths=None,
        quiet=False,
        patch_changed=None,
    ):
        import yaml as _yaml

        project = tmp_path / "proj"
        (project / ".codevira" / "graph").mkdir(parents=True)
        (project / ".codevira" / "config.yaml").write_text(
            _yaml.safe_dump({"project": config})
        )
        for rel, content in files.items():
            p = project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        monkeypatch.setattr("indexer.index_codebase._project_root", lambda: project)
        monkeypatch.setattr(
            "indexer.index_codebase.get_data_dir",
            lambda: project / ".codevira",
        )
        from mcp_server import paths as _paths

        _paths._data_dir_cache.clear()

        patches = [
            patch("indexer.index_codebase._check_search_deps", return_value=False),
            patch("indexer.graph_generator.generate_graph_sqlite"),
        ]
        if patch_changed is not None:
            patches.append(
                patch(
                    "indexer.index_codebase._get_changed_files",
                    return_value=patch_changed,
                )
            )

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            from indexer.index_codebase import cmd_incremental

            cmd_incremental(quiet=quiet, file_paths=file_paths)
        return capsys.readouterr()

    def test_hint_fires_on_project_wide_zero_matches(
        self, tmp_path, monkeypatch, capsys, caplog
    ):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="indexer.index_codebase"):
            captured = self._run(
                tmp_path,
                monkeypatch,
                capsys,
                files={"README.md": "hi"},  # no .py files in src/
                config={
                    "watched_dirs": ["src"],
                    "file_extensions": [".py"],
                    "skip_dirs": [],
                },
                file_paths=None,
            )
        log_msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "No files matched" in m for m in log_msgs
        ), f"expected warning log; got {log_msgs}"
        # Hint goes to stderr (NOT stdout — stdout is the MCP wire in stdio mode).
        assert "codevira configure" in captured.err
        assert "codevira configure" not in captured.out, "hint must not leak to stdout"

    def test_hint_NOT_fired_when_file_paths_given(
        self, tmp_path, monkeypatch, capsys, caplog
    ):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="indexer.index_codebase"):
            captured = self._run(
                tmp_path,
                monkeypatch,
                capsys,
                files={"src/a.py": "x=1"},
                config={
                    "watched_dirs": ["src"],
                    "file_extensions": [".py"],
                    "skip_dirs": [],
                },
                file_paths=["nonexistent.py"],  # caller-scoped, empty result
            )
        log_msgs = [r.getMessage() for r in caplog.records]
        assert not any(
            "No files matched" in m for m in log_msgs
        ), f"hint should not fire; got {log_msgs}"
        assert "codevira configure" not in captured.out
        assert "codevira configure" not in captured.err

    def test_hint_NOT_fired_when_files_exist_but_unchanged_graph_empty(
        self, tmp_path, monkeypatch, capsys, caplog
    ):
        """2026-05-17 Bug B fix: when files exist, nothing changed, AND
        the graph is empty, the 'misconfiguration' warning should NOT
        fire (config is fine) — but neither should we lie about being
        'up to date.' Correct response: warn that graph is empty + tell
        user to run --full.
        """
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="indexer.index_codebase"):
            captured = self._run(
                tmp_path,
                monkeypatch,
                capsys,
                files={"src/a.py": "x=1"},
                config={
                    "watched_dirs": ["src"],
                    "file_extensions": [".py"],
                    "skip_dirs": [],
                },
                file_paths=None,
                patch_changed=[],  # force changed=[] — files exist but nothing stale
            )
        log_msgs = [r.getMessage() for r in caplog.records]
        # Config IS fine — no misconfiguration warning should fire.
        assert not any(
            "No files matched" in m for m in log_msgs
        ), f"misconfig hint should not fire — config matches files; got {log_msgs}"
        # But the graph is empty (no add_node calls), so the truthful
        # message is "graph has 0 nodes" + remediation, NOT "up to date".
        out = captured.out
        assert "0 nodes" in out or "not been indexed" in out or "index --full" in out, (
            f"Bug B regression: graph-empty state should be surfaced honestly. "
            f"Got: {out!r}"
        )

    def test_up_to_date_fires_when_graph_populated_and_unchanged(
        self, tmp_path, monkeypatch, capsys, caplog
    ):
        """2026-05-17 Bug B fix: legitimate "Index is up to date" path —
        graph has content AND nothing changed. This is the truthful
        steady-state. Verifies my graph-empty check doesn't suppress
        the message when it's actually correct.
        """
        import logging as _logging

        # Monkeypatch count_nodes to return >0, simulating a populated graph.
        from indexer import sqlite_graph

        original_count = sqlite_graph.SQLiteGraph.count_nodes
        monkeypatch.setattr(
            sqlite_graph.SQLiteGraph,
            "count_nodes",
            lambda self, kind=None: 42,  # pretend we have 42 indexed nodes
        )
        try:
            with caplog.at_level(_logging.WARNING, logger="indexer.index_codebase"):
                captured = self._run(
                    tmp_path,
                    monkeypatch,
                    capsys,
                    files={"src/a.py": "x=1"},
                    config={
                        "watched_dirs": ["src"],
                        "file_extensions": [".py"],
                        "skip_dirs": [],
                    },
                    file_paths=None,
                    patch_changed=[],
                )
        finally:
            monkeypatch.setattr(sqlite_graph.SQLiteGraph, "count_nodes", original_count)
        log_msgs = [r.getMessage() for r in caplog.records]
        assert not any(
            "No files matched" in m for m in log_msgs
        ), f"misconfig hint should not fire when config matches; got {log_msgs}"
        assert "Index is up to date" in captured.out, (
            f"Expected truthful 'up to date' message when graph is populated. "
            f"Got: {captured.out!r}"
        )


class TestStatusRichCounts:
    """`codevira status` surfaces activity-bearing counts (3.5.1): Symbols and
    Edges move as you edit; Decisions track codevira usage — so the panel isn't
    just a flat file count. Tests the data helper directly (rendering-agnostic,
    so it's immune to the rich-mock leakage other tests in this file create)."""

    def test_entity_counts_include_symbols_edges_decisions(self, tmp_path, monkeypatch):
        import indexer.index_codebase as ic
        from indexer.graph_generator import generate_graph_sqlite
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server import paths

        proj = tmp_path / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / ".codevira" / "graph").mkdir(parents=True)
        (proj / "src" / "a.py").write_text("def f():\n    return 1\n")
        monkeypatch.setattr(ic, "_project_root", lambda: proj)
        monkeypatch.setattr(ic, "get_data_dir", lambda: proj / ".codevira")
        paths._data_dir_cache.clear()
        db_path = proj / ".codevira" / "graph" / "graph.db"
        generate_graph_sqlite(str(proj), str(db_path))

        counts = ic._status_entity_counts(SQLiteGraph(db_path))
        assert set(counts) == {"nodes", "symbols", "edges", "decisions"}
        assert all(isinstance(v, int) for v in counts.values())
        assert counts["nodes"] >= 1  # src/a.py is a file node
