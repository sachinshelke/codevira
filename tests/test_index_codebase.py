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
