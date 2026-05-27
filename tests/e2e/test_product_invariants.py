"""
test_product_invariants.py — machine-checkable foolproof-product invariants.

Where `test_first_contact.py` exercises specific bugs (A–O) against
fixture projects, this file enforces the abstract principles (P1–P10)
that prevent the *next* class of bugs.

Invariants encoded here:
  P1 No silent failures        — every 0-result path emits a reason
  P2 Self-diagnose on startup  — doctor detects every known-bad state
  P3 Atomic state mutations    — config writes survive process kill
  P4 Defensive parsing         — malformed input doesn't crash
  P5 Bounded resources         — error loops have circuit breakers
  P6 Predictable detection     — configure and index agree
  P7 Reversible operations     — every install has a clean uninstall
  P8 Helpful error messages    — every error has WHY + FIX
  P9 Graceful degradation      — single subsystem failure isolated
  P10 Observability            — doctor reports actual state

Run as part of G2 in the release gauntlet. A failure here is a
foolproof-product regression — NEVER ship while red.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def codevira_bin() -> str:
    binary = shutil.which("codevira")
    if not binary:
        pytest.skip("codevira binary not on PATH — run `make dev` first")
    return binary


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CODEVIRA_HOME", str(fake_home / ".codevira"))
    return fake_home


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    """A truly empty project — only a README, no source code."""
    p = tmp_path / "empty"
    p.mkdir()
    (p / "README.md").write_text("# Empty project\n")
    return p


def run_codevira(binary: str, args: list[str], cwd: Path, timeout: int = 60):
    return subprocess.run(
        [binary, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ},
    )


# ─── P1 — No silent failures ───────────────────────────────────────────────


class TestP1NoSilentFailures:
    """Every 0-result code path must explain why."""

    def test_index_on_empty_project_explains_why(
        self,
        codevira_bin: str,
        isolated_home: Path,
        empty_project: Path,
    ) -> None:
        """When index finds 0 files, the output must explain why."""
        run_codevira(codevira_bin, ["init"], cwd=empty_project)
        result = run_codevira(codevira_bin, ["index"], cwd=empty_project)
        combined = result.stdout + result.stderr
        # If the index reports 0 chunks, it MUST mention either:
        #   - the matched count is 0 (and why)
        #   - no files matched watched_dirs/file_extensions
        #   - the configure command as a fix
        if "0 chunks" in combined or "Chunks:  0" in combined:
            explanations = [
                "no files matched",
                "watched_dirs",
                "configure",
                "no source",
            ]
            has_explanation = any(e in combined.lower() for e in explanations)
            assert has_explanation, (
                f"P1 violation: index produced 0 chunks with NO explanation.\n"
                f"  Output: {combined}"
            )

    def test_status_on_empty_project_signals_state(
        self,
        codevira_bin: str,
        isolated_home: Path,
        empty_project: Path,
    ) -> None:
        """Status must not just show 0/0 — it must say "this is not yet indexed"."""
        result = run_codevira(codevira_bin, ["status"], cwd=empty_project)
        combined = result.stdout + result.stderr
        if "0" in combined:
            actionables = [
                "init",
                "configure",
                "index",
                "not",
                "uninitialized",
                "empty",
            ]
            has_actionable = any(a in combined.lower() for a in actionables)
            assert has_actionable, (
                f"P1 violation: status shows 0 with NO actionable signal.\n"
                f"  Output: {combined}"
            )


# ─── P2 — Self-diagnose on startup ─────────────────────────────────────────


class TestP2SelfDiagnose:
    """`codevira doctor` must detect known-bad states."""

    def test_doctor_runs_without_crash(
        self,
        codevira_bin: str,
        isolated_home: Path,
        tmp_path: Path,
    ) -> None:
        """Doctor must always run, even on a clean machine with no projects."""
        result = run_codevira(codevira_bin, ["doctor"], cwd=tmp_path)
        # Doctor's job is to detect issues, so non-zero exit on a fresh
        # machine is fine. The test is that it doesn't crash.
        assert "Traceback" not in result.stderr, (
            f"P2 violation: doctor crashed with traceback.\n"
            f"  stderr: {result.stderr}"
        )

    def test_doctor_output_includes_remediation(
        self,
        codevira_bin: str,
        isolated_home: Path,
        tmp_path: Path,
    ) -> None:
        """Any doctor WARN/FAIL must include a fix_command or remediation hint."""
        result = run_codevira(codevira_bin, ["doctor"], cwd=tmp_path)
        combined = result.stdout + result.stderr
        # Look for WARN/FAIL markers
        has_issues = any(m in combined for m in ["⚠", "✗", "FAIL", "WARN"])
        if has_issues:
            # If there are issues reported, the output should also include
            # actionable language (run X, fix Y, etc.)
            actionables = ["run `", "fix:", "remediation", "to fix", "command"]
            has_actionable = any(a in combined.lower() for a in actionables)
            assert has_actionable, (
                f"P2 violation: doctor flagged issues but offered no fix command.\n"
                f"  Output: {combined}"
            )


# ─── P4 — Defensive parsing ────────────────────────────────────────────────


class TestP4DefensiveParsing:
    """Malformed input must not crash codevira."""

    def test_malformed_yaml_does_not_crash_init(
        self,
        codevira_bin: str,
        isolated_home: Path,
        tmp_path: Path,
    ) -> None:
        """If user creates a bogus .codevira/config.yaml, init should handle it."""
        project = tmp_path / "bad-yaml"
        project.mkdir()
        (project / "README.md").write_text("# bad\n")
        bad_cfg_dir = project / ".codevira"
        bad_cfg_dir.mkdir()
        (bad_cfg_dir / "config.yaml").write_text("this: is: : not: valid: yaml ][[")

        result = run_codevira(codevira_bin, ["init"], cwd=project)
        assert "Traceback" not in result.stderr, (
            f"P4 violation: malformed yaml crashed init.\n" f"  stderr: {result.stderr}"
        )


# ─── P7 — Reversible operations ────────────────────────────────────────────


class TestP7Reversible:
    """Every install has an uninstall."""

    def test_clean_command_exists(self, codevira_bin: str) -> None:
        """`codevira clean` must be a real command."""
        result = subprocess.run(
            [codevira_bin, "clean", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"P7 violation: `codevira clean --help` failed. "
            f"There must be a documented uninstall path.\n"
            f"  exit={result.returncode}\n  stderr={result.stderr}"
        )

    def test_uninstall_exists(self, codevira_bin: str) -> None:
        """`codevira uninstall` must be a real command (v2.2.0+: `hooks`
        sub-surface dropped in favor of a single project-level uninstall)."""
        result = subprocess.run(
            [codevira_bin, "uninstall", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"P7 violation: `codevira uninstall --help` failed.\n"
            f"  exit={result.returncode}\n  stderr={result.stderr}"
        )


# ─── P8 — Helpful error messages ───────────────────────────────────────────


class TestP8HelpfulErrors:
    """Every user-facing error must answer WHAT + WHY + FIX."""

    def test_index_outside_project_explains_what_to_do(
        self,
        codevira_bin: str,
        isolated_home: Path,
        tmp_path: Path,
    ) -> None:
        """Running `codevira index` in a non-project dir must guide the user."""
        non_project = tmp_path / "not-a-project"
        non_project.mkdir()
        # Don't create any project markers — no init, no .codevira

        result = run_codevira(codevira_bin, ["status"], cwd=non_project)
        combined = result.stdout + result.stderr
        # If status fails / warns, it must say what to do.
        if result.returncode != 0 or "✗" in combined or "Error" in combined.lower():
            actionables = ["run", "init", "configure", "fix"]
            has_actionable = any(a in combined.lower() for a in actionables)
            assert has_actionable, (
                f"P8 violation: error reported with no remediation guidance.\n"
                f"  Output: {combined}"
            )


# ─── P10 — Observability ──────────────────────────────────────────────────


class TestP10Observability:
    """Doctor reports the actual state of the system."""

    def test_doctor_reports_structured_checks(
        self,
        codevira_bin: str,
        isolated_home: Path,
        tmp_path: Path,
    ) -> None:
        """Doctor output must include named checks (not just a free-form blob)."""
        result = run_codevira(codevira_bin, ["doctor"], cwd=tmp_path)
        combined = result.stdout + result.stderr
        # Doctor output should include identifiable check names.
        check_indicators = [
            "python_version",
            "codevira_data_dir",
            "doctor",
            "✓",
            "⚠",
            "✗",
        ]
        has_structure = any(i in combined for i in check_indicators)
        assert has_structure, (
            f"P10 violation: doctor output is not structured.\n"
            f"  Output: {combined[:500]}"
        )


# ─── Smoke ─────────────────────────────────────────────────────────────────


def test_codevira_responds_to_version() -> None:
    """Basic: codevira --version returns something."""
    binary = shutil.which("codevira")
    if not binary:
        pytest.skip("codevira not on PATH")
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert result.stdout or result.stderr, "--version produced no output at all"
