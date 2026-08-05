"""4.0 Step 8 — enforce at the commit boundary, which every IDE crosses.

Hard enforcement has always been an IDE hook, and README.md:99-101 admits
the consequence: only Claude Code's PreToolUse hard-blocks, because only
its edits route through the engine. Cursor, Codex, Copilot and Gemini get
advisory context the agent may decline to read — 2 of ~7 surfaces.

You do not close that by asking six vendors for a hook contract. They all
commit with git. This adapter feeds `git diff --cached` through the SAME
dispatch() the IDE hooks use, so a locked decision is enforced in editors
that do not exist yet.

The tests that matter here are the ones about NOT blocking: a memory tool
that wedges someone's commit on its own bug has done more damage than the
decision it was protecting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.engine.wiring import git_hooks


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "src" / "cache.py").write_text("def get(k):\n    return CACHE.get(k)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    monkeypatch.chdir(root)
    monkeypatch.delenv("CODEVIRA_GIT_HOOK_MODE", raising=False)
    monkeypatch.delenv("CODEVIRA_DECISION_LOCK_MODE", raising=False)

    from mcp_server.storage import decisions_store, paths

    paths.ensure_dirs(root)
    (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    decisions_store.invalidate_merged_cache()
    decisions_store.record(
        "Do not add a cache layer in front of the invalidation path.",
        context="Deploys served stale reads for 90s; invalidation races rollout.",
        file_path="src/cache.py",
        do_not_revert=True,
    )
    return root


class TestBlocksAConflictingCommit:
    def test_locked_decision_blocks(self, repo: Path) -> None:
        """The whole point: no IDE hook is installed here."""
        (repo / "src" / "cache.py").write_text(
            "def get(k):\n    return _memo_cache.get(k)  # add a cache layer\n"
        )
        _git(repo, "add", "-A")
        assert git_hooks.handle(repo) == 1

    def test_the_refusal_carries_the_reasoning(self, repo: Path) -> None:
        (repo / "src" / "cache.py").write_text(
            "def get(k):\n    return _memo_cache.get(k)  # add a cache layer\n"
        )
        _git(repo, "add", "-A")
        verdicts = git_hooks.evaluate(repo)
        assert verdicts
        blob = " ".join(v.message or "" for v in verdicts)
        assert "stale reads" in blob, "Step 2.2's evidence must reach this surface too"


class TestDoesNotBlockWhatItShouldNot:
    """Each of these is a way a commit hook earns permanent distrust."""

    def test_nothing_staged_allows(self, repo: Path) -> None:
        assert git_hooks.handle(repo) == 0

    def test_unrelated_file_allows(self, repo: Path) -> None:
        (repo / "src" / "other.py").write_text("def unrelated():\n    return 1\n")
        _git(repo, "add", "-A")
        assert git_hooks.handle(repo) == 0

    def test_merge_commits_are_never_blocked(self, repo: Path) -> None:
        """Blocking a merge leaves the user mid-merge with no good move."""
        (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        (repo / "src" / "cache.py").write_text(
            "def get(k):\n    return _memo_cache.get(k)  # add a cache layer\n"
        )
        _git(repo, "add", "-A")
        assert git_hooks.evaluate(repo) == []
        assert git_hooks.handle(repo) == 0

    def test_warn_mode_reports_without_blocking(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_GIT_HOOK_MODE", "warn")
        (repo / "src" / "cache.py").write_text(
            "def get(k):\n    return _memo_cache.get(k)  # add a cache layer\n"
        )
        _git(repo, "add", "-A")
        assert git_hooks.handle(repo) == 0

    def test_off_mode_short_circuits(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_GIT_HOOK_MODE", "off")
        (repo / "src" / "cache.py").write_text(
            "def get(k):\n    return _memo_cache.get(k)  # add a cache layer\n"
        )
        _git(repo, "add", "-A")
        assert git_hooks.handle(repo) == 0

    def test_an_internal_error_allows_the_commit(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail open. A memory tool that wedges a commit on its own bug has
        done more damage than the decision it was protecting."""
        monkeypatch.setattr(
            git_hooks,
            "staged_files",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert git_hooks.evaluate(repo) == []
        assert git_hooks.handle(repo) == 0

    def test_non_git_directory_does_not_crash(self, tmp_path: Path) -> None:
        assert git_hooks.handle(tmp_path) == 0


class TestBounds:
    def test_file_scan_is_capped(self, repo: Path) -> None:
        """A 300-file refactor must not spawn 300 evaluations in a hook."""
        for i in range(60):
            (repo / "src" / f"f{i}.py").write_text(f"x = {i}\n")
        _git(repo, "add", "-A")
        files, _ = git_hooks.staged_files(repo)
        assert len(files) <= git_hooks._MAX_FILES

    def test_the_cap_reports_what_it_dropped(
        self, repo: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A silent cap makes a truncated run indistinguishable from a clean
        one — so a locked file sorting past #40 reads as 'nothing locked'."""
        for i in range(60):
            (repo / "src" / f"f{i:03d}.py").write_text(f"x = {i}\n")
        _git(repo, "add", "-A")

        files, dropped = git_hooks.staged_files(repo)
        assert dropped > 0, "expected the cap to drop files in this fixture"
        assert len(files) == git_hooks._MAX_FILES

        assert git_hooks.handle(repo) == 0
        err = capsys.readouterr().err
        assert (
            "NOT evaluated" in err and str(dropped) in err
        ), f"the hook must say how many staged files it skipped; got: {err!r}"

    def test_binary_and_unknown_suffixes_are_skipped(self, repo: Path) -> None:
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        _git(repo, "add", "-A")
        files, _ = git_hooks.staged_files(repo)
        assert not any(f.endswith(".bin") for f in files)

    @pytest.mark.parametrize(
        "name", ["Auth.java", "Service.cs", "handler.rb", "main.cpp", "app.php"]
    )
    def test_common_languages_reach_the_engine(self, repo: Path, name: str) -> None:
        """The allowlist started as 'what the graph parses', which made
        commit-time enforcement a silent no-op for most of the world."""
        (repo / "src" / name).write_text("// x\n")
        _git(repo, "add", "-A")
        files, _ = git_hooks.staged_files(repo)
        assert any(
            f.endswith(name) for f in files
        ), f"{name} was filtered out before any policy could see it"


class TestInstaller:
    def test_creates_an_executable_hook(self, repo: Path) -> None:
        assert git_hooks.install_hook(repo) == 0
        hook = repo / ".git" / "hooks" / "pre-commit"
        assert hook.is_file()
        assert hook.stat().st_mode & 0o111, "hook must be executable"
        assert git_hooks._MARKER in hook.read_text()

    def test_is_idempotent(self, repo: Path) -> None:
        git_hooks.install_hook(repo)
        first = (repo / ".git" / "hooks" / "pre-commit").read_text()
        git_hooks.install_hook(repo)
        assert (repo / ".git" / "hooks" / "pre-commit").read_text() == first

    def test_preserves_someone_elses_hook(self, repo: Path) -> None:
        """Another tool's pre-commit is not ours to delete."""
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'existing linter'\n")
        git_hooks.install_hook(repo)
        content = hook.read_text()
        assert "existing linter" in content
        assert git_hooks._MARKER in content

    def test_refuses_outside_a_repo(self, tmp_path: Path) -> None:
        assert git_hooks.install_hook(tmp_path) == 1


class TestWorktreeLayout:
    """`.git` is a FILE in a linked worktree and in a submodule, not a dir.

    Both `is_merge_commit` and `install_hook` hard-coded `<root>/.git/...`,
    so in a worktree the never-block-a-merge guarantee silently did not
    hold and the installer refused to run with a false "not a git
    repository". This repo does routine work in worktrees, so neither was
    a hypothetical edge case.
    """

    @pytest.fixture
    def worktree(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        wt = repo.parent / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
        assert (wt / ".git").is_file(), "fixture assumption: .git is a file here"
        monkeypatch.chdir(wt)
        return wt

    def test_git_path_resolves_through_the_gitdir_pointer(self, worktree: Path) -> None:
        p = git_hooks.git_path(worktree, "MERGE_HEAD")
        assert p is not None
        assert ".git" in str(p), p
        assert not p.exists(), "no merge in progress"

    def test_a_merge_in_a_worktree_is_still_exempt(self, worktree: Path) -> None:
        """The naive `<root>/.git/MERGE_HEAD` probe returns False here, which
        would run the full policy set over a merge and can block it."""
        gitdir = git_hooks.git_path(worktree, "MERGE_HEAD")
        assert gitdir is not None
        gitdir.parent.mkdir(parents=True, exist_ok=True)
        gitdir.write_text("deadbeef\n")

        assert git_hooks.is_merge_commit(worktree) is True
        assert git_hooks.evaluate(worktree) == []

    def test_install_hook_works_in_a_worktree(self, worktree: Path) -> None:
        """Pre-fix this printed 'not a git repository' and returned 1."""
        assert git_hooks.install_hook(worktree) == 0
        # Hooks are shared across worktrees — they live in the COMMON git dir.
        hook = Path(
            subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        if not hook.is_absolute():
            hook = worktree / hook
        hook = hook / "hooks" / "pre-commit"
        assert hook.is_file()
        assert git_hooks._MARKER in hook.read_text()
