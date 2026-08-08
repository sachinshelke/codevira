"""`clean` was removed; `prune` is the tidy-up, `uninstall` the remover. D00012X.

`codevira clean` with no flags wiped ~/.codevira/, stripped codevira from
every IDE config, and removed the launchd service — while its name read as
tidy-up. On 2026-08-01 an agent ran `yes | codevira clean` intending to prune
stale registry rows and destroyed a real installation (project memory survived
only because it lives in-repo, not in ~/.codevira/).

4.0.1 removes the trap entirely (D00013K): the SAFE operations that used to be
hidden behind `clean --orphans/--ghosts/--legacy` are the `codevira prune`
command, the destructive full-uninstall is `codevira uninstall`, and
`codevira clean` no longer exists — it errors with `invalid choice: 'clean'`.

The load-bearing tests here are `TestCleanIsRemoved` (the trap is gone) and
`TestPruneIsSafe` (the safe half never uninstalls).
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


class TestCleanIsRemoved:
    """The incident's real fix: the command is gone, so it can never run."""

    def test_clean_is_an_invalid_choice(self, fake_home: Path) -> None:
        """`codevira clean` (even piped `yes`) must ABORT as unknown — it can
        no longer uninstall anything."""
        r = _run(["clean"], fake_home, stdin="y\n" * 50)
        assert r.returncode != 0, "`clean` should be an unknown command now"
        assert "invalid choice: 'clean'" in r.stderr, r.stderr
        assert fake_home.is_dir(), "the data dir must be untouched"
        assert (fake_home / "global.db").exists()

    def test_clean_with_flags_also_gone(self, fake_home: Path) -> None:
        """The old safe modes moved to `prune`; `clean --orphans` is gone too."""
        r = _run(["clean", "--orphans"], fake_home)
        assert r.returncode != 0
        assert "invalid choice: 'clean'" in r.stderr


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
        """`clean` stripped IDE configs and hooks. `prune` must not — that is
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
