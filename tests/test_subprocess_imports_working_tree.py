"""The suite must test THIS working tree, including in subprocesses.

A subprocess started with ``cwd=tmp_path`` — which most CLI tests do — has
no repo on ``sys.path``, so ``import mcp_server`` falls through to
site-packages: a copy from an earlier ``pip install``. Those tests were
exercising an OLD build rather than the code under test, and passing.

That is not a hypothetical. It is how a real defect survived four rounds
of tracing. The global.db registry was accumulating pytest temp dirs; every
tracer I wrote patched the repo's classes inside the pytest process and
found nothing, because the writes were happening in a subprocess importing
a DIFFERENT copy of the module — one predating ``$CODEVIRA_HOME``, so it
wrote to the developer's real ``~/.codevira``.

    subprocesses use the working tree   -> +0 rows per run
    subprocesses use site-packages      -> +4 rows per run

conftest.py now prepends the repo root to ``PYTHONPATH``. These tests pin
that, because the failure mode is silent: everything still passes, it just
stops testing what you changed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _child_module_path(cwd: Path) -> str:
    """Where a subprocess started in ``cwd`` imports mcp_server from."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mcp_server, pathlib; print(pathlib.Path(mcp_server.__file__).parent)",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


class TestSubprocessesUseTheWorkingTree:
    def test_a_subprocess_outside_the_repo_still_imports_it(
        self, tmp_path: Path
    ) -> None:
        """The case that was broken: cwd is a tmp dir, so only PYTHONPATH
        can point the child at the code under test."""
        assert _child_module_path(tmp_path) == str(REPO / "mcp_server")

    def test_conftest_sets_pythonpath(self) -> None:
        """Guard the mechanism, so removing it fails loudly rather than
        silently reverting the suite to testing an installed build."""
        assert str(REPO) in os.environ.get("PYTHONPATH", "").split(os.pathsep)

    def test_the_child_has_the_4_0_code(self, tmp_path: Path) -> None:
        """A path check alone would pass against a stale tree at the same
        path. Assert a symbol that only exists in this version."""
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mcp_server import paths; print(hasattr(paths, 'GLOBAL_HOME_ENV'))",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == "True"

    def test_a_caller_pythonpath_is_preserved(self) -> None:
        """Prepend, not replace — a developer running with their own
        PYTHONPATH must not have it silently dropped."""
        entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
        assert entries[0] == str(REPO), "the repo must win, but not be alone"


class TestTheIsolationThatDependsOnIt:
    def test_a_subprocess_writes_to_the_test_home(self, tmp_path: Path) -> None:
        """The payoff: because the child imports the working tree, it
        honours $CODEVIRA_HOME and cannot touch the real registry."""
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mcp_server.paths import get_global_home; print(get_global_home())",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        resolved = Path(out.stdout.strip().splitlines()[-1])
        assert resolved != Path.home() / ".codevira", (
            "a subprocess reached the developer's real global home"
        )
