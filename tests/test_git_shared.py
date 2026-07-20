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
