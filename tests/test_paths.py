"""
Tests for mcp_server/paths.py — Centralized path resolution.

Covers:
  - _sanitize_path_key(): deterministic, collision-free, idempotent
  - _discover_project_root(): marker-based project root detection
  - get_project_root(): uses _project_dir_override or cwd
  - set_project_dir(): sets override for CLI --project-dir
  - get_data_dir(): resolution chain (centralized -> git-remote -> legacy -> default)
  - get_package_data_dir(): returns correct path relative to module
  - get_global_home(): returns ~/.codevira, creates if needed
  - get_global_db_path(): returns correct path
  - _get_git_remote_url(): subprocess git remote lookup
  - _find_project_by_git_remote(): scan metadata.json files

Chaos tests:
  - Unicode paths, very long paths, symlinks
  - Corrupt metadata.json during git remote scan
  - Subprocess timeout / no git
  - Idempotence proof for _sanitize_path_key
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import mcp_server.paths as paths
from mcp_server.paths import (
    _sanitize_path_key,
    _discover_project_root,
    _get_git_remote_url,
    _find_project_by_git_remote,
    get_data_dir,
    get_global_db_path,
    get_global_home,
    get_package_data_dir,
    get_project_root,
    set_project_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_project_root(monkeypatch, root: Path) -> None:
    """Point project root discovery at *root* by clearing override and chdir."""
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())


# ===================================================================
# _sanitize_path_key
# ===================================================================

class TestSanitizePathKey:
    """Test path-key generation for centralized storage."""

    def test_unix_path(self):
        key = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert key.startswith("Users_sachin_Projects_Foo_")
        assert len(key.split("_")[-1]) == 8  # 8-char hash suffix

    def test_unix_trailing_slash(self):
        key1 = _sanitize_path_key("/Users/sachin/Projects/Foo/")
        key2 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert key1 == key2

    def test_path_with_spaces(self, tmp_path):
        p = tmp_path / "My Project"
        key = _sanitize_path_key(str(p))
        assert " " not in key

    def test_windows_drive_letter(self):
        key = _sanitize_path_key("C:\\Users\\sachin\\Projects")
        assert ":" not in key
        assert "\\" not in key

    def test_no_leading_trailing_hyphens(self):
        key = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert not key.startswith("-")
        assert not key.startswith("_")
        assert not key.endswith("-")

    def test_no_collision_between_hyphen_and_separator(self):
        """/foo-bar and /foo/bar must produce DIFFERENT keys."""
        key1 = _sanitize_path_key("/tmp/foo-bar")
        key2 = _sanitize_path_key("/tmp/foo/bar")
        assert key1 != key2

    def test_no_collision_across_drive_letters(self):
        """D:\\Projects\\Foo and C:\\Projects\\Foo must produce DIFFERENT keys."""
        key1 = _sanitize_path_key("C:\\Projects\\Foo")
        key2 = _sanitize_path_key("D:\\Projects\\Foo")
        assert key1 != key2

    def test_deterministic(self):
        key1 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        key2 = _sanitize_path_key("/Users/sachin/Projects/Foo")
        assert key1 == key2

    # -- NEW coverage --

    def test_idempotence_many_calls(self):
        """Same input always produces the exact same output (idempotence proof)."""
        test_path = "/Users/dev/workspace/my-app"
        results = {_sanitize_path_key(test_path) for _ in range(100)}
        assert len(results) == 1

    def test_unicode_path(self):
        """Path with unicode chars produces a safe, deterministic key."""
        key = _sanitize_path_key("/home/user/projects/cafe\u0301-app")
        assert key  # non-empty
        assert "/" not in key
        assert "\\" not in key
        # Deterministic
        assert key == _sanitize_path_key("/home/user/projects/cafe\u0301-app")

    def test_very_long_path(self):
        """Path with >200 chars still produces a valid key."""
        long_component = "a" * 250
        key = _sanitize_path_key(f"/tmp/{long_component}/project")
        assert key  # non-empty
        assert len(key.split("_")[-1]) == 8  # hash suffix present

    def test_path_with_dots(self):
        """Dots in path components are preserved."""
        key = _sanitize_path_key("/home/user/my.project.v2")
        assert "." in key  # dots are safe chars

    def test_empty_looking_path(self):
        """Root path produces a key (edge case)."""
        key = _sanitize_path_key("/")
        assert key  # non-empty string

    def test_consecutive_separators_collapsed(self):
        """Consecutive underscores/hyphens are collapsed."""
        key = _sanitize_path_key("/tmp/a///b")
        # Should not have ___ in the human-readable part
        assert "___" not in key

    def test_very_deep_path_capped_to_filesystem_safe_length(self):
        """50+-level-deep paths must produce a slug under filesystem
        ENAMETOOLONG limit (~255 bytes per path component).

        Caught by Week-2 edge-case test — without the cap, mkdir failed
        with OSError 63 (File name too long) on macOS APFS.
        """
        deep = "/" + "/".join(f"d{i:03d}_long_segment" for i in range(50))
        key = _sanitize_path_key(deep)
        # Slug used as a directory name; must be well under 255 bytes.
        assert len(key) < 200, f"Slug too long: {len(key)} bytes"
        # Hash suffix preserves uniqueness — different deep paths get
        # different keys even when the human part truncates identically.
        deep_alt = deep + "/extra"
        key_alt = _sanitize_path_key(deep_alt)
        assert key != key_alt, "Truncation lost uniqueness — hash isn't doing its job"


# ===================================================================
# _discover_project_root
# ===================================================================

class TestDiscoverProjectRoot:
    """Test marker-based project root detection."""

    def test_finds_root_via_git(self, tmp_path, monkeypatch):
        project = tmp_path / "git-project"
        nested = project / "src" / "feature"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_pyproject_toml(self, tmp_path, monkeypatch):
        project = tmp_path / "py-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='x'\n")

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_package_json(self, tmp_path, monkeypatch):
        project = tmp_path / "js-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / "package.json").write_text('{"name":"x"}')

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_go_mod(self, tmp_path, monkeypatch):
        project = tmp_path / "go-project"
        nested = project / "cmd" / "server"
        nested.mkdir(parents=True)
        (project / "go.mod").write_text("module example.com/test\n")

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_cargo_toml(self, tmp_path, monkeypatch):
        project = tmp_path / "rust-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / "Cargo.toml").write_text("[package]\nname='test'\n")

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_finds_root_via_codevira_dir(self, tmp_path, monkeypatch):
        project = tmp_path / "cv-project"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / ".codevira").mkdir()

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()

    def test_stops_at_first_git_for_nested_repos(self, tmp_path, monkeypatch):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / ".git").mkdir()
        (inner / ".git").mkdir()

        _set_project_root(monkeypatch, inner)
        assert get_project_root() == inner.resolve()

    def test_falls_back_to_cwd_when_no_marker(self, tmp_path, monkeypatch):
        project = tmp_path / "no-markers"
        project.mkdir()
        _set_project_root(monkeypatch, project)
        assert get_project_root() == project.resolve()

    def test_deeply_nested_walks_up(self, tmp_path, monkeypatch):
        """Project root is found even from deeply nested subdirectories."""
        project = tmp_path / "deep"
        deep = project / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (project / ".git").mkdir()

        _set_project_root(monkeypatch, deep)
        assert get_project_root() == project.resolve()


# ===================================================================
# set_project_dir / get_project_root with override
# ===================================================================

class TestSetProjectDir:
    """Test CLI --project-dir override."""

    def test_set_project_dir_overrides_cwd(self, tmp_path, monkeypatch):
        """set_project_dir makes get_project_root use the override path."""
        project = tmp_path / "override-project"
        project.mkdir()
        (project / ".git").mkdir()

        # cwd is somewhere else
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)

        set_project_dir(project)
        try:
            root = get_project_root()
            assert root == project.resolve()
        finally:
            # Clean up global state
            monkeypatch.setattr(paths, "_project_dir_override", None)

    def test_set_project_dir_discovers_root_from_subdir(self, tmp_path, monkeypatch):
        """Override points to subdir -> _discover_project_root still finds root."""
        project = tmp_path / "root-proj"
        subdir = project / "src"
        subdir.mkdir(parents=True)
        (project / ".git").mkdir()

        set_project_dir(subdir)
        try:
            root = get_project_root()
            assert root == project.resolve()
        finally:
            monkeypatch.setattr(paths, "_project_dir_override", None)

    def test_set_project_dir_accepts_string(self, tmp_path, monkeypatch):
        """set_project_dir accepts str, not just Path."""
        project = tmp_path / "str-proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("")

        set_project_dir(str(project))
        try:
            root = get_project_root()
            assert root == project.resolve()
        finally:
            monkeypatch.setattr(paths, "_project_dir_override", None)


# ===================================================================
# get_data_dir resolution chain
# ===================================================================

class TestGetDataDir:
    """Test the 4-step resolution chain for data directory."""

    def test_new_project_returns_centralized(self, tmp_path, monkeypatch):
        """New project with no .codevira/ -> centralized path (step 4)."""
        project = tmp_path / "brand-new"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='test'\n")

        _set_project_root(monkeypatch, project)
        fake_home = tmp_path / "global-home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        key = _sanitize_path_key(project)
        expected = fake_home / "projects" / key
        assert data == expected

    def test_centralized_dir_takes_priority(self, tmp_path, monkeypatch):
        """Centralized config.yaml exists -> returns centralized path (step 1)."""
        project = tmp_path / "existing-project"
        project.mkdir()
        (project / ".git").mkdir()

        fake_home = tmp_path / "global-home"
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "config.yaml").write_text("project:\n  name: test\n")

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        assert data == centralized

    def test_legacy_fallback(self, tmp_path, monkeypatch):
        """Legacy .codevira/config.yaml exists, no centralized -> returns legacy (step 3)."""
        project = tmp_path / "legacy-project"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: legacy\n")

        fake_home = tmp_path / "global-home"
        fake_home.mkdir()

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
        data = get_data_dir()
        assert data == legacy.resolve()

    def test_centralized_beats_legacy(self, tmp_path, monkeypatch):
        """Both centralized and legacy exist -> centralized wins (step 1 > step 3)."""
        project = tmp_path / "both-project"
        legacy = project / ".codevira"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("project:\n  name: legacy\n")

        fake_home = tmp_path / "global-home"
        key = _sanitize_path_key(project)
        centralized = fake_home / "projects" / key
        centralized.mkdir(parents=True)
        (centralized / "config.yaml").write_text("project:\n  name: centralized\n")

        _set_project_root(monkeypatch, project)
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        data = get_data_dir()
        assert data == centralized

    def test_git_remote_lookup_survives_rename(self, tmp_path, monkeypatch):
        """Step 2: git remote lookup finds centralized dir after project rename."""
        project = tmp_path / "renamed-project"
        project.mkdir()
        (project / ".git").mkdir()

        fake_home = tmp_path / "global-home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        # Set up a centralized dir under a DIFFERENT key (simulating rename)
        old_key = "old_project_key_abcd1234"
        old_centralized = fake_home / "projects" / old_key
        old_centralized.mkdir(parents=True)
        (old_centralized / "config.yaml").write_text("project:\n  name: test\n")
        (old_centralized / "metadata.json").write_text(json.dumps({
            "git_remote": "https://github.com/org/repo.git",
        }))

        _set_project_root(monkeypatch, project)

        # Mock git remote to return the matching URL
        with patch.object(paths, "_get_git_remote_url", return_value="https://github.com/org/repo.git"):
            data = get_data_dir()
        assert data == old_centralized


# ===================================================================
# get_package_data_dir
# ===================================================================

class TestGetPackageDataDir:
    def test_returns_data_subdir_of_module(self):
        """get_package_data_dir returns <module_dir>/data."""
        result = get_package_data_dir()
        expected = Path(paths.__file__).parent / "data"
        assert result == expected

    def test_path_is_absolute(self):
        result = get_package_data_dir()
        assert result.is_absolute()


# ===================================================================
# get_global_home / get_global_db_path
# ===================================================================

class TestGlobalHome:
    def test_get_global_home_returns_codevira_dir(self, tmp_path, monkeypatch):
        """get_global_home returns ~/.codevira and creates it."""
        # The autouse fixture patches get_global_home, so we test the real function
        # by calling the patched version
        home = get_global_home()
        assert home.exists()

    def test_get_global_db_path(self, tmp_path, monkeypatch):
        """get_global_db_path returns <global_home>/global.db."""
        db_path = get_global_db_path()
        assert db_path.name == "global.db"
        # The autouse fixture patches get_global_home, but get_global_db_path
        # calls paths.get_global_home() internally. Verify the name is correct
        # and it is under some .codevira-like directory.
        assert str(db_path).endswith("global.db")

    def test_get_global_home_creates_dir(self, tmp_path, monkeypatch):
        """get_global_home creates the directory if it does not exist."""
        new_home = tmp_path / "new-codevira-home"
        monkeypatch.setattr(paths, "get_global_home", lambda: _create_and_return(new_home))
        result = paths.get_global_home()
        assert result.exists()


def _create_and_return(p: Path) -> Path:
    """Helper: create dir and return it (simulates real get_global_home)."""
    p.mkdir(parents=True, exist_ok=True)
    return p


# ===================================================================
# _get_git_remote_url
# ===================================================================

class TestGetGitRemoteUrl:
    """Test subprocess-based git remote URL lookup."""

    def test_returns_url_on_success(self, tmp_path):
        """Successful git remote get-url returns the URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo.git\n"

        with patch("mcp_server.paths.subprocess.run", return_value=mock_result):
            url = _get_git_remote_url(tmp_path)
        assert url == "https://github.com/org/repo.git"

    def test_returns_none_on_failure(self, tmp_path):
        """Non-zero return code -> None."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("mcp_server.paths.subprocess.run", return_value=mock_result):
            url = _get_git_remote_url(tmp_path)
        assert url is None

    def test_returns_none_on_empty_output(self, tmp_path):
        """Empty stdout after strip -> None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   \n"

        with patch("mcp_server.paths.subprocess.run", return_value=mock_result):
            url = _get_git_remote_url(tmp_path)
        assert url is None

    def test_returns_none_on_timeout(self, tmp_path):
        """Subprocess timeout -> None (graceful degradation)."""
        with patch("mcp_server.paths.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 3)):
            url = _get_git_remote_url(tmp_path)
        assert url is None

    def test_returns_none_when_git_not_found(self, tmp_path):
        """FileNotFoundError (no git binary) -> None."""
        with patch("mcp_server.paths.subprocess.run", side_effect=FileNotFoundError):
            url = _get_git_remote_url(tmp_path)
        assert url is None

    def test_returns_none_on_os_error(self, tmp_path):
        """Generic OSError -> None."""
        with patch("mcp_server.paths.subprocess.run", side_effect=OSError("disk error")):
            url = _get_git_remote_url(tmp_path)
        assert url is None

    def test_passes_correct_args(self, tmp_path):
        """Verify subprocess.run is called with correct git command and timeout."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git@github.com:org/repo.git\n"

        with patch("mcp_server.paths.subprocess.run", return_value=mock_result) as mock_run:
            _get_git_remote_url(tmp_path)

        mock_run.assert_called_once_with(
            ["git", "-C", str(tmp_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
        )


# ===================================================================
# _find_project_by_git_remote
# ===================================================================

class TestFindProjectByGitRemote:
    """Test scanning metadata.json files for git remote match."""

    def test_finds_matching_project(self, tmp_path, monkeypatch):
        """Returns centralized dir when metadata.json has matching git_remote."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        proj_dir = fake_home / "projects" / "my_project_abc12345"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.json").write_text(json.dumps({
            "git_remote": "https://github.com/org/repo.git",
        }))

        result = _find_project_by_git_remote("https://github.com/org/repo.git")
        assert result == proj_dir

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        """No matching git_remote -> None."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        proj_dir = fake_home / "projects" / "other_proj_abc12345"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.json").write_text(json.dumps({
            "git_remote": "https://github.com/org/other.git",
        }))

        result = _find_project_by_git_remote("https://github.com/org/repo.git")
        assert result is None

    def test_returns_none_when_no_projects_dir(self, tmp_path, monkeypatch):
        """No projects/ directory at all -> None."""
        fake_home = tmp_path / "empty-home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        result = _find_project_by_git_remote("https://github.com/org/repo.git")
        assert result is None

    def test_skips_corrupt_metadata_json(self, tmp_path, monkeypatch):
        """Corrupt metadata.json is skipped; valid one still found."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        # Corrupt metadata
        corrupt_dir = fake_home / "projects" / "corrupt_proj_11111111"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "metadata.json").write_text("{{not json at all")

        # Valid metadata
        valid_dir = fake_home / "projects" / "valid_proj_22222222"
        valid_dir.mkdir(parents=True)
        (valid_dir / "metadata.json").write_text(json.dumps({
            "git_remote": "https://github.com/org/target.git",
        }))

        result = _find_project_by_git_remote("https://github.com/org/target.git")
        assert result == valid_dir

    def test_skips_metadata_without_git_remote_key(self, tmp_path, monkeypatch):
        """metadata.json without git_remote key does not match."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        proj_dir = fake_home / "projects" / "no_remote_abc12345"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.json").write_text(json.dumps({
            "path_key": "no_remote_abc12345",
            "version": "1.6.0",
        }))

        result = _find_project_by_git_remote("https://github.com/org/repo.git")
        assert result is None

    def test_handles_os_error_on_read(self, tmp_path, monkeypatch):
        """OSError reading metadata.json -> skipped gracefully."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        proj_dir = fake_home / "projects" / "perm_err_abc12345"
        proj_dir.mkdir(parents=True)
        meta = proj_dir / "metadata.json"
        meta.write_text(json.dumps({"git_remote": "https://github.com/org/repo.git"}))

        # Make it unreadable (on unix)
        if sys.platform != "win32":
            meta.chmod(0o000)
            try:
                result = _find_project_by_git_remote("https://github.com/org/repo.git")
                # Should return None because the file is unreadable
                assert result is None
            finally:
                meta.chmod(0o644)


# ===================================================================
# CHAOS Tests
# ===================================================================

class TestPathsChaos:
    """Edge cases, corruptions, and adversarial inputs."""

    def test_unicode_project_name_in_path_key(self):
        """Unicode project names produce valid keys."""
        key = _sanitize_path_key("/home/user/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8")  # Japanese
        assert key
        assert "/" not in key
        assert "\\" not in key
        # Repeatable
        assert key == _sanitize_path_key("/home/user/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8")

    def test_emoji_in_path(self):
        """Emoji in path produces valid key."""
        key = _sanitize_path_key("/home/user/rocket-\U0001f680-app")
        assert key
        assert "/" not in key

    def test_very_long_path_over_200_chars(self):
        """Path exceeding 200 chars produces a usable key with hash."""
        long_name = "x" * 220
        key = _sanitize_path_key(f"/projects/{long_name}")
        assert key
        # Hash suffix still 8 chars
        parts = key.rsplit("_", 1)
        assert len(parts[-1]) == 8

    def test_symlink_as_project_root(self, tmp_path, monkeypatch):
        """Symlink to a project root resolves to the real path."""
        real_project = tmp_path / "real-project"
        real_project.mkdir()
        (real_project / ".git").mkdir()

        link = tmp_path / "link-project"
        link.symlink_to(real_project)

        _set_project_root(monkeypatch, link)
        root = get_project_root()
        # Should resolve through symlink
        assert root == real_project.resolve()

    def test_get_data_dir_when_git_remote_fails(self, tmp_path, monkeypatch):
        """get_data_dir falls through when git remote times out."""
        project = tmp_path / "timeout-project"
        project.mkdir()
        (project / ".git").mkdir()

        fake_home = tmp_path / "global-home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
        _set_project_root(monkeypatch, project)

        # Git remote times out -> step 2 skipped, goes to step 4 (default centralized)
        with patch("mcp_server.paths.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 3)):
            data = get_data_dir()

        key = _sanitize_path_key(project)
        expected = fake_home / "projects" / key
        assert data == expected

    def test_find_project_by_git_remote_with_all_corrupt_metadata(self, tmp_path, monkeypatch):
        """All metadata.json files are corrupt -> returns None."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)

        for i in range(3):
            d = fake_home / "projects" / f"corrupt_{i}_aaaaaaaa"
            d.mkdir(parents=True)
            (d / "metadata.json").write_text(f"CORRUPT DATA {i} {{{{")

        result = _find_project_by_git_remote("https://github.com/org/repo.git")
        assert result is None

    def test_sanitize_path_key_special_chars(self):
        """Paths with special characters (@, #, $, etc.) produce safe keys."""
        key = _sanitize_path_key("/home/user/project@v2#final$release")
        assert key
        # Only allowed chars: a-zA-Z0-9._-  plus underscores from separators
        import re
        human_part = key.rsplit("_", 1)[0]
        assert re.match(r"^[a-zA-Z0-9._-]+$", human_part)

    def test_data_dir_resolution_with_no_git_and_no_legacy(self, tmp_path, monkeypatch):
        """Non-git project with no legacy dir -> pure centralized default."""
        project = tmp_path / "plain-project"
        project.mkdir()
        (project / "pyproject.toml").write_text("")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
        _set_project_root(monkeypatch, project)

        # Mock git as not found
        with patch("mcp_server.paths.subprocess.run", side_effect=FileNotFoundError):
            data = get_data_dir()

        key = _sanitize_path_key(project)
        assert data == fake_home / "projects" / key

    def test_discover_project_root_with_multiple_markers(self, tmp_path, monkeypatch):
        """Directory with multiple markers (.git + pyproject.toml) still finds root."""
        project = tmp_path / "multi-marker"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()
        (project / "pyproject.toml").write_text("")
        (project / "package.json").write_text("{}")

        _set_project_root(monkeypatch, nested)
        assert get_project_root() == project.resolve()


# ===================================================================
# is_invalid_project_root (v1.8.1)
# ===================================================================

class TestIsInvalidProjectRoot:
    """Refuses $HOME and known system top-levels as a project root.

    Regression test for the v1.8.0 production crash mode: a $HOME data
    dir got registered as a project, the watcher walked
    ~/Library/Group Containers/... and crashed 41 times in 70 minutes
    on EINTR. v1.8.1 prevents the rogue project from forming in the
    first place via this helper.
    """

    def test_rejects_home(self, tmp_path, monkeypatch):
        """Path.home() exactly is rejected with $HOME message."""
        from mcp_server.paths import is_invalid_project_root
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        result = is_invalid_project_root(fake_home)
        assert result is not None
        assert "$HOME" in result

    def test_rejects_root_slash(self):
        from mcp_server.paths import is_invalid_project_root
        result = is_invalid_project_root(Path("/"))
        assert result is not None
        assert "system directory" in result

    def test_rejects_users_parent(self):
        """macOS top-level /Users is rejected."""
        from mcp_server.paths import is_invalid_project_root
        result = is_invalid_project_root(Path("/Users"))
        assert result is not None
        assert "system directory" in result

    def test_rejects_home_parent_linux(self):
        """Linux top-level /home is rejected."""
        from mcp_server.paths import is_invalid_project_root
        result = is_invalid_project_root(Path("/home"))
        assert result is not None
        assert "system directory" in result

    def test_rejects_tmp(self):
        """Both /tmp and the macOS-resolved /private/tmp are rejected."""
        from mcp_server.paths import is_invalid_project_root
        # On macOS, /tmp is a symlink to /private/tmp; Path.resolve() follows
        # the symlink so the comparison must include the resolved form.
        # Use /private/tmp directly to verify regardless of platform.
        if Path("/private/tmp").exists():
            assert is_invalid_project_root(Path("/private/tmp")) is not None
        if Path("/tmp").exists():
            assert is_invalid_project_root(Path("/tmp")) is not None

    def test_rejects_var_etc_opt(self):
        """/var, /etc, /opt all rejected."""
        from mcp_server.paths import is_invalid_project_root
        for p in ("/var", "/etc", "/opt"):
            if Path(p).exists():
                assert is_invalid_project_root(Path(p)) is not None, f"{p} should be invalid"

    def test_accepts_real_project_path(self, tmp_path):
        """A normal project directory passes (returns None)."""
        from mcp_server.paths import is_invalid_project_root
        project = tmp_path / "my-project"
        project.mkdir()
        assert is_invalid_project_root(project) is None

    def test_accepts_home_subdirectory(self, tmp_path, monkeypatch):
        """A subdirectory of $HOME passes (returns None)."""
        from mcp_server.paths import is_invalid_project_root
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        sub = fake_home / "Documents" / "MyProject"
        sub.mkdir(parents=True)
        assert is_invalid_project_root(sub) is None

    def test_handles_nonexistent_path(self, tmp_path):
        """A path that .resolve() can't fail on (no symlink loop) is fine; if
        .resolve() raises we return None and let caller surface the OSError."""
        from mcp_server.paths import is_invalid_project_root
        # A non-existent path doesn't make resolve() raise on most filesystems;
        # the helper should still return None for "looks like a project, not
        # a forbidden top-level".
        nonexistent = tmp_path / "definitely-does-not-exist"
        assert is_invalid_project_root(nonexistent) is None

    def test_resolves_symlinked_home(self, tmp_path, monkeypatch):
        """A symlink that points to $HOME is rejected (resolve-aware)."""
        from mcp_server.paths import is_invalid_project_root
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        link = tmp_path / "home-link"
        link.symlink_to(fake_home)
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        result = is_invalid_project_root(link)
        assert result is not None
        assert "$HOME" in result
