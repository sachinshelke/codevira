"""
test_worktree_shared_memory.py — D000130.

An engineer working in a git worktree must share ONE `.codevira/` memory store
with the main checkout, otherwise decisions made in the worktree land in a
separate store that can't be merged back (the worktree fragmentation Sachin
hit). `codevira_dir()` redirects a linked worktree's memory to the MAIN
worktree root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import paths as sp


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo_with_worktree(tmp_path):
    """A real git repo (main checkout + one linked worktree)."""
    main = tmp_path / "repo"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@t.co")
    _git(main, "config", "user.name", "t")
    (main / ".gitignore").write_text(".codevira/\n")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-qm", "init")
    (main / ".codevira").mkdir()
    (main / ".codevira" / "decisions.jsonl").write_text('{"id":"D1"}\n')
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "feature")
    return main, wt


class TestWorktreeSharedMemory:
    def test_worktree_memory_redirects_to_main(self, repo_with_worktree):
        main, wt = repo_with_worktree
        # a worktree's .git is a FILE, so its memory must resolve to main's store
        assert (wt / ".git").is_file()
        assert sp.codevira_dir(wt).resolve() == (main / ".codevira").resolve()

    def test_main_checkout_unchanged(self, repo_with_worktree):
        main, _wt = repo_with_worktree
        # normal checkout: .git is a dir → no redirect
        assert (main / ".git").is_dir()
        assert sp.codevira_dir(main) == main / ".codevira"

    def test_decisions_path_follows_redirect(self, repo_with_worktree):
        main, wt = repo_with_worktree
        assert (
            sp.decisions_path(wt).resolve()
            == (main / ".codevira" / "decisions.jsonl").resolve()
        )

    def test_isolated_env_opts_out(self, repo_with_worktree, monkeypatch):
        main, wt = repo_with_worktree
        monkeypatch.setenv("CODEVIRA_WORKTREE_ISOLATED", "1")
        # opt-out: worktree keeps its OWN store
        assert sp.codevira_dir(wt) == wt / ".codevira"

    def test_non_repo_dir_unchanged(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert sp._main_worktree_root(plain) == plain

    def test_git_file_pointing_at_invalid_root_falls_back(self, tmp_path, monkeypatch):
        """Honors D000012: if the derived main root is an invalid project root,
        fall back to the worktree rather than write memory somewhere bad."""
        wt = tmp_path / "wt"
        wt.mkdir()
        # forge a worktree .git file whose main root would resolve to $HOME
        home = Path.home()
        (wt / ".git").write_text(f"gitdir: {home}/.git/worktrees/x\n")
        monkeypatch.setattr(sp, "is_invalid_project_root", lambda p: p == home)
        assert sp._main_worktree_root(wt) == wt  # fell back, did not use $HOME


# ---------------------------------------------------------------------------
# Migration: fold a PRE-EXISTING worktree-local .codevira into the main store
# (so the D00012A redirect never orphans decisions written before the upgrade)
# ---------------------------------------------------------------------------


class TestWorktreeMemoryMergeMigration:
    def _read_ids(self, jsonl: Path) -> set[str]:
        import json

        return {
            json.loads(ln)["id"] for ln in jsonl.read_text().splitlines() if ln.strip()
        }

    def test_folds_worktree_decisions_into_main_nondestructively(
        self, repo_with_worktree
    ):
        from mcp_server import migrate

        main, wt = repo_with_worktree
        # main already has D1 (from the fixture). Simulate a PRE-FIX worktree
        # that wrote its own D2 into its own local .codevira/.
        (wt / ".codevira").mkdir()
        (wt / ".codevira" / "decisions.jsonl").write_text(
            '{"id":"D2","decision":"made in worktree"}\n'
        )

        assert migrate._mig_v380_worktree_memory_merge(wt) is True

        # main store now holds BOTH decisions (union)
        main_ids = self._read_ids(main / ".codevira" / "decisions.jsonl")
        assert {"D1", "D2"} <= main_ids
        # non-destructive: worktree store renamed to a premerge backup, not deleted
        assert not (wt / ".codevira").exists()
        backups = list(wt.glob(".codevira.premerge-*"))
        assert backups and (backups[0] / "decisions.jsonl").exists()

    def test_noop_on_main_checkout(self, repo_with_worktree):
        from mcp_server import migrate

        main, _wt = repo_with_worktree
        assert migrate._mig_v380_worktree_memory_merge(main) is False

    def test_noop_when_worktree_has_no_local_store(self, repo_with_worktree):
        from mcp_server import migrate

        _main, wt = repo_with_worktree
        # worktree has no OWN .codevira (the post-fix normal state)
        assert not (wt / ".codevira").exists()
        assert migrate._mig_v380_worktree_memory_merge(wt) is False

    def test_dedups_shared_decision(self, repo_with_worktree):
        from mcp_server import migrate

        main, wt = repo_with_worktree
        # both stores contain the SAME D1 record → union must not duplicate it
        (wt / ".codevira").mkdir()
        (wt / ".codevira" / "decisions.jsonl").write_text('{"id":"D1"}\n{"id":"D9"}\n')
        migrate._mig_v380_worktree_memory_merge(wt)
        lines = [
            ln
            for ln in (main / ".codevira" / "decisions.jsonl").read_text().splitlines()
            if '"id":"D1"' in ln.replace(" ", "")
        ]
        assert len(lines) == 1, "D1 must appear once after union, not duplicated"
