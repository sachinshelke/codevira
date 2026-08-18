"""
test_git_shared.py — v3.7.1: opt-in team-shared (git-committed) memory.

Two engineers on the SAME GitHub repo need to see each other's codevira
decisions. That requires ``.codevira/`` memory to stay COMMITTED — but the
default v3.7.1 behavior (fix E) untracks it to stop cross-project bleed. The
reconciliation is an explicit opt-in: ``codevira init --shared`` writes
``git_shared: true``, which

  1. keeps memory git-tracked (skips the anti-bleed untrack), and
  2. silences the doctor "committed memory" warning,

while the default (no flag) still untracks — so bleed stays fixed for every
project that did NOT opt in.

These tests pin BOTH halves: the opt-in must preserve tracking, and the
default must still untrack.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server import cli_init
from mcp_server import paths as paths_mod
from mcp_server.paths import git_tracked_memory_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def committed_repo(tmp_path, monkeypatch):
    """A git repo that committed .codevira/ memory, with codevira pinned to it
    and the global home redirected so all resolution stays in-repo/hermetic."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    cv = repo / ".codevira"
    cv.mkdir()
    (cv / "decisions.jsonl").write_text('{"id":"D1","decision":"team secret"}\n')
    (cv / "sessions.jsonl").write_text('{"session_id":"s1"}\n')
    (cv / "config.yaml").write_text("schema_version: 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init with committed memory")

    # Pin codevira to this repo and isolate the global home.
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(repo))
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: tmp_path / "global")
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()
    yield repo
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()


def _memory_tracked(repo: Path) -> bool:
    return ".codevira/decisions.jsonl" in git_tracked_memory_files(repo)


def test_init_shared_keeps_memory_tracked_and_sets_flag(committed_repo):
    """--shared must NOT untrack committed memory, and must persist the flag.
    FAILS before the gate (Step 8 untracked unconditionally)."""
    rc = cli_init.cmd_init(yes=True, shared=True)
    assert rc == 0
    # Memory is still committed → teammates inherit it.
    assert _memory_tracked(committed_repo), "shared init wrongly untracked memory"
    # Flag persisted so a later plain re-init won't undo it.
    cfg = (committed_repo / ".codevira" / "config.yaml").read_text()
    assert "git_shared: true" in cfg


def test_init_default_untracks_memory(committed_repo):
    """Default (no --shared) must still untrack — bleed stays fixed."""
    rc = cli_init.cmd_init(yes=True)
    assert rc == 0
    assert not _memory_tracked(committed_repo), "default init should untrack memory"


def test_init_respects_existing_git_shared_flag(committed_repo):
    """A plain re-init on a repo already marked git_shared must keep memory
    tracked (effective_shared reads the persisted flag, not just the arg)."""
    cfg_path = committed_repo / ".codevira" / "config.yaml"
    cfg_path.write_text("schema_version: 1\ngit_shared: true\n")
    _git(committed_repo, "add", "-A")
    _git(committed_repo, "commit", "-qm", "mark shared")

    rc = cli_init.cmd_init(yes=True)  # no --shared
    assert rc == 0
    assert _memory_tracked(committed_repo)


def test_doctor_committed_memory_silent_when_shared(committed_repo):
    """doctor's committed-memory check is a WARN by default but PASS when the
    repo opts into git_shared (committed memory is intentional there)."""
    from mcp_server.doctor import _PASS, _WARN, check_committed_memory

    # Default: tracked memory → WARN.
    assert check_committed_memory().state == _WARN

    # Opt in and the same tracked memory becomes PASS.
    (committed_repo / ".codevira" / "config.yaml").write_text(
        "schema_version: 1\ngit_shared: true\n"
    )
    assert check_committed_memory().state == _PASS


# ─── The .gitignore half of `--shared` (4.0.2) ─────────────────────────────
#
# The tests above call `cli_init.cmd_init(shared=True)` directly. The REAL CLI
# runs the legacy `cli.cmd_init()` scaffold FIRST, and that is what owns the
# `.codevira/` .gitignore entry — so a bug living there was invisible to every
# test in this file. `init --shared` wrote `.codevira/` into .gitignore, printed
# its own warning that this "defeats codevira's core promise", then reported
# "✓ Team mode ... memory stays committed" and told the user to run
# `git add .codevira/` — which silently adds nothing. Team sharing never worked.
#
# These go through the real `python -m mcp_server` entry point for that reason.


def _run_cli(repo: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke the real CLI against `repo` with a hermetic global home."""
    import os
    import sys

    env = {
        **os.environ,
        "CODEVIRA_HOME": str(tmp_path / "cli-global"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    return subprocess.run(
        [sys.executable, "-m", "mcp_server", "--project-dir", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def _dir_is_ignored(repo: Path) -> bool:
    """True if git would ignore the memory store (so `git add` no-ops)."""
    return (
        subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", ".codevira/decisions.jsonl"],
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.fixture
def bare_repo(tmp_path):
    """An empty git repo with no codevira state yet."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    return repo


def test_init_shared_leaves_memory_git_trackable(bare_repo, tmp_path):
    """`init --shared` must NOT gitignore .codevira/ — the whole point is that
    teammates receive the decision log. Fails before the 4.0.2 fix."""
    r = _run_cli(bare_repo, tmp_path, "init", "-y", "--shared", "--no-inject")
    assert r.returncode == 0, r.stderr
    assert not _dir_is_ignored(bare_repo), (
        "init --shared left .codevira/ ignored, so `git add .codevira/` adds "
        "nothing and no teammate ever receives the memory.\n"
        f".gitignore:\n{(bare_repo / '.gitignore').read_text()}"
    )
    # And the success banner must not claim team mode over a broken state.
    assert "✗ Team mode requested" not in r.stdout, r.stdout


def test_init_default_still_ignores_memory(bare_repo, tmp_path):
    """The default (no --shared) must keep ignoring .codevira/ — that is the
    anti-bleed behavior and the fix must not regress it."""
    r = _run_cli(bare_repo, tmp_path, "init", "-y", "--no-inject")
    assert r.returncode == 0, r.stderr
    assert _dir_is_ignored(bare_repo), "default init should keep .codevira/ ignored"


def test_init_shared_repairs_an_ignore_written_by_an_earlier_default_init(
    bare_repo, tmp_path
):
    """Upgrade path: a repo that ran a plain `init` first, then `init --shared`.
    The stale ignore line must be REMOVED, not merely warned about."""
    _run_cli(bare_repo, tmp_path, "init", "-y", "--no-inject")
    assert _dir_is_ignored(bare_repo), "precondition: default init ignores it"

    r = _run_cli(bare_repo, tmp_path, "init", "-y", "--shared", "--no-inject")
    assert r.returncode == 0, r.stderr
    assert not _dir_is_ignored(bare_repo), "init --shared must repair the stale ignore"
    # The rebuildable cache stays ignored — only the memory store is freed.
    assert ".codevira-cache/" in (bare_repo / ".gitignore").read_text()


class TestDerivedStateDoesNotConflictOnMerge:
    """Shared mode must not hand teammates a merge conflict in generated files.

    Measured with two branches each recording one decision, against 4.1.0:
    the merge driver resolved `.codevira/decisions.jsonl` correctly — the id
    collision was re-minted, both decisions survived, ids stayed unique — but
    `git merge` still exited 1 because `.codevira/digest.jsonl` and
    `.codevira/manifest.yaml` conflicted. Both are pure functions of
    decisions.jsonl, regenerated by `codevira sync`, so committing them
    guarantees a conflict on every concurrent write for content nobody needs
    to review.

    AGENTS.md is deliberately NOT covered here. It is derived too, but it is
    the contract other AI tools read WITHOUT running codevira, and this repo's
    own .gitignore documents keeping it committed. Ignoring it would break the
    cross-tool promise to save a merge conflict.
    """

    def _init_shared(self, root):
        import os
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        env = dict(os.environ, CODEVIRA_PROJECT_DIR=str(root))
        subprocess.run(
            ["codevira", "init", "--shared", "--no-inject", "-y"],
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
        )

    def test_shared_mode_ignores_regenerable_derived_state(self, tmp_path):
        root = tmp_path / "team"
        root.mkdir()
        self._init_shared(root)
        gi = (
            (root / ".gitignore").read_text() if (root / ".gitignore").is_file() else ""
        )
        for derived in (".codevira/digest.jsonl", ".codevira/manifest.yaml"):
            assert derived in gi, (
                f"{derived} is regenerated by `codevira sync`; committing it "
                f"makes every concurrent write a merge conflict.\n{gi}"
            )
        # The canonical log must NEVER be ignored in shared mode — that is the
        # whole point of team memory.
        assert ".codevira/decisions.jsonl" not in gi
        # AGENTS.md stays committed: other tools read it without codevira.
        assert "AGENTS.md" not in gi
