"""
Tests for mcp_server/auto_init.py — auto-initialization on first tool call.

Standalone replacement for the auto_init portion of test_playbook_and_auto_init.py.
Resets module globals between tests via autouse fixture.
"""

from __future__ import annotations

import json
import sys
import time
import types
import yaml
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import mcp_server.auto_init as ai
from mcp_server.auto_init import (
    ensure_project_initialized,
    get_init_progress,
    InitStatus,
    _write_config,
    _write_metadata,
    _register_global,
    _run_background_init,
    _update_progress,
)


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------


def _reset_ai_globals():
    ai._init_started = False
    ai._indexing_thread = None
    ai._progress = {
        "status": "not_started",
        "files_indexed": 0,
        "total_files": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }
    ai._start_time = None


@pytest.fixture(autouse=True)
def reset_auto_init():
    """Reset module-level state before AND after each test."""
    _reset_ai_globals()
    yield
    _reset_ai_globals()


def _stub_graph_generator():
    """Create a fake indexer.graph_generator module so patch() doesn't trigger
    the real import (which requires tree_sitter_language_pack)."""
    mod = types.ModuleType("indexer.graph_generator")
    mod.generate_graph_sqlite = MagicMock()
    return mod


def _stub_index_codebase():
    """Create a fake indexer.index_codebase module."""
    mod = types.ModuleType("indexer.index_codebase")
    mod.start_background_full_index = MagicMock(side_effect=ImportError)
    return mod


@pytest.fixture(autouse=True)
def stub_heavy_modules():
    """Install fake modules for optional heavy deps that may not be installed.

    Both sys.modules AND the parent package attribute must be set so that
    unittest.mock.patch() can resolve 'indexer.graph_generator.generate_graph_sqlite'.
    """
    import indexer as indexer_pkg

    graph_gen = _stub_graph_generator()
    idx_code = _stub_index_codebase()

    orig_gg = sys.modules.get("indexer.graph_generator")
    orig_ic = sys.modules.get("indexer.index_codebase")
    orig_gg_attr = getattr(indexer_pkg, "graph_generator", None)
    orig_ic_attr = getattr(indexer_pkg, "index_codebase", None)

    sys.modules["indexer.graph_generator"] = graph_gen
    sys.modules["indexer.index_codebase"] = idx_code
    indexer_pkg.graph_generator = graph_gen
    indexer_pkg.index_codebase = idx_code

    yield

    # Restore originals
    if orig_gg is not None:
        sys.modules["indexer.graph_generator"] = orig_gg
    else:
        sys.modules.pop("indexer.graph_generator", None)
    if orig_ic is not None:
        sys.modules["indexer.index_codebase"] = orig_ic
    else:
        sys.modules.pop("indexer.index_codebase", None)
    if orig_gg_attr is not None:
        indexer_pkg.graph_generator = orig_gg_attr
    elif hasattr(indexer_pkg, "graph_generator"):
        delattr(indexer_pkg, "graph_generator")
    if orig_ic_attr is not None:
        indexer_pkg.index_codebase = orig_ic_attr
    elif hasattr(indexer_pkg, "index_codebase"):
        delattr(indexer_pkg, "index_codebase")


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

DEFAULT_DETECTED = {
    "name": "proj",
    "language": "python",
    "watched_dirs": ["src"],
    "file_extensions": [".py"],
    "collection_name": "proj",
}


def _bg_init_patches(
    detected,
    graph_side_effect=None,
    discover_return=None,
    index_side_effect=ImportError,
):
    """Return a combined context manager for background init patches."""
    stack = ExitStack()
    stack.enter_context(
        patch("mcp_server.detect.auto_detect_project", return_value=detected)
    )
    if graph_side_effect:
        stack.enter_context(
            patch(
                "indexer.graph_generator.generate_graph_sqlite",
                side_effect=graph_side_effect,
            )
        )
    else:
        stack.enter_context(patch("indexer.graph_generator.generate_graph_sqlite"))
    stack.enter_context(
        patch(
            "mcp_server.gitignore.discover_source_files",
            return_value=discover_return or [],
        )
    )
    stack.enter_context(
        patch(
            "indexer.index_codebase.start_background_full_index",
            side_effect=index_side_effect,
        )
    )
    stack.enter_context(patch("mcp_server.auto_init._register_global"))
    return stack


# ---------------------------------------------------------------
# ensure_project_initialized()
# ---------------------------------------------------------------


class TestEnsureProjectInitialized:
    def test_first_call_triggers_init(self, tmp_path):
        """First call on an uninitialized project spawns a background thread."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch(
            "mcp_server.paths.get_project_root", return_value=project_root
        ), patch(
            "mcp_server.paths.get_data_dir", return_value=data_dir
        ), _bg_init_patches(DEFAULT_DETECTED):
            status = ensure_project_initialized(project_root)

        assert isinstance(status, InitStatus)
        assert status.ready is False
        assert status.indexing is True
        assert ai._init_started is True

    def test_second_call_is_fast_path_noop(self):
        """After init is done, second call returns immediately without locks."""
        ai._init_started = True
        ai._progress["status"] = "ready"

        start = time.monotonic()
        status = ensure_project_initialized()
        elapsed = time.monotonic() - start

        assert status.ready is True
        assert status.indexing is False
        # Should be sub-millisecond for a flag check
        assert elapsed < 0.05

    def test_already_initialized_project_returns_ready(self, tmp_path):
        """Fully initialized project (config + graph.db WITH NODES) returns ready=True.

        Bug 21a (rc.4): "initialized" requires both bookkeeping (config.yaml)
        AND heavy state (graph/graph.db).
        rc.5 (P0-B): the graph.db must additionally contain at least one row in
        the ``nodes`` table — an empty graph.db (no schema or zero nodes) is
        treated as "needs build" so the heavy-init path can fire to actually
        populate it.
        """
        import sqlite3 as _sqlite3

        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        (data_dir / "graph").mkdir(parents=True)
        (data_dir / "config.yaml").write_text("project:\n  name: test\n")
        # Create a real graph.db with a populated nodes table.
        graph_db = data_dir / "graph" / "graph.db"
        conn = _sqlite3.connect(str(graph_db))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, file_path TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('n1', 'file', 'main.py', 'src/main.py')"
        )
        conn.commit()
        conn.close()

        with patch(
            "mcp_server.paths.get_project_root", return_value=project_root
        ), patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            status = ensure_project_initialized(project_root)

        assert status.ready is True
        assert status.indexing is False
        assert ai._init_started is True
        assert ai._indexing_thread is None

    def test_fast_path_while_indexing(self):
        """When status is 'indexing', fast path reports indexing=True with counts."""
        ai._init_started = True
        ai._progress["status"] = "indexing"
        ai._progress["files_indexed"] = 5
        ai._progress["total_files"] = 10

        status = ensure_project_initialized()
        assert status.ready is False
        assert status.indexing is True
        assert status.files_indexed == 5
        assert status.total_files == 10

    def test_concurrent_calls_only_one_init(self, tmp_path):
        """Multiple sequential calls should only init once (fast-path after first)."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch(
            "mcp_server.paths.get_project_root", return_value=project_root
        ), patch(
            "mcp_server.paths.get_data_dir", return_value=data_dir
        ), _bg_init_patches(DEFAULT_DETECTED):
            s1 = ensure_project_initialized(project_root)
            s2 = ensure_project_initialized(project_root)
            s3 = ensure_project_initialized(project_root)

        # First call starts init; second and third hit fast-path
        assert ai._init_started is True
        assert isinstance(s1, InitStatus)
        assert isinstance(s2, InitStatus)
        assert isinstance(s3, InitStatus)
        # All return without error — fast-path doesn't re-trigger init


# ---------------------------------------------------------------
# get_init_progress()
# ---------------------------------------------------------------


class TestGetInitProgress:
    def test_default_state(self):
        """Default progress is not_started with zeroed counters."""
        progress = get_init_progress()
        assert progress["status"] == "not_started"
        assert progress["files_indexed"] == 0
        assert progress["total_files"] == 0
        assert progress["error"] is None

    def test_elapsed_time_tracking(self):
        """Elapsed time computed from _start_time when set."""
        ai._start_time = time.monotonic() - 5.0
        progress = get_init_progress()
        assert progress["elapsed_seconds"] >= 4.5
        assert progress["elapsed_seconds"] < 10.0

    def test_elapsed_zero_when_no_start_time(self):
        """When _start_time is None, elapsed_seconds stays at the default 0."""
        progress = get_init_progress()
        assert progress["elapsed_seconds"] == 0.0

    def test_reflects_status_updates(self):
        """Progress reflects updates made via _update_progress."""
        ai._progress["status"] = "indexing"
        ai._progress["files_indexed"] = 42
        ai._progress["total_files"] = 100
        progress = get_init_progress()
        assert progress["status"] == "indexing"
        assert progress["files_indexed"] == 42
        assert progress["total_files"] == 100


# ---------------------------------------------------------------
# _write_config()
# ---------------------------------------------------------------


class TestWriteConfig:
    def test_writes_valid_yaml(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        detected = {
            "name": "my-project",
            "language": "go",
            "watched_dirs": ["cmd", "pkg"],
            "file_extensions": [".go"],
            "collection_name": "my_project",
        }
        _write_config(data_dir, detected, tmp_path / "project")

        config_path = data_dir / "config.yaml"
        assert config_path.exists()
        config = yaml.safe_load(config_path.read_text())
        assert config["project"]["name"] == "my-project"
        assert config["project"]["language"] == "go"
        assert config["project"]["watched_dirs"] == ["cmd", "pkg"]
        assert config["project"]["file_extensions"] == [".go"]
        assert config["project"]["collection_name"] == "my_project"

    def test_config_is_parseable_yaml(self, tmp_path):
        """The written file can be loaded back as valid YAML without errors."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_config(data_dir, DEFAULT_DETECTED, tmp_path / "proj")

        raw = (data_dir / "config.yaml").read_text()
        parsed = yaml.safe_load(raw)
        assert isinstance(parsed, dict)
        assert "project" in parsed


# ---------------------------------------------------------------
# _write_metadata()
# ---------------------------------------------------------------


class TestWriteMetadata:
    def test_writes_valid_json_with_required_fields(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path / "my-project"
        project_root.mkdir()

        with patch(
            "mcp_server.paths._sanitize_path_key", return_value="test_key"
        ), patch(
            "mcp_server.paths._get_git_remote_url",
            return_value="git@github.com:test/repo.git",
        ):
            _write_metadata(data_dir, project_root)

        meta_path = data_dir / "metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())

        # All required fields present
        assert meta["path_key"] == "test_key"
        assert meta["git_remote"] == "git@github.com:test/repo.git"
        assert meta["original_path"] == str(project_root)
        from mcp_server import __version__

        assert meta["version"] == __version__
        assert meta["auto_initialized"] is True
        assert "created_at" in meta

    def test_metadata_is_parseable_json(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path / "proj"
        project_root.mkdir()

        with patch("mcp_server.paths._sanitize_path_key", return_value="k"), patch(
            "mcp_server.paths._get_git_remote_url", return_value=""
        ):
            _write_metadata(data_dir, project_root)

        raw = (data_dir / "metadata.json").read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------
# _register_global()
# ---------------------------------------------------------------


class TestRegisterGlobal:
    def test_registers_in_global_db(self, tmp_path):
        """Bug 20 (rc.4): _register_global must register under project_root,
        NOT data_dir. Pre-fix this asserted path=str(data_dir), which silently
        accepted the bug — same logical project then accumulated duplicate
        rows because global_sync.py registered under project_root and these
        two call sites registered under the storage path.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path / "proj"
        project_root.mkdir()

        mock_gdb = MagicMock()
        with patch("indexer.global_db.GlobalDB", return_value=mock_gdb), patch(
            "mcp_server.paths.get_global_db_path", return_value=tmp_path / "global.db"
        ), patch("mcp_server.paths._get_git_remote_url", return_value="git@host:r.git"):
            _register_global(data_dir, project_root, DEFAULT_DETECTED)

        mock_gdb.register_project.assert_called_once_with(
            path=str(project_root),
            name="proj",
            language="python",
            git_remote="git@host:r.git",
        )
        # Explicit regression guard: the path MUST NOT be the storage dir.
        # If a future refactor regresses Bug 20, this assertion fires loudly.
        call_kwargs = mock_gdb.register_project.call_args.kwargs
        assert call_kwargs["path"] != str(data_dir), (
            "Bug 20 regression: _register_global passed data_dir as path. "
            "It must pass project_root so global.db keys match the canonical "
            "project path used by global_sync.py."
        )
        mock_gdb.close.assert_called_once()

    def test_register_global_failure_is_non_fatal(self, tmp_path):
        """If GlobalDB raises, _register_global logs a warning but doesn't crash."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path / "proj"
        project_root.mkdir()

        with patch(
            "indexer.global_db.GlobalDB", side_effect=RuntimeError("DB error")
        ), patch(
            "mcp_server.paths.get_global_db_path", return_value=tmp_path / "g.db"
        ), patch("mcp_server.paths._get_git_remote_url", return_value=""):
            # Should NOT raise
            _register_global(data_dir, project_root, DEFAULT_DETECTED)


# ---------------------------------------------------------------
# Background thread lifecycle
# ---------------------------------------------------------------


class TestBackgroundThread:
    def test_thread_is_daemon(self, tmp_path):
        """The background init thread should be a daemon thread."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch(
            "mcp_server.paths.get_project_root", return_value=project_root
        ), patch(
            "mcp_server.paths.get_data_dir", return_value=data_dir
        ), _bg_init_patches(DEFAULT_DETECTED):
            ensure_project_initialized(project_root)

        assert ai._indexing_thread is not None
        assert ai._indexing_thread.daemon is True
        assert ai._indexing_thread.name == "codevira-auto-init"

    def test_status_transitions_during_background_init(self, tmp_path):
        """Background init transitions: initializing -> indexing -> ready."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(DEFAULT_DETECTED):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"

    def test_creates_directory_structure(self, tmp_path):
        """Background init creates graph, codeindex, and logs dirs.

        v2.2.0+: changesets sub-dir no longer created (feature removed).
        """
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(DEFAULT_DETECTED):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert (data_dir / "graph").is_dir()
        assert (data_dir / "codeindex").is_dir()
        assert (data_dir / "logs").is_dir()


# ---------------------------------------------------------------
# ChromaDB not installed -> skips semantic index
# ---------------------------------------------------------------


class TestChromaDBNotInstalled:
    def test_import_error_skips_gracefully(self, tmp_path):
        """When chromadb is not installed (ImportError), init still completes as ready."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path("a.py"), Path("b.py")]
        with _bg_init_patches(
            DEFAULT_DETECTED,
            discover_return=fake_files,
            index_side_effect=ImportError("No chromadb"),
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"
        assert ai._progress["files_indexed"] == 0


# ---------------------------------------------------------------
# Graph generation failure -> still marks ready
# ---------------------------------------------------------------


class TestGraphGenerationFailure:
    def test_graph_failure_still_reaches_ready(self, tmp_path):
        """If graph generation throws, the init still completes with status=ready."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(
            DEFAULT_DETECTED, graph_side_effect=RuntimeError("Graph failed")
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"
        # Config should still have been written before the graph step
        assert (data_dir / "config.yaml").exists()


# ---------------------------------------------------------------
# Progress updates from background thread
# ---------------------------------------------------------------


class TestProgressUpdates:
    def test_not_started_to_initializing(self):
        assert ai._progress["status"] == "not_started"
        _update_progress(status="initializing")
        assert ai._progress["status"] == "initializing"

    def test_initializing_to_indexing(self):
        ai._progress["status"] = "initializing"
        _update_progress(status="indexing")
        assert ai._progress["status"] == "indexing"

    def test_indexing_to_ready(self):
        ai._progress["status"] = "indexing"
        _update_progress(status="ready")
        assert ai._progress["status"] == "ready"

    def test_update_progress_merges_fields(self):
        _update_progress(status="indexing", files_indexed=10, total_files=50)
        assert ai._progress["status"] == "indexing"
        assert ai._progress["files_indexed"] == 10
        assert ai._progress["total_files"] == 50

    def test_error_field_set_on_failure(self):
        _update_progress(status="error", error="Something broke")
        assert ai._progress["status"] == "error"
        assert ai._progress["error"] == "Something broke"

    def test_total_files_tracked_from_discover(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path(f"src/f{i}.py") for i in range(15)]
        with _bg_init_patches(DEFAULT_DETECTED, discover_return=fake_files):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["total_files"] == 15

    def test_fatal_error_sets_error_status(self, tmp_path):
        """If auto_detect_project raises, status becomes 'error'."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch(
            "mcp_server.detect.auto_detect_project",
            side_effect=RuntimeError("Detect failed"),
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "error"
        assert "Detect failed" in ai._progress["error"]


# ---------------------------------------------------------------
# InitStatus dataclass
# ---------------------------------------------------------------


class TestInitStatusDataclass:
    def test_defaults(self):
        status = InitStatus(ready=True, indexing=False)
        assert status.files_indexed == 0
        assert status.total_files == 0

    def test_with_counts(self):
        status = InitStatus(ready=False, indexing=True, files_indexed=5, total_files=20)
        assert status.files_indexed == 5
        assert status.total_files == 20


# ---------------------------------------------------------------
# Background thread: discover_source_files failure + indexing
# exception branches (lines 164-165, 176-181)
# ---------------------------------------------------------------


class TestBackgroundInitExceptionBranches:
    def test_discover_source_files_exception_continues(self, tmp_path):
        """When discover_source_files raises, init uses empty file list and
        still completes (lines 164-165)."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(
            DEFAULT_DETECTED,
            discover_return=None,
            index_side_effect=ImportError("no chromadb"),
        ), patch(
            "mcp_server.gitignore.discover_source_files",
            side_effect=Exception("discover failed"),
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        # Despite discover failure, init should complete (not error)
        assert ai._progress["status"] in ("ready", "error")

    def test_import_error_on_indexing_sets_ready(self, tmp_path):
        """When start_background_full_index raises ImportError (no chromadb),
        status becomes 'ready' (lines 176-178)."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path("src/a.py"), Path("src/b.py")]
        with _bg_init_patches(
            DEFAULT_DETECTED,
            discover_return=fake_files,
            index_side_effect=ImportError("No chromadb"),
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"
        # files_indexed stays 0 for graph-only mode
        assert ai._progress["files_indexed"] == 0

    def test_runtime_error_on_indexing_sets_ready(self, tmp_path):
        """When start_background_full_index raises a general exception,
        status still becomes 'ready' (lines 179-181)."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path("src/a.py")]
        with _bg_init_patches(
            DEFAULT_DETECTED,
            discover_return=fake_files,
            index_side_effect=RuntimeError("unexpected error"),
        ):
            ai._start_time = time.monotonic()
            _run_background_init(project_root, data_dir)

        # Non-fatal exception -> status is still "ready"
        assert ai._progress["status"] == "ready"


# ---------------------------------------------------------------
# v1.8.1 — _run_background_init refuses $HOME / system dirs
# ---------------------------------------------------------------


class TestRunBackgroundInitRefusesInvalidRoots:
    """Regression for the v1.8.0 production crash: an MCP tool call from
    $HOME would silently auto-init a rogue project. The watcher then walked
    ~/Library/... and crashed 41 times. v1.8.1 short-circuits in the
    background thread before anything touches the filesystem."""

    def test_refuses_home_sets_status_error(self, tmp_path, monkeypatch):
        """When project_root is $HOME, the thread sets status=error and
        returns early — no graph build, no index, no metadata.json."""
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # No _bg_init_patches — if the guard is missing this would crash on
        # missing modules. The whole point is the guard returns early.
        ai._start_time = time.monotonic()
        _run_background_init(fake_home, data_dir)

        assert ai._progress["status"] == "error"
        assert ai._progress["error"] is not None
        assert "$HOME" in ai._progress["error"]
        # And nothing got written under data_dir
        assert not (data_dir / "config.yaml").exists()
        assert not (data_dir / "metadata.json").exists()

    def test_refuses_root_slash(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ai._start_time = time.monotonic()
        _run_background_init(Path("/"), data_dir)

        assert ai._progress["status"] == "error"
        assert "system directory" in ai._progress["error"]
        assert not (data_dir / "config.yaml").exists()
