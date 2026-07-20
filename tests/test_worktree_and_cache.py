"""
test_worktree_and_cache.py — v3.7.1 binding defects 3 and 4.

**Worktrees / submodules.** ``project_binding`` tested ``(root / ".git").is_dir()``.
In a worktree or submodule ``.git`` is a FILE containing ``gitdir: <path>``, so
those roots were classified "not a repo": the binder declined to bind and the
server kept its inherited cwd — the wrong project. It also disagreed with
``paths._discover_project_root``, which accepts them via ``.exists()``. This
matters for anyone using ``.claude/worktrees/``.

**Cross-process cache staleness.** ``_data_dir_cache`` cached the rule-4 guess
("no store yet, assume the default centralized path"). That answer goes stale
the moment a store appears — which normally happens in ANOTHER process, since
the MCP server is already running when the user runs ``codevira init``. Nothing
invalidated it cross-process, so a live server kept using a directory the CLI
never reads. The same staleness masked the data-loss bug: a session looked
healthy only because it had resolved before the store moved.
"""

from __future__ import annotations

import pytest

import mcp_server.paths as paths
from mcp_server import project_binding


def _worktree(tmp_path, name="wt"):
    """A worktree-shaped root: .git is a FILE, not a directory."""
    wt = tmp_path / name
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    return wt


def _repo(tmp_path, name="repo"):
    r = tmp_path / name
    (r / ".git").mkdir(parents=True)
    return r


class TestWorktreesAreRecognized:
    def test_worktree_git_file_counts_as_a_repo(self, tmp_path):
        """THE regression: .git as a file was treated as 'not a repo'."""
        assert project_binding._is_git_repo(_worktree(tmp_path)) is True

    def test_plain_repo_still_recognized(self, tmp_path):
        assert project_binding._is_git_repo(_repo(tmp_path)) is True

    def test_non_repo_is_not_a_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert project_binding._is_git_repo(plain) is False

    def test_choose_binding_binds_a_worktree(self, tmp_path):
        """Previously returned None, so the server kept the wrong project."""
        wt = _worktree(tmp_path)
        assert project_binding.choose_binding(wt, None) == wt

    def test_binding_layers_agree_on_worktrees(self, tmp_path):
        """paths._discover_project_root already accepted worktrees; the two
        layers must not answer 'is this a project?' differently."""
        wt = _worktree(tmp_path)
        sub = wt / "src" / "deep"
        sub.mkdir(parents=True)

        assert paths._discover_project_root(sub) == wt
        assert project_binding._is_git_repo(wt) is True


class TestProvisionalCacheRefreshes:
    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        home = tmp_path / "global"
        (home / "projects").mkdir(parents=True)
        monkeypatch.setattr(paths, "get_global_home", lambda: home)
        monkeypatch.setattr(paths, "_get_git_remote_url", lambda p: None)
        paths.invalidate_data_dir_cache()
        yield
        paths.invalidate_data_dir_cache()

    def test_reresolves_when_a_store_appears_in_another_process(self, tmp_path):
        """THE regression: `codevira init` in a terminal while the server runs."""
        proj = tmp_path / "proj"
        proj.mkdir()

        first = paths._resolve_data_dir(proj)
        paths._data_dir_cache[proj] = first  # simulate the live server's cache
        assert not (first / "config.yaml").exists()  # rule-4 guess, provisional

        # Another process initializes the project in-repo.
        (proj / ".codevira").mkdir()
        (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")

        assert paths._is_provisional(proj, first) is True
        refreshed = paths._resolve_data_dir(proj)
        assert (
            refreshed == proj / ".codevira"
        ), "server kept using a directory the CLI never reads"

    def test_settled_resolution_is_not_reresolved(self, tmp_path):
        """A cache entry pointing at a REAL store must stay cached (the fast
        path exists for a reason — don't re-stat on every call)."""
        proj = tmp_path / "proj2"
        (proj / ".codevira").mkdir(parents=True)
        (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")

        resolved = paths._resolve_data_dir(proj)
        assert paths._is_provisional(proj, resolved) is False
