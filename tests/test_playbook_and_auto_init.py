"""
Tests for:
  - mcp_server/tools/playbook.py — curated rule playbooks by task type
  - mcp_server/auto_init.py — auto-initialization on first tool call

Split into two sections. The auto_init tests reset module globals between
tests to avoid state leakage.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# =====================================================================
# PLAYBOOK TESTS
# =====================================================================

from mcp_server.tools.playbook import get_playbook, PLAYBOOKS


class TestPlaybookValidTaskTypes:
    def test_add_route_returns_rules(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "api-standards.md").write_text("# API Standards\nRule content here.")
        (rules_dir / "coding-standards.md").write_text("# Coding Standards\nMore rules.")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("add_route")

        assert result["found"] is True
        assert result["task_type"] == "add_route"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "api-standards.md"
        assert "API Standards" in result["rules"][0]["content"]

    def test_commit_returns_rules(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "git_commits.md").write_text("# Git Commits\nCommit rules.")
        (rules_dir / "git-cicd-governance.md").write_text("# CI/CD Governance\nCI rules.")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("commit")

        assert result["found"] is True
        assert len(result["rules"]) == 2

    def test_write_test_returns_rules(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "testing-standards.md").write_text("# Testing Standards")
        (rules_dir / "smoke-testing.md").write_text("# Smoke Testing")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("write_test")

        assert result["found"] is True
        assert len(result["rules"]) == 2


class TestPlaybookUnknownAndEdge:
    def test_unknown_task_type_returns_not_found(self, tmp_path):
        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("nonexistent_task")

        assert result["found"] is False
        assert "nonexistent_task" == result["task_type"]
        assert "available_task_types" in result
        assert sorted(PLAYBOOKS.keys()) == result["available_task_types"]

    def test_missing_rule_file_returns_placeholder(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Only create one of the two expected files
        (rules_dir / "api-standards.md").write_text("# API Standards")
        # coding-standards.md is missing

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("add_route")

        assert result["found"] is True
        missing_rule = [r for r in result["rules"] if "File not found" in r["content"]]
        assert len(missing_rule) == 1

    def test_case_insensitivity_and_whitespace(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "api-standards.md").write_text("content")
        (rules_dir / "coding-standards.md").write_text("content")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("  Add_Route  ")

        assert result["found"] is True
        assert result["task_type"] == "add_route"

    def test_all_six_task_types_return_correct_rule_counts(self, tmp_path):
        """All 6 task types should map to the expected number of rule files."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Create all rule files referenced in PLAYBOOKS
        all_files = set()
        for rule_list in PLAYBOOKS.values():
            all_files.update(rule_list)
        for f in all_files:
            (rules_dir / f).write_text(f"# {f}\nContent.")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            for task_type, expected_files in PLAYBOOKS.items():
                result = get_playbook(task_type)
                assert result["found"] is True, f"Failed for {task_type}"
                assert len(result["rules"]) == len(expected_files), f"Wrong count for {task_type}"

    def test_note_field_present(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "coding-standards.md").write_text("content")
        (rules_dir / "resilience-observability.md").write_text("content")

        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("add_service")

        assert "note" in result
        assert "add_service" in result["note"]

    def test_hint_field_on_unknown_type(self, tmp_path):
        with patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path):
            result = get_playbook("foo")

        assert "hint" in result
        assert "get_node" in result["hint"]


# =====================================================================
# AUTO-INIT TESTS
# =====================================================================

import mcp_server.auto_init as ai
from mcp_server.auto_init import (
    ensure_project_initialized,
    get_init_progress,
    InitStatus,
    _write_config,
    _write_metadata,
)


@pytest.fixture(autouse=True)
def reset_auto_init():
    """Reset module-level state between tests to prevent leakage."""
    ai._init_done = False
    ai._indexing_thread = None
    ai._progress = {
        "status": "not_started",
        "files_indexed": 0,
        "total_files": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }
    ai._start_time = None
    yield


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


DEFAULT_DETECTED = {
    "name": "proj",
    "language": "python",
    "watched_dirs": ["src"],
    "file_extensions": [".py"],
    "collection_name": "proj",
}


def _bg_init_patches(detected, graph_side_effect=None, discover_return=None,
                     index_side_effect=ImportError):
    """Return a combined context manager for background init patches.

    Patches the lazy imports at their source modules.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("mcp_server.detect.auto_detect_project", return_value=detected))
    if graph_side_effect:
        stack.enter_context(patch("indexer.graph_generator.generate_graph_sqlite",
                                  side_effect=graph_side_effect))
    else:
        stack.enter_context(patch("indexer.graph_generator.generate_graph_sqlite"))
    stack.enter_context(patch("mcp_server.gitignore.discover_source_files",
                              return_value=discover_return or []))
    stack.enter_context(patch("indexer.index_codebase.start_background_full_index",
                              side_effect=index_side_effect))
    stack.enter_context(patch("mcp_server.auto_init._register_global"))
    return stack


class TestEnsureProjectInitialized:
    def test_first_call_on_uninitialized_project_triggers_init(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch("mcp_server.paths.get_project_root", return_value=project_root), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             _bg_init_patches(DEFAULT_DETECTED):
            status = ensure_project_initialized(project_root)

        assert isinstance(status, InitStatus)
        assert status.ready is False
        assert status.indexing is True

    def test_second_call_is_fast_path(self, tmp_path):
        """After init is done, second call returns immediately."""
        ai._init_done = True
        ai._progress["status"] = "ready"

        status = ensure_project_initialized()
        assert status.ready is True
        assert status.indexing is False

    def test_already_initialized_project_returns_ready(self, tmp_path):
        """If config.yaml already exists, returns ready=True without spawning a thread."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("project:\n  name: test\n")

        with patch("mcp_server.paths.get_project_root", return_value=project_root), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            status = ensure_project_initialized(project_root)

        assert status.ready is True
        assert status.indexing is False
        assert ai._init_done is True

    def test_fast_path_when_indexing(self, tmp_path):
        """When status is 'indexing', fast path returns ready=False, indexing=True."""
        ai._init_done = True
        ai._progress["status"] = "indexing"
        ai._progress["files_indexed"] = 5
        ai._progress["total_files"] = 10

        status = ensure_project_initialized()
        assert status.ready is False
        assert status.indexing is True
        assert status.files_indexed == 5
        assert status.total_files == 10


class TestGetInitProgress:
    def test_returns_default_status(self):
        progress = get_init_progress()
        assert progress["status"] == "not_started"
        assert progress["files_indexed"] == 0
        assert progress["total_files"] == 0

    def test_tracks_elapsed_time(self):
        ai._start_time = time.monotonic() - 5.0
        progress = get_init_progress()
        assert progress["elapsed_seconds"] >= 4.5

    def test_reflects_status_updates(self):
        ai._progress["status"] = "indexing"
        ai._progress["files_indexed"] = 42
        ai._progress["total_files"] = 100
        progress = get_init_progress()
        assert progress["status"] == "indexing"
        assert progress["files_indexed"] == 42
        assert progress["total_files"] == 100


class TestWriteConfig:
    def test_writes_config_yaml(self, tmp_path):
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


class TestWriteMetadata:
    def test_writes_metadata_json(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path / "my-project"
        project_root.mkdir()

        with patch("mcp_server.paths._sanitize_path_key", return_value="test_key"), \
             patch("mcp_server.paths._get_git_remote_url", return_value="git@github.com:test/repo.git"):
            _write_metadata(data_dir, project_root)

        meta_path = data_dir / "metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["path_key"] == "test_key"
        assert meta["git_remote"] == "git@github.com:test/repo.git"
        assert meta["original_path"] == str(project_root)
        assert meta["version"] == "1.6.0"
        assert meta["auto_initialized"] is True
        assert "created_at" in meta


class TestBackgroundInit:
    def test_graph_generation_failure_still_reaches_ready(self, tmp_path):
        """If graph generation fails, the init still completes with status=ready."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(DEFAULT_DETECTED,
                              graph_side_effect=RuntimeError("Graph failed")):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"

    def test_chromadb_not_installed_skips_gracefully(self, tmp_path):
        """When chromadb is not installed (ImportError), init still completes."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path("a.py"), Path("b.py")]
        with _bg_init_patches(DEFAULT_DETECTED, discover_return=fake_files,
                              index_side_effect=ImportError("No chromadb")):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "ready"
        assert ai._progress["files_indexed"] == 0

    def test_creates_directory_structure(self, tmp_path):
        """Background init creates graph/changesets, codeindex, and logs dirs."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(DEFAULT_DETECTED):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert (data_dir / "graph" / "changesets").is_dir()
        assert (data_dir / "codeindex").is_dir()
        assert (data_dir / "logs").is_dir()

    def test_config_yaml_written_during_init(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        detected = {
            "name": "proj",
            "language": "typescript",
            "watched_dirs": ["src"],
            "file_extensions": [".ts"],
            "collection_name": "proj",
        }

        with _bg_init_patches(detected):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        config = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert config["project"]["language"] == "typescript"

    def test_metadata_json_written_during_init(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with _bg_init_patches(DEFAULT_DETECTED):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert (data_dir / "metadata.json").exists()
        meta = json.loads((data_dir / "metadata.json").read_text())
        assert meta["original_path"] == str(project_root)
        assert meta["auto_initialized"] is True

    def test_total_files_tracked_from_discover(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        fake_files = [Path(f"src/f{i}.py") for i in range(15)]
        with _bg_init_patches(DEFAULT_DETECTED, discover_return=fake_files):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert ai._progress["total_files"] == 15

    def test_fatal_error_sets_error_status(self, tmp_path):
        """If auto_detect_project raises, status becomes 'error'."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with patch("mcp_server.detect.auto_detect_project",
                   side_effect=RuntimeError("Detect failed")):
            ai._start_time = time.monotonic()
            ai._run_background_init(project_root, data_dir)

        assert ai._progress["status"] == "error"
        assert "Detect failed" in ai._progress["error"]


class TestProgressTransitions:
    def test_not_started_to_initializing(self, tmp_path):
        """Progress starts at not_started, moves to initializing on first call."""
        assert ai._progress["status"] == "not_started"
        ai._update_progress(status="initializing")
        assert ai._progress["status"] == "initializing"

    def test_initializing_to_indexing(self):
        ai._progress["status"] = "initializing"
        ai._update_progress(status="indexing")
        assert ai._progress["status"] == "indexing"

    def test_indexing_to_ready(self):
        ai._progress["status"] = "indexing"
        ai._update_progress(status="ready")
        assert ai._progress["status"] == "ready"

    def test_update_progress_merges_fields(self):
        ai._update_progress(status="indexing", files_indexed=10, total_files=50)
        assert ai._progress["status"] == "indexing"
        assert ai._progress["files_indexed"] == 10
        assert ai._progress["total_files"] == 50

    def test_error_field_set_on_failure(self):
        ai._update_progress(status="error", error="Something broke")
        assert ai._progress["status"] == "error"
        assert ai._progress["error"] == "Something broke"


class TestInitStatusDataclass:
    def test_defaults(self):
        status = InitStatus(ready=True, indexing=False)
        assert status.files_indexed == 0
        assert status.total_files == 0

    def test_with_counts(self):
        status = InitStatus(ready=False, indexing=True, files_indexed=5, total_files=20)
        assert status.files_indexed == 5
        assert status.total_files == 20
