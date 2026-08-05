"""The sdist must ship a test suite that is runnable, or none at all.

Left to itself, setuptools inherits distutils' legacy default of
`test*.py` in the test directory. That glob is non-recursive and does not
match `conftest.py`, so v4.0.0b1's sdist carried 96 of 167 test files:
the flat tests/ directory only.

What was missing is what makes the suite safe to run. `tests/conftest.py`
sets CODEVIRA_HOME to a temp dir and prepends the repo to PYTHONPATH.
Without it a downstream packager running the shipped suite writes into
their own ~/.codevira/ and imports site-packages instead of the tree they
are testing — the two bugs this release fixed for us, re-inflicted on
them. The gates a packager would most want (e2e/test_first_contact.py is
G2, integration/test_mcp_roundtrip.py is G1.5) were not shipped at all.

Building an sdist here is slower than asserting on MANIFEST.in text, and
that is the point: a manifest directive that parses is not evidence that
the archive contains the file.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Every directory that must survive packaging, with a named file whose
#: absence has a concrete consequence rather than a tidiness complaint.
REQUIRED = {
    "tests/conftest.py": "CODEVIRA_HOME + PYTHONPATH isolation for the whole suite",
    "tests/e2e/test_first_contact.py": "the G2 gate",
    "tests/integration/test_mcp_roundtrip.py": "the G1.5 gate",
}

REQUIRED_DIRS = ("tests/e2e/", "tests/engine/", "tests/integration/", "tests/storage/")


@pytest.fixture(scope="module")
def sdist_names(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """Build a real sdist and return its member paths, project-root-relative.

    `codevira.egg-info/SOURCES.txt` caches the resolved file list, and
    setuptools will reuse it rather than re-reading MANIFEST.in. That cache
    is not a detail — verifying this fix the first time, the tests passed
    against the OLD manifest because a previous build's SOURCES.txt was
    still on disk. Left in place it would let a broken manifest ship green
    forever. It regenerates on every build, so removing it costs nothing.
    """
    stale = REPO / "codevira.egg-info" / "SOURCES.txt"
    stale.unlink(missing_ok=True)

    out = tmp_path_factory.mktemp("sdist")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(out), str(REPO)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(f"sdist build failed:\n{(proc.stdout + proc.stderr)[-3000:]}")

    archives = list(out.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected one sdist, got {archives}"
    with tarfile.open(archives[0]) as tf:
        # Strip the leading `codevira-<version>/` component.
        return {n.split("/", 1)[1] for n in tf.getnames() if "/" in n}


class TestTheShippedSuiteIsRunnable:
    @pytest.mark.parametrize("path", sorted(REQUIRED))
    def test_load_bearing_file_ships(self, sdist_names: set[str], path: str) -> None:
        assert path in sdist_names, f"{path} missing from sdist — {REQUIRED[path]}"

    @pytest.mark.parametrize("directory", REQUIRED_DIRS)
    def test_subdirectory_ships(self, sdist_names: set[str], directory: str) -> None:
        """The legacy glob is non-recursive, so subdirectories vanish whole."""
        found = [n for n in sdist_names if n.startswith(directory)]
        assert found, f"no files under {directory} in sdist"

    def test_ships_the_whole_tree_not_a_subset(self, sdist_names: set[str]) -> None:
        """A count, so a future narrowing of the glob cannot pass quietly."""
        on_disk = {
            str(p.relative_to(REPO))
            for p in (REPO / "tests").rglob("*.py")
            if "__pycache__" not in p.parts
        }
        shipped = {
            n for n in sdist_names if n.startswith("tests/") and n.endswith(".py")
        }
        missing = on_disk - shipped
        assert (
            not missing
        ), f"{len(missing)} test file(s) not shipped, e.g. {sorted(missing)[:5]}"


class TestPackagingHygiene:
    def test_no_bytecode_ships(self, sdist_names: set[str]) -> None:
        """`recursive-include tests/e2e/fixtures *` would sweep in __pycache__."""
        junk = [n for n in sdist_names if "__pycache__" in n or n.endswith(".pyc")]
        assert not junk, f"bytecode in sdist: {junk[:5]}"

    def test_e2e_fixtures_keep_their_non_python_files(
        self, sdist_names: set[str]
    ) -> None:
        """The language-detection fixtures are .md/.json/.ts/.yaml — a
        `*.py`-only rule ships the tests without what they read."""
        fixtures = [n for n in sdist_names if n.startswith("tests/e2e/fixtures/")]
        assert any(n.endswith(".json") for n in fixtures), fixtures[:10]
        assert any(n.endswith(".md") for n in fixtures), fixtures[:10]
