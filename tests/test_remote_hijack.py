"""
test_remote_hijack.py — v3.7.1: a shared git remote must not route a project to
another project's store.

`get_data_dir()` governs the code graph, semantic index and config (decision
memory is in-repo, see storage/paths.py). So a misroute here makes get_impact /
query_graph / search_codebase answer about a DIFFERENT project's code.

Two defects, both reproduced from a real setup:

  1. The git-remote lookup ran BEFORE the in-repo check, so a project with its
     own `codevira init`-created .codevira/config.yaml was still routed to
     whichever centralized dir shared its `origin` — i.e. any clone, fork or
     second checkout. This is the "populated store loses to another project's"
     case, and it is exactly the shape a 2-engineer shared repo has.

  2. The lookup matched on metadata.json ALONE, so a GHOST dir (metadata from a
     scan, no config.yaml, no real store) out-ranked a genuinely initialized
     project; and glob() order made the winner vary between runs and machines.
"""

from __future__ import annotations

import json

import pytest

import mcp_server.paths as paths


@pytest.fixture
def global_home(tmp_path, monkeypatch):
    home = tmp_path / "global"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setattr(paths, "get_global_home", lambda: home)
    paths.invalidate_data_dir_cache()
    yield home
    paths.invalidate_data_dir_cache()


def _central(home, name, *, remote, with_config=True):
    """Create a centralized store dir, optionally a ghost (no config.yaml)."""
    d = home / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        json.dumps(
            {"path_key": name, "git_remote": remote, "original_path": f"/x/{name}"}
        )
    )
    if with_config:
        (d / "config.yaml").write_text("schema_version: 1\n")
    return d


def _project(tmp_path, name, *, initialized=True):
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    if initialized:
        (p / ".codevira").mkdir()
        (p / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    return p


REMOTE = "git@github.com:acme/shared.git"


class TestInitializedProjectWins:
    def test_own_store_beats_a_remote_sibling(self, tmp_path, global_home, monkeypatch):
        """THE regression: B is initialized locally but shares A's remote."""
        _central(global_home, "project_A", remote=REMOTE)
        b = _project(tmp_path, "B")
        monkeypatch.setattr(paths, "_get_git_remote_url", lambda p: REMOTE)

        resolved = paths._resolve_data_dir(b)

        assert (
            resolved == b / ".codevira"
        ), "explicitly-initialized project was routed to a sibling's store"

    def test_remote_lookup_still_survives_a_rename(
        self, tmp_path, global_home, monkeypatch
    ):
        """The remote lookup keeps its real purpose: a project with NO in-repo
        marker (e.g. renamed directory -> new path key) still finds its store."""
        a = _central(global_home, "project_A", remote=REMOTE)
        moved = _project(tmp_path, "moved", initialized=False)
        monkeypatch.setattr(paths, "_get_git_remote_url", lambda p: REMOTE)

        assert paths._resolve_data_dir(moved) == a


class TestGhostDirsNeverWin:
    def test_ghost_without_config_is_ignored(self, tmp_path, global_home, monkeypatch):
        """A metadata-only dir is not a store — it must not be matched."""
        _central(global_home, "ghost", remote=REMOTE, with_config=False)
        proj = _project(tmp_path, "P", initialized=False)
        monkeypatch.setattr(paths, "_get_git_remote_url", lambda p: REMOTE)

        # No real match -> falls through to the default centralized path,
        # NOT the ghost.
        resolved = paths._resolve_data_dir(proj)
        assert resolved != global_home / "projects" / "ghost"

    def test_real_store_beats_ghost_regardless_of_order(
        self, tmp_path, global_home, monkeypatch
    ):
        # "aaa_ghost" sorts first, so a first-match-wins scan would pick it.
        _central(global_home, "aaa_ghost", remote=REMOTE, with_config=False)
        real = _central(global_home, "zzz_real", remote=REMOTE)

        assert paths._find_project_by_git_remote(REMOTE) == real


class TestDeterminism:
    def test_multiple_matches_resolve_stably(self, global_home):
        """Clones share a remote; the winner must not vary between runs."""
        _central(global_home, "clone_b", remote=REMOTE)
        _central(global_home, "clone_a", remote=REMOTE)

        results = {paths._find_project_by_git_remote(REMOTE) for _ in range(5)}
        assert len(results) == 1, "resolution is nondeterministic across runs"
        assert results.pop().name == "clone_a", "expected stable sorted order"

    def test_empty_remote_never_matches(self, global_home):
        """v3.7.1 fix E — a null remote must not match the many null-remote
        projects (kept as a regression guard)."""
        _central(global_home, "no_remote", remote=None)
        assert paths._find_project_by_git_remote(None) is None
        assert paths._find_project_by_git_remote("") is None
