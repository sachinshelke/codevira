"""
Tests for mcp_server/cli.py

Covers:
  - _set_project_dir_early(): parse --project-dir from raw args
  - _detect_project_root_markers(): check for project root markers
  - main(): argument parsing and subcommand dispatch
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.cli import _set_project_dir_early, _detect_project_root_markers


# ---------------------------------------------------------------------------
# _set_project_dir_early
# ---------------------------------------------------------------------------

class TestSetProjectDirEarly:
    """Parse --project-dir from a raw args list."""

    def test_space_separated(self):
        result = _set_project_dir_early(["--project-dir", "/tmp/foo"])
        assert result == Path("/tmp/foo").resolve()

    def test_equals_separated(self):
        result = _set_project_dir_early(["--project-dir=/tmp/foo"])
        assert result == Path("/tmp/foo").resolve()

    def test_no_project_dir(self):
        result = _set_project_dir_early(["init"])
        assert result is None

    def test_flag_without_value(self):
        result = _set_project_dir_early(["--project-dir"])
        assert result is None

    def test_flag_at_end_of_args(self):
        """--project-dir as the last arg with no following value."""
        result = _set_project_dir_early(["init", "--project-dir"])
        assert result is None

    def test_empty_args(self):
        result = _set_project_dir_early([])
        assert result is None

    def test_mixed_args(self):
        """--project-dir buried among other flags."""
        result = _set_project_dir_early(
            ["serve", "--port", "8080", "--project-dir", "/tmp/bar", "--https"]
        )
        assert result == Path("/tmp/bar").resolve()


# ---------------------------------------------------------------------------
# _detect_project_root_markers
# ---------------------------------------------------------------------------

class TestDetectProjectRootMarkers:
    """Check whether a directory contains project root markers."""

    def test_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _detect_project_root_markers(tmp_path) is True

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert _detect_project_root_markers(tmp_path) is True

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='test'\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        assert _detect_project_root_markers(tmp_path) is True

    def test_empty_dir(self, tmp_path):
        assert _detect_project_root_markers(tmp_path) is False

    def test_unrelated_files(self, tmp_path):
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / "notes.txt").write_text("hello")
        assert _detect_project_root_markers(tmp_path) is False


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

# All subcommand handlers that main() dispatches to.
_PATCH_TARGETS = [
    "mcp_server.cli.cmd_init",
    "mcp_server.cli.cmd_index",
    "mcp_server.cli.cmd_status",
    "mcp_server.cli.cmd_report",
    "mcp_server.cli.cmd_serve",
    "mcp_server.cli.cmd_register",
    "mcp_server.cli.cmd_server",
]


def _make_patches():
    """Return a dict of (target_name -> patcher) for all subcommand handlers."""
    return {t: patch(t, new_callable=MagicMock) for t in _PATCH_TARGETS}


class TestMainDispatch:
    """Verify that main() routes sys.argv to the correct subcommand handler."""

    def _run_main(self, argv_list: list[str], mocks: dict):
        """Invoke main() with the given argv and all handlers mocked."""
        from mcp_server.cli import main

        with patch.object(sys, "argv", ["codevira"] + argv_list):
            # Also patch set_project_dir to avoid side effects
            with patch("mcp_server.cli.set_project_dir", create=True):
                main()
        return mocks

    def test_init(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["init"], mocks)
            mocks["mcp_server.cli.cmd_init"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_index_full(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["index", "--full"], mocks)
            mocks["mcp_server.cli.cmd_index"].assert_called_once_with(
                full=True, quiet=False
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_status(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["status"], mocks)
            mocks["mcp_server.cli.cmd_status"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_report_clear(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["report", "--clear"], mocks)
            mocks["mcp_server.cli.cmd_report"].assert_called_once_with(
                limit=20, clear=True
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_register(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["register"], mocks)
            mocks["mcp_server.cli.cmd_register"].assert_called_once_with(
                claude_desktop=False, http_url=None,
                autostart=False, autostart_port=7443, autostart_project_dir=None,
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_register_http_url(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(
                ["register", "--http-url", "https://localhost:7443/mcp"], mocks
            )
            mocks["mcp_server.cli.cmd_register"].assert_called_once_with(
                claude_desktop=False, http_url="https://localhost:7443/mcp",
                autostart=False, autostart_port=7443, autostart_project_dir=None,
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_serve_with_port_and_https(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["serve", "--port", "8080", "--https"], mocks)
            call_kwargs = mocks["mcp_server.cli.cmd_serve"].call_args
            assert call_kwargs[1]["port"] == 8080 or call_kwargs.kwargs["port"] == 8080
            assert call_kwargs[1].get("use_https") is True or call_kwargs.kwargs.get("use_https") is True
        finally:
            for p in patchers.values():
                p.stop()

    def test_no_subcommand_defaults_to_server(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main([], mocks)
            mocks["mcp_server.cli.cmd_server"].assert_called_once()
        finally:
            for p in patchers.values():
                p.stop()

    def test_index_without_full(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["index"], mocks)
            mocks["mcp_server.cli.cmd_index"].assert_called_once_with(
                full=False, quiet=False
            )
        finally:
            for p in patchers.values():
                p.stop()

    def test_report_default(self):
        patchers = _make_patches()
        mocks = {}
        for name, p in patchers.items():
            mocks[name] = p.start()
        try:
            self._run_main(["report"], mocks)
            mocks["mcp_server.cli.cmd_report"].assert_called_once_with(
                limit=20, clear=False
            )
        finally:
            for p in patchers.values():
                p.stop()


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------

class TestCmdInit:
    """Tests for the 10-step cmd_init() initialization workflow."""

    def _base_patches(self, tmp_path):
        """Return a dict of patch targets -> kwargs for all cmd_init dependencies."""
        data_dir = tmp_path / ".codevira"
        detected = {
            "name": "myproject",
            "language": "python",
            "collection_name": "myproject_index",
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
        }
        return data_dir, detected

    def test_basic_happy_path(self, tmp_path, capsys):
        """cmd_init() completes without error when all subsystems succeed."""
        data_dir, detected = self._base_patches(tmp_path)
        # Create the data_dir subdirs that cmd_init expects to create
        global_db_path = tmp_path / "global.db"

        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild") as mock_rebuild, \
             patch("indexer.index_codebase.cmd_generate_graph") as mock_graph, \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap") as mock_roadmap, \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={"Claude Code": "/some/path"}) as mock_inject, \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata") as mock_write_meta, \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()

        captured = capsys.readouterr()
        assert "Codevira — Project Initialization" in captured.out
        assert "myproject" in captured.out
        mock_rebuild.assert_called_once()

    def test_subdirectory_warning_abort(self, tmp_path, capsys):
        """When cwd has no markers but parent does, and user inputs 'n', sys.exit(0) is raised."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"

        def detect_markers_side_effect(path):
            # cwd (tmp_path) returns False, parent returns True
            if path == tmp_path:
                return False
            return True

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", side_effect=detect_markers_side_effect), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=MagicMock()), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"), \
             patch("builtins.input", return_value="n"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            with pytest.raises(SystemExit) as exc_info:
                cmd_init()
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Aborted" in captured.out

    def test_subdirectory_warning_continue(self, tmp_path, capsys):
        """When cwd has no markers but parent does, and user inputs 'y', init continues."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        def detect_markers_side_effect(path):
            if path == tmp_path:
                return False
            return True

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", side_effect=detect_markers_side_effect), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"), \
             patch("builtins.input", return_value="y"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()  # must NOT raise

        captured = capsys.readouterr()
        assert "Codevira — Project Initialization" in captured.out

    def test_migration_triggered(self, tmp_path, capsys):
        """When detect_migration_needed returns True, migrate_to_centralized is called."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        migration_result = {"migrated": True, "files_copied": 5, "new_path": str(data_dir)}

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=True), \
             patch("mcp_server.migrate.migrate_to_centralized", return_value=migration_result) as mock_migrate, \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()

        mock_migrate.assert_called_once_with(tmp_path)
        captured = capsys.readouterr()
        assert "5 files" in captured.out

    def test_migration_exception(self, tmp_path, capsys):
        """When migrate_to_centralized raises, init continues without crashing."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=True), \
             patch("mcp_server.migrate.migrate_to_centralized", side_effect=RuntimeError("disk full")), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()  # must NOT raise

        captured = capsys.readouterr()
        assert "failed" in captured.out or "disk full" in captured.out

    def test_no_inject_flag(self, tmp_path, capsys):
        """When cmd_init._no_inject = True, inject_ide_config is never called."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config") as mock_inject, \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = True
            cmd_init()

        mock_inject.assert_not_called()

    def test_global_memory_registration_exception(self, tmp_path, capsys):
        """When GlobalDB raises, cmd_init prints a warning and continues."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", side_effect=Exception("db locked")), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()  # must NOT raise

        captured = capsys.readouterr()
        assert "Global memory registration skipped" in captured.out

    def test_gitignore_addition(self, tmp_path, capsys):
        """In non-centralized mode, .codevira/ is added to .gitignore."""
        # non-centralized: data_dir inside cwd (not under ~/.codevira/projects)
        data_dir = tmp_path / ".codevira"
        detected = {
            "name": "myproject",
            "language": "python",
            "collection_name": "myproject_index",
            "watched_dirs": ["src"],
            "file_extensions": [".py"],
        }
        # Create .git so gitignore logic runs
        (tmp_path / ".git").mkdir()
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".codevira/" in gitignore.read_text()

    def test_index_build_exception(self, tmp_path, capsys):
        """When cmd_full_rebuild raises, cmd_init prints 'skipped' and continues."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild", side_effect=RuntimeError("chroma unavailable")), \
             patch("indexer.index_codebase.cmd_generate_graph"), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()  # must NOT raise

        captured = capsys.readouterr()
        assert "skipped" in captured.out

    def test_graph_stubs_exception(self, tmp_path, capsys):
        """When cmd_generate_graph raises, cmd_init prints 'skipped' and continues."""
        data_dir, detected = self._base_patches(tmp_path)
        global_db_path = tmp_path / "global.db"
        mock_gdb = MagicMock()
        mock_gdb.get_project_count.return_value = 1

        with patch("mcp_server.paths.get_project_root", return_value=tmp_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_package_data_dir", return_value=tmp_path), \
             patch("mcp_server.cli._detect_project_root_markers", return_value=True), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("mcp_server.detect.auto_detect_project", return_value=detected), \
             patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_generate_graph", side_effect=RuntimeError("graph db error")), \
             patch("indexer.index_codebase.cmd_bootstrap_roadmap"), \
             patch("mcp_server.ide_inject.inject_ide_config", return_value={}), \
             patch("indexer.global_db.GlobalDB", return_value=mock_gdb), \
             patch("mcp_server.auto_init._write_metadata"), \
             patch("mcp_server.paths._get_git_remote_url", return_value=None), \
             patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.global_sync.import_global_to_project"):
            from mcp_server.cli import cmd_init
            cmd_init._overrides = {}
            cmd_init._no_inject = False
            cmd_init()  # must NOT raise

        captured = capsys.readouterr()
        assert "skipped" in captured.out


# ---------------------------------------------------------------------------
# cmd_index
# ---------------------------------------------------------------------------

class TestCmdIndex:
    """Tests for cmd_index()."""

    def test_cmd_index_full(self):
        """cmd_index(full=True) delegates to cmd_full_rebuild."""
        with patch("indexer.index_codebase.cmd_full_rebuild") as mock_rebuild, \
             patch("indexer.index_codebase.cmd_incremental") as mock_incremental:
            from mcp_server.cli import cmd_index
            cmd_index(full=True)
        mock_rebuild.assert_called_once()
        mock_incremental.assert_not_called()

    def test_cmd_index_incremental(self):
        """cmd_index(full=False) delegates to cmd_incremental."""
        with patch("indexer.index_codebase.cmd_full_rebuild") as mock_rebuild, \
             patch("indexer.index_codebase.cmd_incremental") as mock_incremental:
            from mcp_server.cli import cmd_index
            cmd_index(full=False)
        mock_incremental.assert_called_once_with(quiet=False)
        mock_rebuild.assert_not_called()

    def test_cmd_index_quiet(self):
        """cmd_index(full=False, quiet=True) passes quiet=True to cmd_incremental."""
        with patch("indexer.index_codebase.cmd_full_rebuild"), \
             patch("indexer.index_codebase.cmd_incremental") as mock_incremental:
            from mcp_server.cli import cmd_index
            cmd_index(full=False, quiet=True)
        mock_incremental.assert_called_once_with(quiet=True)


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    """Tests for cmd_status()."""

    def test_cmd_status_delegates(self):
        """cmd_status() calls the _cmd_status function from index_codebase."""
        with patch("indexer.index_codebase.cmd_status") as mock_status:
            from mcp_server.cli import cmd_status
            cmd_status()
        mock_status.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_report
# ---------------------------------------------------------------------------

class TestCmdReport:
    """Tests for cmd_report()."""

    def test_cmd_report_shows_crashes(self, capsys):
        """cmd_report() without clear flag prints crash log content."""
        with patch("mcp_server.crash_logger.read_recent_crashes", return_value="Error: boom\n  at foo.py:10") as mock_read, \
             patch("mcp_server.crash_logger.get_crash_log_path"):
            from mcp_server.cli import cmd_report
            cmd_report(limit=5, clear=False)

        mock_read.assert_called_once_with(limit=5)
        captured = capsys.readouterr()
        assert "Codevira — Crash Report" in captured.out
        assert "Error: boom" in captured.out

    def test_cmd_report_clear_existing(self, tmp_path, capsys):
        """cmd_report(clear=True) unlinks the log file when it exists."""
        log_path = tmp_path / "crash.log"
        log_path.write_text("some crash data")

        with patch("mcp_server.crash_logger.get_crash_log_path", return_value=log_path), \
             patch("mcp_server.crash_logger.read_recent_crashes"):
            from mcp_server.cli import cmd_report
            cmd_report(limit=20, clear=True)

        assert not log_path.exists()
        captured = capsys.readouterr()
        assert "Crash log cleared" in captured.out

    def test_cmd_report_clear_no_log(self, tmp_path, capsys):
        """cmd_report(clear=True) prints 'No crash log' when file does not exist."""
        missing_path = tmp_path / "nonexistent_crash.log"

        with patch("mcp_server.crash_logger.get_crash_log_path", return_value=missing_path), \
             patch("mcp_server.crash_logger.read_recent_crashes"):
            from mcp_server.cli import cmd_report
            cmd_report(limit=20, clear=True)

        captured = capsys.readouterr()
        assert "No crash log to clear" in captured.out


# ---------------------------------------------------------------------------
# cmd_server
# ---------------------------------------------------------------------------

class TestCmdServer:
    """Tests for cmd_server()."""

    def test_cmd_server_calls_main(self):
        """cmd_server() invokes server_main from mcp_server.server."""
        with patch("mcp_server.server.main") as mock_server_main:
            from mcp_server.cli import cmd_server
            cmd_server()
        mock_server_main.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_serve
# ---------------------------------------------------------------------------

class TestCmdServe:
    """Tests for cmd_serve() including service install/uninstall and regular serve."""

    def test_install_service_success(self, capsys):
        """install_service=True calls install_launchd and prints result plist path."""
        with patch("mcp_server.launchd.install_launchd", return_value="/Library/LaunchAgents/com.codevira.plist") as mock_install, \
             patch("mcp_server.http_server.run_http_server"):
            from mcp_server.cli import cmd_serve
            cmd_serve(port=7007, use_https=False, host="127.0.0.1", install_service=True)

        mock_install.assert_called_once_with(port=7007, use_https=False, host="127.0.0.1", project_dir=None)
        captured = capsys.readouterr()
        assert "Launchd service installed" in captured.out
        assert "com.codevira.plist" in captured.out

    def test_install_service_failure(self, capsys):
        """install_launchd raising RuntimeError causes sys.exit(1)."""
        with patch("mcp_server.launchd.install_launchd", side_effect=RuntimeError("permission denied")), \
             patch("mcp_server.http_server.run_http_server"):
            from mcp_server.cli import cmd_serve
            with pytest.raises(SystemExit) as exc_info:
                cmd_serve(port=7007, use_https=False, host="127.0.0.1", install_service=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.out

    def test_uninstall_service_removed(self, capsys):
        """uninstall_service=True with removed=True prints 'Launchd service removed'."""
        with patch("mcp_server.launchd.uninstall_launchd", return_value=True) as mock_uninstall, \
             patch("mcp_server.http_server.run_http_server"):
            from mcp_server.cli import cmd_serve
            cmd_serve(uninstall_service=True)

        mock_uninstall.assert_called_once()
        captured = capsys.readouterr()
        assert "Launchd service removed" in captured.out

    def test_uninstall_service_not_found(self, capsys):
        """uninstall_service=True with removed=False prints 'No launchd service'."""
        with patch("mcp_server.launchd.uninstall_launchd", return_value=False), \
             patch("mcp_server.http_server.run_http_server"):
            from mcp_server.cli import cmd_serve
            cmd_serve(uninstall_service=True)

        captured = capsys.readouterr()
        assert "No launchd service" in captured.out

    def test_uninstall_service_failure(self, capsys):
        """uninstall_launchd raising RuntimeError causes sys.exit(1)."""
        with patch("mcp_server.launchd.uninstall_launchd", side_effect=RuntimeError("not found")), \
             patch("mcp_server.http_server.run_http_server"):
            from mcp_server.cli import cmd_serve
            with pytest.raises(SystemExit) as exc_info:
                cmd_serve(uninstall_service=True)

        assert exc_info.value.code == 1

    def test_regular_serve(self):
        """No flags → calls run_http_server with correct args."""
        with patch("mcp_server.http_server.run_http_server") as mock_http:
            from mcp_server.cli import cmd_serve
            cmd_serve(host="0.0.0.0", port=8080, use_https=True, project_dir=None)

        mock_http.assert_called_once_with(
            host="0.0.0.0",
            port=8080,
            use_https=True,
            project_dir=None,
        )


# ---------------------------------------------------------------------------
# cmd_register
# ---------------------------------------------------------------------------

class TestCmdRegister:
    """Tests for cmd_register() covering all three dispatch paths."""

    def _base_patches(self):
        """Common patches needed for cmd_register's import block."""
        return {
            "mcp_server.paths.get_project_root": patch("mcp_server.paths.get_project_root", return_value=Path("/tmp/proj")),
            "mcp_server.ide_inject._resolve_command": patch("mcp_server.ide_inject._resolve_command", return_value=("codevira", "/usr/bin/python3")),
            "mcp_server.ide_inject.detect_installed_ides": patch("mcp_server.ide_inject.detect_installed_ides", return_value=[]),
            "mcp_server.ide_inject.inject_global_claude_code": patch("mcp_server.ide_inject.inject_global_claude_code", return_value="/path/to/claude"),
            "mcp_server.ide_inject.inject_global_cursor": patch("mcp_server.ide_inject.inject_global_cursor", return_value="/path/to/cursor"),
            "mcp_server.ide_inject.inject_global_windsurf": patch("mcp_server.ide_inject.inject_global_windsurf", return_value="/path/to/windsurf"),
            "mcp_server.ide_inject._inject_claude_desktop": patch("mcp_server.ide_inject._inject_claude_desktop", return_value="/path/to/desktop"),
            "mcp_server.ide_inject.inject_claude_http_url": patch("mcp_server.ide_inject.inject_claude_http_url", return_value="/path/to/http"),
        }

    def test_http_url_path(self, capsys):
        """http_url kwarg → calls inject_claude_http_url and returns early."""
        patches = self._base_patches()
        started = {k: p.start() for k, p in patches.items()}
        try:
            from mcp_server.cli import cmd_register
            cmd_register(http_url="https://example.com/mcp")
        finally:
            for p in patches.values():
                p.stop()

        started["mcp_server.ide_inject.inject_claude_http_url"].assert_called_once_with("https://example.com/mcp")
        captured = capsys.readouterr()
        assert "Claude Code (HTTP URL)" in captured.out

    def test_claude_desktop_path(self, capsys):
        """claude_desktop=True → calls _inject_claude_desktop and returns early."""
        patches = self._base_patches()
        started = {k: p.start() for k, p in patches.items()}
        try:
            from mcp_server.cli import cmd_register
            cmd_register(claude_desktop=True)
        finally:
            for p in patches.values():
                p.stop()

        started["mcp_server.ide_inject._inject_claude_desktop"].assert_called_once()
        captured = capsys.readouterr()
        assert "Claude Desktop" in captured.out

    def test_global_mode_with_ides(self, capsys):
        """No flags, detect_installed_ides returns multiple IDEs → inject functions called."""
        patches = self._base_patches()
        # Override the detect_installed_ides mock to return real IDEs
        patches["mcp_server.ide_inject.detect_installed_ides"] = patch(
            "mcp_server.ide_inject.detect_installed_ides",
            return_value=["claude", "cursor"],
        )
        started = {k: p.start() for k, p in patches.items()}
        try:
            from mcp_server.cli import cmd_register
            cmd_register()
        finally:
            for p in patches.values():
                p.stop()

        started["mcp_server.ide_inject.inject_global_claude_code"].assert_called_once()
        started["mcp_server.ide_inject.inject_global_cursor"].assert_called_once()
        captured = capsys.readouterr()
        assert "Claude Code (global)" in captured.out or "Cursor (global)" in captured.out

    def test_global_mode_no_ides(self, capsys):
        """No flags, detect_installed_ides returns [] → prints 'No AI tools detected'."""
        patches = self._base_patches()
        started = {k: p.start() for k, p in patches.items()}
        try:
            from mcp_server.cli import cmd_register
            cmd_register()
        finally:
            for p in patches.values():
                p.stop()

        captured = capsys.readouterr()
        assert "No AI tools detected" in captured.out

    def test_global_mode_ide_exception(self, capsys):
        """Inject function raises → prints warning and continues without crashing."""
        patches = self._base_patches()
        patches["mcp_server.ide_inject.detect_installed_ides"] = patch(
            "mcp_server.ide_inject.detect_installed_ides",
            return_value=["claude"],
        )
        patches["mcp_server.ide_inject.inject_global_claude_code"] = patch(
            "mcp_server.ide_inject.inject_global_claude_code",
            side_effect=RuntimeError("config locked"),
        )
        started = {k: p.start() for k, p in patches.items()}
        try:
            from mcp_server.cli import cmd_register
            cmd_register()  # must NOT raise
        finally:
            for p in patches.values():
                p.stop()

        captured = capsys.readouterr()
        assert "Warning" in captured.out or "could not configure" in captured.out


# ---------------------------------------------------------------------------
# main() — serve sub-project-dir path (lines 596-602)
# ---------------------------------------------------------------------------

class TestMainServeWithProjectDir:
    """Tests for main() when serve subcommand includes --project-dir."""

    def test_serve_with_sub_project_dir(self, tmp_path):
        """main() with ['serve', '--project-dir', path] resolves and sets project_dir."""
        project_path = str(tmp_path)
        mock_set_project_dir = MagicMock()
        mock_cmd_serve = MagicMock()

        patchers = [
            patch("mcp_server.cli.cmd_serve", mock_cmd_serve),
            patch("mcp_server.cli.cmd_init", MagicMock()),
            patch("mcp_server.cli.cmd_index", MagicMock()),
            patch("mcp_server.cli.cmd_status", MagicMock()),
            patch("mcp_server.cli.cmd_report", MagicMock()),
            patch("mcp_server.cli.cmd_register", MagicMock()),
            patch("mcp_server.cli.cmd_server", MagicMock()),
            # Patch set_project_dir in the paths module (imported inside main())
            patch("mcp_server.paths.set_project_dir", mock_set_project_dir),
        ]
        for p in patchers:
            p.start()
        try:
            from mcp_server.cli import main
            with patch.object(sys, "argv", ["codevira", "serve", "--project-dir", project_path]):
                main()
        finally:
            for p in patchers:
                p.stop()

        # set_project_dir should have been called with the resolved path
        mock_set_project_dir.assert_called_with(Path(project_path).resolve())
        # cmd_serve should have been called with project_dir set
        mock_cmd_serve.assert_called_once()
        call_kwargs = mock_cmd_serve.call_args.kwargs
        assert call_kwargs.get("project_dir") == Path(project_path).resolve()
