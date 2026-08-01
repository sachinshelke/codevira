"""4.0 Step 10 — $CODEVIRA_HOME, and why the test suite needed it.

`tests/conftest.py` has isolated the global home since 2026-06-16, using
`monkeypatch.setattr(paths, "get_global_home", ...)`. That works in-process
and not one step further: **18 test files spawn a `codevira` subprocess**,
and a child process inherits environment, not monkeypatches.

So every one of those tests wrote into the developer's real
`~/.codevira/global.db`. On the reference machine 371 of 387 rows in the
project registry were pytest temp directories, and the count grew during a
single suite run.

That is not only untidy. `codevira memory undo --all-projects` iterates the
registry, so a polluted registry means a rollback tool walking into pytest
temp dirs.

The override is also a feature in its own right: separate profiles,
containers with a read-only $HOME, CI runners that must not share state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import paths


#: Captured at MODULE IMPORT time, which pytest does at collection — before
#: the autouse `_isolate_global_home` fixture swaps in its lambda. Tests that
#: exercise the real resolution need the real function; every other test in
#: the suite still gets the isolating stub.
_REAL_GET_GLOBAL_HOME = paths.get_global_home
assert _REAL_GET_GLOBAL_HOME.__name__ == "get_global_home", (
    "conftest patched get_global_home before this module imported; these "
    "tests would silently exercise the stub instead of the shipped function"
)


@pytest.fixture
def real_resolution(monkeypatch: pytest.MonkeyPatch):
    """Undo the suite-wide stub for tests about resolution itself."""
    monkeypatch.setattr(paths, "get_global_home", _REAL_GET_GLOBAL_HOME)


def _child_global_home(env: dict[str, str]) -> str:
    """What a fresh process resolves — the thing a monkeypatch cannot reach."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mcp_server.paths import get_global_home; print(get_global_home())",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


class TestItCrossesTheProcessBoundary:
    """The property monkeypatch could not give us."""

    def test_a_subprocess_honours_the_override(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        env = {**os.environ, paths.GLOBAL_HOME_ENV: str(target)}
        assert _child_global_home(env) == str(target)

    def test_a_subprocess_without_it_uses_the_default(self, tmp_path: Path) -> None:
        env = {k: v for k, v in os.environ.items() if k != paths.GLOBAL_HOME_ENV}
        env["HOME"] = str(tmp_path)
        assert _child_global_home(env) == str(tmp_path / ".codevira")

    def test_the_suite_itself_is_isolated(self) -> None:
        """The regression that motivated this. If CODEVIRA_HOME is ever
        dropped from conftest, this fails and the suite stops silently
        writing into the developer's real registry."""
        assert os.environ.get(
            paths.GLOBAL_HOME_ENV
        ), "conftest must set CODEVIRA_HOME before any test runs"
        assert Path.home() / ".codevira" != Path(os.environ[paths.GLOBAL_HOME_ENV])

    def test_no_test_writes_to_the_real_registry(self) -> None:
        """Belt and braces: the resolved home must not be the real one even
        if something re-derives it mid-run."""
        resolved = paths.get_global_home()
        assert resolved != Path.home() / ".codevira"


class TestTheOverrideItself:
    def test_it_relocates_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_resolution
    ) -> None:
        target = tmp_path / "profile-a"
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, str(target))
        assert paths.get_global_home() == target
        assert target.is_dir(), "it must create the directory it points at"

    def test_the_db_follows_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_resolution
    ) -> None:
        target = tmp_path / "profile-b"
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, str(target))
        assert paths.get_global_db_path() == target / "global.db"

    def test_two_profiles_do_not_share_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_resolution
    ) -> None:
        """The point of the feature, not just the test fix."""
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, str(tmp_path / "a"))
        a = paths.get_global_db_path()
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, str(tmp_path / "b"))
        b = paths.get_global_db_path()
        assert a != b

    def test_tilde_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_resolution
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, "~/custom-cv")
        assert paths.get_global_home() == tmp_path / "custom-cv"


class TestItDegradesRatherThanBreaking:
    """This sits under nearly every code path. A typo in an env var must
    not make the tool unusable."""

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_override_falls_back(
        self,
        blank: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        real_resolution,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, blank)
        assert paths.get_global_home() == tmp_path / ".codevira"

    def test_an_unusable_path_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_resolution
    ) -> None:
        """Pointing at a path under an existing FILE cannot be created."""
        blocker = tmp_path / "iam-a-file"
        blocker.write_text("x")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(paths.GLOBAL_HOME_ENV, str(blocker / "nested"))
        assert paths.get_global_home() == tmp_path / ".codevira"
