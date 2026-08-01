"""`clean` is the uninstaller; `prune` is the tidy-up. See D00012X.

`codevira clean` with no flags wipes ~/.codevira/, strips codevira from
every IDE config, and removes the launchd service. The name reads as
tidy-up. On 2026-08-01 an agent ran `yes | codevira clean` intending to
prune stale registry rows and destroyed a real installation. Project
memory survived only because it lives in-repo, not in ~/.codevira/.

Two things were wrong and both are fixed here:

1. The SAFE operations were hidden behind flags (`clean --orphans`,
   `--ghosts`, `--legacy`) while the DESTRUCTIVE one was the bare
   default. `prune` promotes the safe half to its own name.

2. The destructive path used the plain y/n `confirm()`, so a piped "y"
   answered it. `codevira reset` has required a TYPED confirmation since
   v2.1.2 for exactly this reason; `clean` now does too.

The load-bearing test in this file is `test_a_piped_yes_cannot_confirm`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run(args: list[str], home: Path, stdin: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "CODEVIRA_HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "mcp_server.cli", *args],
        cwd=REPO,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "cvhome"
    (home / "projects" / "demo").mkdir(parents=True)
    (home / "global.db").write_text("")
    return home


class TestAPipedYesCannotWipe:
    """The incident, as a test."""

    def test_a_piped_yes_cannot_confirm(self, fake_home: Path) -> None:
        """`yes | codevira clean` must ABORT, not uninstall."""
        r = _run(["clean"], fake_home, stdin="y\n" * 50)
        assert fake_home.is_dir(), "a piped 'y' still wiped the data dir"
        assert "aborted" in (r.stdout + r.stderr).lower()

    def test_the_prompt_demands_a_typed_word(self, fake_home: Path) -> None:
        r = _run(["clean"], fake_home, stdin="y\n")
        assert "Type 'uninstall'" in r.stdout + r.stderr

    def test_typing_the_word_is_still_possible(self, fake_home: Path) -> None:
        """The guard must not make deliberate uninstall impossible."""
        r = _run(["clean"], fake_home, stdin="uninstall\n")
        out = (r.stdout + r.stderr).lower()
        assert "aborted" not in out or "removed" in out

    def test_explicit_yes_flag_still_works_for_scripts(self, fake_home: Path) -> None:
        """--yes is the documented scripted path and must keep working —
        the fix targets ACCIDENTAL confirmation, not automation."""
        r = _run(["clean", "--yes"], fake_home)
        assert r.returncode == 0
        assert not fake_home.exists() or not (fake_home / "projects").exists()


class TestCleanSaysWhatItIs:
    def test_bare_clean_warns_it_is_an_uninstall(self, fake_home: Path) -> None:
        r = _run(["clean"], fake_home, stdin="n\n")
        blob = r.stdout + r.stderr
        assert "DEPRECATED" in blob
        assert "UNINSTALL" in blob

    def test_the_warning_names_what_it_deletes(self, fake_home: Path) -> None:
        """A warning that does not say what is lost is decoration."""
        blob = _run(["clean"], fake_home, stdin="n\n").stderr
        for token in ("~/.codevira/", "global.db", "snapshots"):
            assert token in blob, token

    def test_the_warning_points_at_the_alternatives(self, fake_home: Path) -> None:
        blob = _run(["clean"], fake_home, stdin="n\n").stderr
        assert "codevira prune" in blob
        assert "codevira uninstall" in blob

    def test_selective_clean_points_at_prune_instead(self, fake_home: Path) -> None:
        """`clean --orphans` was always safe; it should redirect, not shout."""
        blob = _run(["clean", "--orphans", "--yes"], fake_home).stderr
        assert "codevira prune" in blob
        assert "DEPRECATED" not in blob

    def test_help_does_not_call_it_a_cleanup(self) -> None:
        """The help text is where the next person forms their model."""
        r = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "clean", "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert "DEPRECATED" in r.stdout
        assert "UNINSTALL" in r.stdout.upper()


class TestPruneIsSafe:
    def test_prune_exists(self) -> None:
        r = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "prune", "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert "--dry-run" in r.stdout

    def test_prune_never_removes_the_data_dir(self, fake_home: Path) -> None:
        """The whole point: tidying must not uninstall."""
        _run(["prune", "--yes"], fake_home)
        assert fake_home.is_dir()
        assert (fake_home / "global.db").exists()

    def test_prune_dry_run_changes_nothing(self, fake_home: Path) -> None:
        before = sorted(p.name for p in fake_home.rglob("*"))
        _run(["prune", "--dry-run"], fake_home)
        assert sorted(p.name for p in fake_home.rglob("*")) == before

    def test_prune_does_not_touch_ide_config_or_hooks(self, fake_home: Path) -> None:
        """`clean` strips IDE configs and hooks. `prune` must not — that is
        the entire distinction between the two commands."""
        r = _run(["prune", "--yes"], fake_home)
        blob = (r.stdout + r.stderr).lower()
        for forbidden in ("mcpservers", "hooks", "launchd", "settings.json"):
            assert forbidden not in blob, forbidden

    @pytest.mark.parametrize("flag", ["--orphans", "--ghosts", "--legacy"])
    def test_each_selective_mode_runs(self, fake_home: Path, flag: str) -> None:
        r = _run(["prune", flag, "--dry-run"], fake_home)
        assert r.returncode == 0, r.stderr
        assert fake_home.is_dir()
