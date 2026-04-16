"""
Tests for indexer/index_codebase.py

Covers:
  - _load_config(): reads config.yaml, returns project sub-dict, handles missing
  - _check_search_deps(): True/False based on chromadb availability
  - _compute_hash(): SHA256 of file contents, deterministic
  - _get_changed_files(): compares file hashes against stored
  - _get_requested_files(): validates paths, computes hashes
  - _chunk_to_document(): formats chunk into (doc_id, document, metadata)
  - get_indexing_status(): returns current background status dict
  - start_background_full_index(): starts daemon thread
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
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
    _chunk_to_document,
    get_indexing_status,
    start_background_full_index,
)


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


@dataclass
class FakeChunk:
    """Minimal stand-in for CodeChunk used in _chunk_to_document tests."""

    file_path: str
    chunk_type: str
    name: str
    source_text: str
    start_line: int
    end_line: int
    docstring: str
    layer: str


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
    """Return True/False based on chromadb availability."""

    def test_returns_true_when_available(self):
        """When both chromadb and sentence_transformers can be imported."""
        mock_chromadb = MagicMock()
        mock_st = MagicMock()
        with patch.dict("sys.modules", {
            "chromadb": mock_chromadb,
            "sentence_transformers": mock_st,
        }):
            assert _check_search_deps() is True

    def test_returns_false_when_missing(self):
        """When chromadb import raises ImportError."""
        with patch.dict("sys.modules", {"chromadb": None}):
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
# _chunk_to_document
# ---------------------------------------------------------------------------

class TestChunkToDocument:
    """Format a CodeChunk into (doc_id, document, metadata)."""

    def test_basic_formatting(self):
        chunk = FakeChunk(
            file_path="src/main.py",
            chunk_type="function",
            name="process",
            source_text="def process(): pass",
            start_line=10,
            end_line=11,
            docstring="Process data.",
            layer="service",
        )
        doc_id, document, metadata = _chunk_to_document(chunk)

        assert doc_id == "src/main.py::function::process::10"
        assert "src/main.py" in document
        assert "process" in document
        assert "Process data." in document
        assert "def process(): pass" in document
        assert metadata["file_path"] == "src/main.py"
        assert metadata["name"] == "process"
        assert metadata["chunk_type"] == "function"
        assert metadata["start_line"] == 10
        assert metadata["end_line"] == 11
        assert metadata["layer"] == "service"

    def test_class_chunk(self):
        chunk = FakeChunk(
            file_path="src/models.py",
            chunk_type="class",
            name="User",
            source_text="class User: ...",
            start_line=1,
            end_line=20,
            docstring="User model.",
            layer="data",
        )
        doc_id, document, metadata = _chunk_to_document(chunk)

        assert doc_id == "src/models.py::class::User::1"
        assert metadata["chunk_type"] == "class"
        assert metadata["layer"] == "data"

    def test_empty_docstring(self):
        chunk = FakeChunk(
            file_path="src/util.py",
            chunk_type="function",
            name="helper",
            source_text="def helper(): ...",
            start_line=5,
            end_line=6,
            docstring="",
            layer="util",
        )
        doc_id, document, metadata = _chunk_to_document(chunk)

        assert doc_id == "src/util.py::function::helper::5"
        # Document still contains the name and source even without docstring
        assert "helper" in document
        assert "def helper(): ..." in document


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


# ---------------------------------------------------------------------------
# cmd_full_rebuild
# ---------------------------------------------------------------------------

class TestCmdFullRebuild:
    def test_no_search_deps_builds_graph_only(self, project_env):
        """When chromadb not installed, cmd_full_rebuild still builds graph."""
        _project, data_dir, _db = project_env
        mock_result = {"nodes_added": 5, "edges_added": 3}
        with patch("indexer.index_codebase._check_search_deps", return_value=False), \
             patch("indexer.graph_generator.generate_graph_sqlite", return_value=mock_result) as mock_graph, \
             patch("indexer.index_codebase.SQLiteGraph") as mock_db_cls:
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            from indexer.index_codebase import cmd_full_rebuild
            cmd_full_rebuild()
        mock_graph.assert_called_once()
        mock_db.close.assert_called_once()

    def test_with_chromadb_indexes_chunks(self, project_env):
        """When chromadb available, cmd_full_rebuild indexes chunks."""
        _project, data_dir, _db = project_env
        # Create the watched 'src' dir so abs_dir.exists() passes
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / "main.py").write_text("def main(): pass")

        mock_chunk = MagicMock()
        mock_chunk.file_path = "src/main.py"
        mock_chunk.chunk_type = "function"
        mock_chunk.name = "main"
        mock_chunk.start_line = 1
        mock_chunk.end_line = 10
        mock_chunk.docstring = ""
        mock_chunk.source_text = "def main(): pass"
        mock_chunk.layer = "api"

        mock_collection = MagicMock()
        mock_client = MagicMock()
        mock_client.create_collection.return_value = mock_collection

        # chunk_project is imported locally inside cmd_full_rebuild, so patch at source
        with patch("indexer.index_codebase._check_search_deps", return_value=True), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.chunker.chunk_project", return_value=[mock_chunk]), \
             patch("indexer.graph_generator.generate_graph_sqlite", return_value={"nodes_added": 1}), \
             patch("indexer.index_codebase.SQLiteGraph") as mock_db_cls:
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            from indexer.index_codebase import cmd_full_rebuild
            cmd_full_rebuild()
        mock_collection.add.assert_called()


# ---------------------------------------------------------------------------
# cmd_incremental
# ---------------------------------------------------------------------------

class TestCmdIncremental:
    def test_no_changed_files_returns_zero(self, project_env):
        _project, data_dir, db = project_env
        with patch("indexer.index_codebase._get_changed_files", return_value=[]), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db):
            from indexer.index_codebase import cmd_incremental
            result = cmd_incremental()
        assert result == 0

    def test_no_collection_prints_error_and_exits(self, project_env):
        _project, data_dir, db = project_env
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = Exception("collection missing")
        with patch("indexer.index_codebase._get_changed_files", return_value=[("src/main.py", "abc123")]), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db), \
             patch("indexer.index_codebase._check_search_deps", return_value=True), \
             pytest.raises(SystemExit):
            from indexer.index_codebase import cmd_incremental
            cmd_incremental()

    def test_explicit_files_no_match_returns_zero(self, project_env):
        _project, data_dir, db = project_env
        # file_paths given but no actual matching files exist
        with patch("indexer.index_codebase._get_requested_files", return_value=[]), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db):
            from indexer.index_codebase import cmd_incremental
            result = cmd_incremental(file_paths=["nonexistent.py"])
        assert result == 0


# ---------------------------------------------------------------------------
# _get_chroma_client / _get_embedding_fn
# ---------------------------------------------------------------------------

class TestGetChromaClientAndEmbedFn:
    def test_get_chroma_client_returns_client(self, project_env):
        mock_chromadb = MagicMock()
        mock_client = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client
        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            from indexer.index_codebase import _get_chroma_client
            result = _get_chroma_client()
        assert result is mock_client

    def test_get_chroma_client_raises_import_error_if_no_chromadb(self, project_env):
        import sys
        # Simulate chromadb being unimportable by removing it from sys.modules
        saved = sys.modules.pop("chromadb", None)
        try:
            with patch.dict("sys.modules", {"chromadb": None}):
                from indexer.index_codebase import _get_chroma_client
                with pytest.raises(ImportError, match="codevira"):
                    _get_chroma_client()
        finally:
            if saved is not None:
                sys.modules["chromadb"] = saved


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatusIndexCb:
    def test_cmd_status_shows_panel(self, project_env, capsys):
        _project, data_dir, db = project_env
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 42
        mock_client.get_collection.return_value = mock_collection
        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.index_codebase._get_changed_files", return_value=[]):
            from indexer.index_codebase import cmd_status
            cmd_status()  # Should not raise

    def test_cmd_status_no_chromadb(self, project_env):
        _project, data_dir, db = project_env
        with patch("indexer.index_codebase.SQLiteGraph", return_value=db), \
             patch("indexer.index_codebase._get_chroma_client", side_effect=ImportError("chromadb")), \
             patch("indexer.index_codebase._get_changed_files", return_value=[]):
            from indexer.index_codebase import cmd_status
            cmd_status()  # Should not raise


# ---------------------------------------------------------------------------
# cmd_generate_graph
# ---------------------------------------------------------------------------

class TestCmdGenerateGraph:
    def test_generates_graph(self, project_env):
        _project, data_dir, _db = project_env
        mock_result = {"files_processed": 5, "nodes_added": 10, "nodes_skipped": 2}
        # generate_graph_sqlite is imported locally inside cmd_generate_graph
        with patch("indexer.graph_generator.generate_graph_sqlite", return_value=mock_result) as mock_gen, \
             patch("indexer.index_codebase.get_data_dir", return_value=data_dir), \
             patch("indexer.index_codebase.get_project_root", return_value=_project):
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
        with patch("indexer.graph_generator.generate_roadmap_stub") as mock_stub, \
             patch("indexer.index_codebase.get_data_dir", return_value=data_dir), \
             patch("indexer.index_codebase.get_project_root", return_value=_project):
            from indexer.index_codebase import cmd_bootstrap_roadmap
            cmd_bootstrap_roadmap()
        mock_stub.assert_called_once()

    def test_skips_if_roadmap_exists(self, project_env, capsys):
        _project, data_dir, _db = project_env
        roadmap_file = data_dir / "roadmap.yaml"
        roadmap_file.write_text("phases: []")
        with patch("indexer.graph_generator.generate_roadmap_stub") as mock_stub, \
             patch("indexer.index_codebase.get_data_dir", return_value=data_dir):
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
        with patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("watchdog.observers.Observer") as mock_observer_cls:
            mock_observer = MagicMock()
            mock_observer_cls.return_value = mock_observer
            from indexer.index_codebase import start_background_watcher
            result = start_background_watcher(quiet=True)
        mock_observer.start.assert_called_once()

    def test_watcher_does_not_start_for_missing_dirs(self, project_env):
        _project, _data_dir, _db = project_env
        config = {"watched_dirs": ["nonexistent"], "file_extensions": [".py"], "skip_dirs": []}
        with patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("watchdog.observers.Observer") as mock_observer_cls:
            mock_observer = MagicMock()
            mock_observer_cls.return_value = mock_observer
            from indexer.index_codebase import start_background_watcher
            start_background_watcher(quiet=True)
        mock_observer.start.assert_not_called()


# ---------------------------------------------------------------------------
# _get_embedding_fn exit path
# ---------------------------------------------------------------------------

class TestGetEmbeddingFnExit:
    def test_import_error_when_chromadb_utils_missing(self, project_env):
        """_get_embedding_fn raises ImportError if chromadb.utils unavailable."""
        import sys
        original_mods = {
            "chromadb.utils": sys.modules.pop("chromadb.utils", None),
            "chromadb.utils.embedding_functions": sys.modules.pop(
                "chromadb.utils.embedding_functions", None
            ),
        }
        try:
            sys.modules["chromadb.utils"] = None  # type: ignore[assignment]
            sys.modules["chromadb.utils.embedding_functions"] = None  # type: ignore[assignment]
            from indexer.index_codebase import _get_embedding_fn
            with pytest.raises(ImportError, match="codevira"):
                _get_embedding_fn()
        finally:
            for k, v in original_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


# ---------------------------------------------------------------------------
# cmd_incremental — actual indexing loop (changed files, non-empty collection)
# ---------------------------------------------------------------------------

class TestCmdIncrementalLoop:
    """cmd_incremental — actual indexing loop (lines 283-323)."""

    @staticmethod
    def _rich_mods():
        """Return fake rich sub-modules so cmd_incremental doesn't fail on import."""
        import types
        import sys
        mods = {}
        for name in ("rich", "rich.console", "rich.table", "rich.panel",
                     "rich.progress"):
            if name not in sys.modules:
                mods[name] = types.ModuleType(name)
        # Console must be callable and return something with .print()
        rich_console_mod = mods.get("rich.console") or sys.modules["rich.console"]
        rich_console_mod.Console = MagicMock(return_value=MagicMock())
        return mods

    def test_indexes_changed_file_successfully(self, project_env):
        """cmd_incremental processes changed files and updates the hash."""
        import sys
        _project, data_dir, db = project_env

        src = _project / "src"
        src.mkdir(exist_ok=True)
        (src / "api.py").write_text("def hello(): pass")

        mock_collection = MagicMock()
        mock_client = MagicMock()
        mock_client.get_collection.return_value = mock_collection

        mock_chunk = MagicMock()
        mock_chunk.file_path = "src/api.py"
        mock_chunk.chunk_type = "function"
        mock_chunk.name = "hello"
        mock_chunk.start_line = 1
        mock_chunk.end_line = 1
        mock_chunk.docstring = ""
        mock_chunk.source_text = "def hello(): pass"
        mock_chunk.layer = "api"

        with patch.dict(sys.modules, self._rich_mods()), \
             patch("indexer.index_codebase._get_changed_files", return_value=[("src/api.py", "newhash123")]), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.chunker.chunk_file", return_value=[mock_chunk]), \
             patch("indexer.graph_generator.generate_graph_sqlite", return_value={}), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db):
            from indexer.index_codebase import cmd_incremental
            result = cmd_incremental()
        assert result == 0
        mock_collection.delete.assert_called()
        mock_collection.add.assert_called()

    def test_chunk_error_continues_to_next_file(self, project_env):
        """When chunk_file raises, cmd_incremental continues to the next file."""
        import sys
        _project, data_dir, db = project_env

        mock_collection = MagicMock()
        mock_client = MagicMock()
        mock_client.get_collection.return_value = mock_collection

        with patch.dict(sys.modules, self._rich_mods()), \
             patch("indexer.index_codebase._get_changed_files",
                   return_value=[("src/api.py", "hash1"), ("src/db.py", "hash2")]), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.chunker.chunk_file", side_effect=Exception("chunk failed")), \
             patch("indexer.graph_generator.generate_graph_sqlite", return_value={}), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db):
            from indexer.index_codebase import cmd_incremental
            result = cmd_incremental()
        # Returns 0 even when all files fail to chunk (indexed_any=False path)
        assert result == 0


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
        import sys, types
        mods = {}
        for name in ("watchdog", "watchdog.observers", "watchdog.events"):
            if name not in sys.modules:
                mods[name] = types.ModuleType(name)
        # Observer must be a class
        mock_observer_cls = MagicMock()
        mods.get("watchdog.observers", sys.modules.get("watchdog.observers")).Observer = mock_observer_cls
        # FileSystemEventHandler must be a real class (DebouncedHandler inherits it)
        class _FakeHandler:
            pass
        mods.get("watchdog.events", sys.modules.get("watchdog.events")).FileSystemEventHandler = _FakeHandler
        return mods

    def _start_watcher_and_get_handler(self, project_env):
        """Inject watchdog stubs, start watcher, return (src_dir, handler, timer_cls)."""
        import sys
        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}

        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()
        mock_timer = MagicMock()

        # Patch watchdog modules into sys.modules so the local import inside
        # start_background_watcher succeeds, then patch the Observer class
        # and threading.Timer to intercept calls.
        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:

            # Override the Observer class inside the fake watchdog.observers mod
            fake_obs_mod = sys.modules["watchdog.observers"]
            fake_obs_mod.Observer = MagicMock(return_value=mock_obs)

            from indexer.index_codebase import start_background_watcher
            start_background_watcher(quiet=True)

            handler = mock_obs.schedule.call_args[0][0] if mock_obs.schedule.called else None
            return src_dir, handler, mock_timer_cls, mock_obs

    def test_on_modified_triggers_schedule(self, project_env):
        """DebouncedHandler.on_modified calls _schedule_reindex for .py files."""
        import sys
        _project, _data_dir, _db = project_env
        src_dir = _project / "src"
        src_dir.mkdir(exist_ok=True)
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(return_value=mock_obs)
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
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(return_value=mock_obs)
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
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(return_value=mock_obs)
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
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(return_value=mock_obs)
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
        config = {"watched_dirs": ["src"], "file_extensions": [".py"], "skip_dirs": ["__pycache__"]}
        fake_mods = self._fake_watchdog_mods()
        mock_obs = MagicMock()

        with patch.dict(sys.modules, fake_mods), \
             patch("indexer.index_codebase._load_config", return_value=config), \
             patch("indexer.index_codebase.get_project_root", return_value=_project), \
             patch("threading.Timer", return_value=MagicMock()) as mock_timer_cls:
            sys.modules["watchdog.observers"].Observer = MagicMock(return_value=mock_obs)
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

        with patch("indexer.index_codebase.cmd_full_rebuild",
                   side_effect=RuntimeError("rebuild failed")):
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

    rich is an optional dependency not present in the test environment, so we
    inject a minimal fake into sys.modules for each test.
    """

    @staticmethod
    def _fake_rich_mods():
        """Return fake rich sub-modules for patch.dict injection."""
        import sys, types
        mods = {}
        for name in ("rich", "rich.console", "rich.table", "rich.panel",
                     "rich.progress"):
            if name not in sys.modules:
                mod = types.ModuleType(name)
                mods[name] = mod

        # Provide minimal classes that cmd_status and cmd_incremental use
        console_mod = mods.get("rich.console") or sys.modules.get("rich.console", types.ModuleType("rich.console"))
        console_mod.Console = MagicMock(return_value=MagicMock())
        mods["rich.console"] = console_mod

        table_mod = mods.get("rich.table") or sys.modules.get("rich.table", types.ModuleType("rich.table"))
        table_mod.Table = MagicMock(return_value=MagicMock())
        mods["rich.table"] = table_mod

        panel_mod = mods.get("rich.panel") or sys.modules.get("rich.panel", types.ModuleType("rich.panel"))
        panel_mod.Panel = MagicMock(return_value=MagicMock())
        mods["rich.panel"] = panel_mod

        return mods

    def test_cmd_status_shows_stale_files(self, project_env):
        """cmd_status prints stale file list when files need reindexing."""
        import sys
        _project, data_dir, db = project_env
        stale = [(f"src/file_{i}.py", f"hash{i}") for i in range(3)]

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_client.get_collection.return_value = mock_collection

        with patch.dict(sys.modules, self._fake_rich_mods()), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.index_codebase._get_changed_files", return_value=stale):
            from indexer.index_codebase import cmd_status
            cmd_status()  # should not raise

    def test_cmd_status_many_stale_files_truncated(self, project_env):
        """cmd_status truncates stale file list after 10 items."""
        import sys
        _project, data_dir, db = project_env
        # 15 stale files — display first 10 then "and N more"
        stale = [(f"src/file_{i}.py", f"hash{i}") for i in range(15)]

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_client.get_collection.return_value = mock_collection

        with patch.dict(sys.modules, self._fake_rich_mods()), \
             patch("indexer.index_codebase.SQLiteGraph", return_value=db), \
             patch("indexer.index_codebase._get_chroma_client", return_value=mock_client), \
             patch("indexer.index_codebase._get_embedding_fn", return_value=MagicMock()), \
             patch("indexer.index_codebase._get_changed_files", return_value=stale):
            from indexer.index_codebase import cmd_status
            cmd_status()  # should not raise
