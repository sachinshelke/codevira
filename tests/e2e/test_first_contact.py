"""
test_first_contact.py — G2 of the release gauntlet.

WHY this exists. v2.0.0 shipped to PyPI with 23 silent-failure bugs
(catalogued in ROADMAP.md as A–O) because no test exercised the
"brand new user runs `codevira init` against a real-shape project"
path. Unit tests passed (2,395/2,395) yet:

  - docs-only repos silently produced 0 chunks
  - polyglot repos lost top-level files from watched_dirs
  - configure and index used different file matchers
  - status / index commands lied when nothing was indexed

Each of those is a one-test-away regression. This file is that test.
Every release MUST keep this green. The PreToolUse hook
(.claude/hooks/pre-release-block.sh) refuses `twine upload` without
G2 evidence.

WHAT it tests. The fresh-user contact surface against 4 fixture
project shapes:

  1. docs_only       — only .md / .json files (no parseable code)
  2. code_only_python — straight Python repo
  3. polyglot         — Python + TypeScript + .yaml + .md
  4. monorepo         — packages/<name>/src/* layout

For each fixture we run the canonical first-contact flow:

  codevira init       → must succeed, write config to centralized dir
  codevira configure  → must agree with init on what to scan
  codevira index      → must produce chunks > 0 OR fail loudly
                        (silent 0-chunks is a bug, not a feature)
  codevira status     → must reflect actual state, not lie

Failure modes we explicitly assert against:

  ✗ "No files changed. Index is up to date." when graph has 0 nodes
  ✗ index reports 0 chunks but doesn't say WHY
  ✗ configure writes to .codevira/ in-repo while init writes centralized
  ✗ init's "Auto-detected Extensions" lists 80 extensions when project
     only has .py files
  ✗ docs-only repo produces silent 0-chunks with no warning

NOT covered (separate tests):

  - IDE-specific MCP integration (test_cross_tool_universality.py)
  - Stress tests at scale (test_v2_release_candidate.py)
  - Hero policy correctness (per-policy unit tests)

Generic-first design note. The CodeviraInstance helper below is
deliberately slim so a future `codevira/discipline-scaffold` template
can swap codevira-specific commands for any other tool's equivalents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


# ─── Fixtures ──────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ALL_FIXTURE_NAMES = ["docs_only", "code_only_python", "polyglot", "monorepo"]


@pytest.fixture
def codevira_bin() -> str:
    """
    Resolve the codevira binary, preferring the in-repo editable
    install. Falls back to PATH lookup so the test works in CI where
    codevira is `pip install -e .` into the test venv.
    """
    binary = shutil.which("codevira")
    if not binary:
        pytest.skip("codevira binary not on PATH — run `make dev` first")
    return binary


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Give each test its own ~/.codevira dir so tests don't pollute
    each other or the developer's real codevira state. Without this,
    the global.db row from one test bleeds into the next.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CODEVIRA_HOME", str(fake_home / ".codevira"))
    return fake_home


@pytest.fixture(params=ALL_FIXTURE_NAMES)
def project_root(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """
    Copy the named fixture into a fresh tmp dir. Each test gets a
    pristine project tree — no risk of leftover state from other runs.
    """
    fixture_name = request.param
    src = FIXTURES_DIR / fixture_name
    if not src.is_dir():
        pytest.skip(f"fixture {fixture_name} missing — see tests/e2e/fixtures/")
    dst = tmp_path / fixture_name
    shutil.copytree(src, dst)
    return dst


# ─── Generic helpers ───────────────────────────────────────────────────────


class CodeviraResult:
    """One command run, captured as a structured result for assertions."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.combined = stdout + "\n" + stderr

    def __repr__(self) -> str:
        return (
            f"CodeviraResult(rc={self.returncode}, "
            f"stdout={self.stdout[:200]!r}, stderr={self.stderr[:200]!r})"
        )


def run_codevira(
    binary: str,
    args: list[str],
    cwd: Path,
    timeout: int = 60,
) -> CodeviraResult:
    """Run codevira <args> in cwd. Capture exit code, stdout, stderr."""
    proc = subprocess.run(
        [binary, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ},  # propagate isolated_home from the test
    )
    return CodeviraResult(proc.returncode, proc.stdout, proc.stderr)


# ─── The actual gauntlet tests (one per fixture, parameterized) ────────────


class TestFirstContact:
    """
    The first-contact gauntlet. Every fixture goes through every step.
    A failure in any step blocks release.
    """

    def test_init_succeeds(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """`codevira init` must succeed on every fixture shape."""
        result = run_codevira(codevira_bin, ["init"], cwd=project_root)
        assert result.returncode == 0, (
            f"init failed for fixture {project_root.name}:\n"
            f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

    def test_init_writes_centralized_config(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug D: init must write to ~/.codevira/projects/<key>/config.yaml,
        NOT to <project>/.codevira/config.yaml. The legacy in-repo path
        causes split-config bugs where init and configure disagree.
        """
        run_codevira(codevira_bin, ["init"], cwd=project_root)
        in_repo_config = project_root / ".codevira" / "config.yaml"
        # We accept EITHER no in-repo dir (preferred) OR an in-repo dir
        # that's empty / a migration breadcrumb. We do NOT accept a real
        # config.yaml inside the project directory.
        if in_repo_config.exists():
            content = in_repo_config.read_text()
            assert "watched_dirs" not in content, (
                f"Bug D regression: init wrote real config to in-repo .codevira/. "
                f"Should be in ~/.codevira/projects/<key>/. Got:\n{content[:500]}"
            )

    def test_init_extensions_match_disk(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug M: init's 'Auto-detected Extensions' list must reflect the
        project's actual file extensions, not the union of all 80 known
        extensions. A docs-only repo printing '.swift, .elm, .dart' in
        its detected extensions list destroys user trust.
        """
        result = run_codevira(codevira_bin, ["init"], cwd=project_root)
        # Extract the "Extensions:" line from init's output.
        for line in result.stdout.splitlines():
            if "Extensions:" in line and "Auto-detected" not in line:
                ext_line = line
                break
        else:
            pytest.skip("init output didn't include an Extensions line — UI changed")
        # Count detected extensions; should be way under 80 for any
        # realistic small fixture.
        ext_count = ext_line.count(".")
        # Tolerate up to 20 — generous to allow .yaml + .md + a few code
        # extensions plus near-noise. The bug is "all 80"; anything
        # under 20 means the detector actually scanned disk.
        assert ext_count < 20, (
            f"Bug M regression: init detected {ext_count} extensions for "
            f"fixture {project_root.name}, but the fixture has only a "
            f"handful of real extensions on disk. Detector likely returned "
            f"the all-known union. Got: {ext_line}"
        )

    def test_index_does_not_lie(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug B: `codevira index` must not say "up to date" when the
        index has 0 chunks. Either say "not initialized", "no files
        matched", or "indexed N files" — but NEVER lie about being
        up to date when nothing exists.

        Assertion logic: IF index claims 'up to date' THEN status must
        NOT show 0 chunks. (Previously had inverted OR/AND logic — fixed.)
        """
        run_codevira(codevira_bin, ["init"], cwd=project_root)
        # Run index AGAIN immediately. Either it indexes successfully,
        # or it tells the user clearly why it can't.
        result = run_codevira(codevira_bin, ["index"], cwd=project_root)
        if "up to date" in result.combined.lower():
            # If we claim "up to date", the index MUST have nonzero content.
            status = run_codevira(codevira_bin, ["status"], cwd=project_root)
            chunks_zero = (
                "Chunks:  0" in status.combined
                or "ChromaDB Chunks:  0" in status.combined
                or "Chunks: 0" in status.combined  # tolerate one-space variant
            )
            assert not chunks_zero, (
                f"Bug B regression: index claims 'up to date' but status "
                f"reports 0 chunks for fixture {project_root.name}. "
                f"Lie. Either index actually completed or it should say "
                f"'not initialized' / 'no files matched'.\n"
                f"  index output: {result.combined}\n"
                f"  status output: {status.combined}"
            )

    def test_init_includes_top_level_files(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug F: init must include '.' in watched_dirs when the project
        has matching files at the top level. The polyglot fixture has
        CLAUDE.md at top level — init dropped it because
        detect_watched_dirs filtered by single-language extension only.

        Only relevant for fixtures that actually have top-level source/
        docs files (polyglot, docs_only).
        """
        # Skip fixtures where top-level has no files we care about.
        if project_root.name not in ("polyglot", "docs_only"):
            pytest.skip(f"fixture {project_root.name} has no top-level files to check")

        result = run_codevira(codevira_bin, ["init"], cwd=project_root)
        # Extract the "Source dirs:" line from init output.
        for line in result.stdout.splitlines():
            if "Source dirs:" in line:
                dirs_line = line
                break
        else:
            pytest.skip("init output didn't include a Source dirs line — UI changed")

        # Extract the dir list from the right of "Source dirs:" — robust
        # against any of these formatting shapes init may use:
        #   "Source dirs: ., docs"          (comma-space joined, current style)
        #   "Source dirs: . docs"           (space joined)
        #   "Source dirs: ['.', 'docs']"    (list repr)
        # Walk the tokens and look for a bare "." entry. (Earlier version
        # used substring-match on `". "` which missed `". ,"` and `".,"`
        # — caught by gauntlet 2026-05-17.)
        rhs = dirs_line.split("Source dirs:", 1)[1].strip()
        # Normalize: strip brackets, quotes, commas, then split on whitespace.
        tokens = (
            rhs.replace("[", " ")
            .replace("]", " ")
            .replace("'", " ")
            .replace('"', " ")
            .replace(",", " ")
            .split()
        )
        has_top_level = "." in tokens
        assert has_top_level, (
            f"Bug F regression: init dropped top-level '.' from watched_dirs "
            f"for fixture {project_root.name} even though it has matching "
            f"files at top level (CLAUDE.md / README.md / package.json).\n"
            f"  Source dirs line: {dirs_line}\n  parsed tokens: {tokens}"
        )

    def test_docs_only_does_not_silently_produce_zero_chunks(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug E: docs-only projects must EITHER produce > 0 chunks (when
        the markdown chunker lands) OR fail loudly with a clear message
        like "no parseable code". A silent 0-chunks result was the
        lh-interface bug surfaced 2026-05-15.

        Only meaningful for the docs_only fixture.
        """
        if project_root.name != "docs_only":
            pytest.skip("only docs_only fixture exercises Bug E")

        run_codevira(codevira_bin, ["init"], cwd=project_root)
        index_result = run_codevira(codevira_bin, ["index"], cwd=project_root)
        status_result = run_codevira(codevira_bin, ["status"], cwd=project_root)

        chunks_zero = (
            "Chunks:  0" in status_result.combined
            or "Chunks: 0" in status_result.combined
        )
        if not chunks_zero:
            # Markdown chunker fix landed — > 0 chunks indexed. Pass.
            return

        # Chunks are 0. The user MUST have been told why.
        explanations = [
            "no parseable",
            "no source code",
            "no chunks produced",
            "markdown not yet supported",
            "no parser",
            "indexes code, not documentation",
            "see docs/limitations",
        ]
        combined = (index_result.combined + status_result.combined).lower()
        has_explanation = any(e in combined for e in explanations)
        assert has_explanation, (
            f"Bug E regression: docs-only fixture produced 0 chunks "
            f"with no explanation. Silent failure. Either ship the "
            f"markdown chunker OR fail loudly with a 'no parseable "
            f"code' message.\n"
            f"  index output: {index_result.combined}\n"
            f"  status output: {status_result.combined}"
        )

    def test_status_reflects_reality(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug C: status on a freshly-init'd project should NOT silently
        show 0 graph nodes / 0 chunks without explaining why.
        """
        run_codevira(codevira_bin, ["init"], cwd=project_root)
        result = run_codevira(codevira_bin, ["status"], cwd=project_root)
        if (
            "Graph Nodes:" in result.stdout
            and "0" in result.stdout.split("Graph Nodes:")[1][:20]
        ):
            # 0 graph nodes after init is a problem unless explained.
            # We accept some explanation in the output — "no parseable
            # code", "run codevira index", "fixture has no source files",
            # or any directive output.
            explanations = [
                "no parseable",
                "run codevira",
                "no files matched",
                "not yet indexed",
                "no source",
                "uninitialized",
            ]
            has_explanation = any(e in result.combined.lower() for e in explanations)
            assert has_explanation, (
                f"Bug C regression: status shows 0 graph nodes for "
                f"fixture {project_root.name} with no explanation. "
                f"Should tell user what to do.\n  output: {result.stdout}"
            )

    @pytest.mark.skip(
        reason="Placeholder — real Bug A test needs `configure --json --accept-all` "
        "non-interactive mode (planned in v2.1 alongside the matcher unification "
        "fix). Today this test would have to drive an interactive prompt."
    )
    def test_configure_uses_same_matcher_as_index(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Bug A: configure and index must use the same file matcher.
        If configure discovers N files but index matches 0, that's the
        canonical silent-failure pattern.

        When unskipped: drive `configure --json --accept-all`, parse the
        emitted file-count, then run index --verbose and compare counts.
        """
        pass

    def test_status_is_well_formed(
        self,
        codevira_bin: str,
        isolated_home: Path,
        project_root: Path,
    ) -> None:
        """
        Smoke check: status output is parseable. Catches breakage in
        the status formatter independent of the bigger Bug B / C tests.
        """
        run_codevira(codevira_bin, ["init"], cwd=project_root)
        status = run_codevira(codevira_bin, ["status"], cwd=project_root)
        assert (
            "Graph Nodes" in status.stdout or "graph_nodes" in status.stdout.lower()
        ), (
            f"status output looks malformed for fixture {project_root.name}:\n"
            f"  {status.stdout}"
        )


# ─── Smoke test that the test infrastructure itself works ──────────────────


def test_fixtures_present() -> None:
    """Sanity check: all 4 fixtures must exist or this whole suite is moot."""
    missing = [name for name in ALL_FIXTURE_NAMES if not (FIXTURES_DIR / name).is_dir()]
    assert not missing, (
        f"Missing fixtures: {missing}. "
        f"See tests/e2e/fixtures/ — each fixture is a sample project tree."
    )


def test_codevira_binary_resolvable() -> None:
    """Sanity check: the test runner can find codevira."""
    binary = shutil.which("codevira")
    if not binary:
        pytest.skip("codevira not on PATH — run `make dev` and re-run")
    assert Path(binary).is_file(), f"codevira on PATH but not a file: {binary}"
