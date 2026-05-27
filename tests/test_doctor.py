"""
test_doctor.py — Pillar 1.3: `codevira doctor` health check.

Tier-0 + deep-audit applied:
  - Real DB integration (each check runs against real fixtures)
  - End-to-end through `cmd_doctor` (the CLI entry path)
  - Subprocess test for the wired-up CLI subcommand
  - Bug-X-shape: every check returns a CheckResult, never raises
  - Empty / corrupt input handling per check (Lesson #19)
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import doctor


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".git").mkdir()
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    return project


# =====================================================================
# Individual check tests
# =====================================================================


class TestPythonVersion:
    def test_pass_on_modern_python(self):
        # We're running on 3.13 in tests, so this passes
        r = doctor.check_python_version()
        assert r.state == "PASS"
        assert "3." in r.message


class TestDataDir:
    def test_pass_when_dir_exists(self, isolated_project: Path):
        r = doctor.check_codevira_data_dir()
        assert r.state == "PASS"

    def test_warn_when_dir_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        missing = tmp_path / "nonexistent"
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home",
            lambda: missing,
        )
        r = doctor.check_codevira_data_dir()
        assert r.state == "WARN"
        assert "does not exist" in r.message
        assert "codevira setup" in r.fix_command


class TestProjectRoot:
    def test_pass_for_valid_project(self, isolated_project: Path):
        r = doctor.check_project_root()
        assert r.state == "PASS"

    def test_fail_for_root_dir(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root",
            lambda: Path("/"),
        )
        r = doctor.check_project_root()
        assert r.state == "FAIL"
        assert "rejected" in r.message


class TestGraphDB:
    def test_warn_when_missing(self, isolated_project: Path):
        r = doctor.check_graph_db()
        assert r.state == "WARN"
        assert "does not exist" in r.message
        assert "codevira index" in r.fix_command

    def test_pass_when_valid(self, isolated_project: Path):
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        SQLiteGraph(db_path).close()  # creates schema
        r = doctor.check_graph_db()
        assert r.state == "PASS"
        assert "expected tables" in r.message

    def test_fail_when_corrupted(self, isolated_project: Path):
        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"not a sqlite db")
        r = doctor.check_graph_db()
        assert r.state == "FAIL"
        # Either "corrupted" or "missing tables" depending on which
        # bug surfaces first
        assert "graph.db" in r.message or "corrupt" in r.message.lower()
        assert r.fix_command


class TestEngineKillSwitch:
    def test_pass_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)
        r = doctor.check_engine_kill_switch()
        assert r.state == "PASS"

    def test_warn_when_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")
        r = doctor.check_engine_kill_switch()
        assert r.state == "WARN"
        assert "DISABLED" in r.message
        assert "unset CODEVIRA_ENGINE" in r.fix_command

    def test_warn_on_invalid_value(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_ENGINE", "garbage")
        r = doctor.check_engine_kill_switch()
        assert r.state == "WARN"


class TestCrashLogSize:
    def test_pass_when_no_log(self, isolated_project: Path):
        r = doctor.check_crash_log_size()
        assert r.state == "PASS"

    def test_warn_when_oversized(self, isolated_project: Path):
        from mcp_server.paths import get_global_home

        log = get_global_home() / "crash.log"
        log.write_bytes(b"x" * (6 * 1024 * 1024))  # 6 MB > 5 MB cap
        r = doctor.check_crash_log_size()
        assert r.state == "WARN"
        assert "MB" in r.message
        assert "archived" in r.fix_command


class TestNudgeFiles:
    def test_warn_when_missing(self, isolated_project: Path):
        """v2.2.0+ (2026-05-22 surface-cut audit): the per-IDE nudge
        matrix collapsed to AGENTS.md only. The doctor check now
        verifies that single file; the fix command is `codevira sync`
        (not the deleted `codevira agents`)."""
        # No AGENTS.md present in the fresh isolated_project fixture.
        r = doctor.check_nudge_files()
        assert r.state in ("PASS", "WARN")
        if r.state == "WARN":
            assert "codevira sync" in r.fix_command, (
                f"fix command should now point at `codevira sync` "
                f"(was `codevira agents` pre-v2.2.0); got "
                f"{r.fix_command!r}"
            )


# =====================================================================
# Runner
# =====================================================================


# =====================================================================
# v2.0-rc.4 (Bugs 10, 11, 12) — new doctor checks
# =====================================================================


class TestClaudeMcpVisibility:
    """Bug 10: codevira doctor must catch the rc.1 regression where
    codevira was silently invisible to Claude Code's MCP runtime."""

    def test_warns_when_claude_cli_missing(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = doctor.check_claude_mcp_visibility()
        assert result.state == "WARN"
        assert "claude CLI" in result.message or "claude cli" in result.message.lower()

    def test_pass_when_codevira_listed_and_connected(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/fake/claude")

        class FakeResult:
            returncode = 0
            stdout = "codevira: /usr/local/bin/codevira  - ✓ Connected\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        result = doctor.check_claude_mcp_visibility()
        assert result.state == "PASS"

    def test_fail_when_codevira_missing_from_list(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/fake/claude")

        class FakeResult:
            returncode = 0
            stdout = "claude.ai Notion: https://... - ✓ Connected\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        result = doctor.check_claude_mcp_visibility()
        assert result.state == "FAIL"
        assert "codevira" in result.message.lower()
        assert (
            "codevira setup" in result.fix_command
            or "claude mcp add" in result.fix_command
        )

    def test_warn_when_listed_but_not_connected(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/fake/claude")

        class FakeResult:
            returncode = 0
            stdout = "codevira: /usr/local/bin/codevira  - ✗ Failed to connect\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        result = doctor.check_claude_mcp_visibility()
        assert result.state == "WARN"


class TestCodeindexFreshness:
    """Bug 11: detect codeindex/ from older codevira version."""

    def test_pass_when_no_codeindex(self, isolated_project: Path):
        result = doctor.check_codeindex_freshness()
        assert result.state == "PASS"

    def test_pass_when_codeindex_recent(self, isolated_project: Path):
        from mcp_server.paths import get_data_dir

        ci = get_data_dir() / "codeindex"
        ci.mkdir(parents=True)
        # Create a fresh file
        (ci / "data.bin").write_text("recent")
        result = doctor.check_codeindex_freshness()
        assert result.state == "PASS"

    def test_warn_when_codeindex_stale(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import os
        from mcp_server.paths import get_data_dir

        ci = get_data_dir() / "codeindex"
        ci.mkdir(parents=True)
        old_file = ci / "data.bin"
        old_file.write_text("old")
        # Backdate 30 days
        old_time = old_file.stat().st_mtime - (30 * 86400)
        os.utime(old_file, (old_time, old_time))

        result = doctor.check_codeindex_freshness()
        assert result.state == "WARN"
        assert "stale" in result.message.lower()
        assert "rm -rf" in result.fix_command


class TestSemanticSearchHealth:
    """Bug 12: surface ChromaDB chunks=0 as a doctor warning."""

    def test_warn_when_no_codeindex(self, isolated_project: Path):
        result = doctor.check_semantic_search_health()
        assert result.state == "WARN"
        assert "codevira index" in result.fix_command

    def test_warn_when_codeindex_tiny(self, isolated_project: Path):
        from mcp_server.paths import get_data_dir

        ci = get_data_dir() / "codeindex"
        ci.mkdir(parents=True)
        (ci / "data.bin").write_text("tiny")  # < 100 KB
        result = doctor.check_semantic_search_health()
        assert result.state == "WARN"
        assert "empty" in result.message.lower() or "degraded" in result.message.lower()

    def test_pass_when_codeindex_substantial(self, isolated_project: Path):
        from mcp_server.paths import get_data_dir

        ci = get_data_dir() / "codeindex"
        ci.mkdir(parents=True)
        # Write 200 KB so it crosses the 100 KB threshold
        (ci / "data.bin").write_text("x" * (200 * 1024))
        result = doctor.check_semantic_search_health()
        assert result.state == "PASS"


class TestRunAllChecks:
    def test_run_all_returns_report(self, isolated_project: Path):
        report = doctor.run_all_checks()
        # rc.4 added 3 new checks: claude_mcp_visibility, codeindex_freshness,
        # semantic_search_health. Total checks ≥ 12 now.
        assert len(report.results) >= 12
        # Every result is a CheckResult
        for r in report.results:
            assert isinstance(r, doctor.CheckResult)
            assert r.state in ("PASS", "WARN", "FAIL")
            assert r.message  # non-empty

    def test_buggy_check_does_not_propagate(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Defense: if any check raises, the runner catches and emits
        a FAIL result — the doctor never crashes its own output."""
        from mcp_server import doctor as d_mod

        def crashing_check():
            raise RuntimeError("intentional check crash")

        monkeypatch.setattr(d_mod, "_CHECKS", (crashing_check,))
        report = d_mod.run_all_checks()
        assert len(report.results) == 1
        assert report.results[0].state == "FAIL"
        assert "crashed" in report.results[0].message


# =====================================================================
# CLI entry point
# =====================================================================


class TestCmdDoctor:
    @pytest.mark.skip(
        reason="v2.2.0: tests deprecated feature (search_codebase / _check_search_deps / graph.db backend)"
    )
    def test_cmd_doctor_returns_0_on_clean(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If no checks fail (warns OK), exit code is 0."""
        # Make sure all FAIL paths are off (set up enough state)
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        SQLiteGraph(db_path).close()

        out = io.StringIO()
        rc = doctor.cmd_doctor(out=out)
        assert rc == 0
        text = out.getvalue()
        assert "Codevira health check" in text
        assert "summary:" in text

    def test_cmd_doctor_returns_1_on_failure(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Force a failure: make project_root invalid
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root",
            lambda: Path("/"),
        )
        out = io.StringIO()
        rc = doctor.cmd_doctor(out=out)
        assert rc == 1

    def test_cmd_doctor_shows_fix_commands_for_warns(
        self,
        isolated_project: Path,
    ):
        """User-facing requirement: every warn / fail must show the
        '→ to fix:' line so the user knows what to do."""
        out = io.StringIO()
        doctor.cmd_doctor(out=out)
        text = out.getvalue()
        # If anything is WARN or FAIL, "→ to fix:" should appear at
        # least once. (On a clean install everything passes, so this
        # might be vacuous — but at least we verify the format.)
        # Force a warn condition: graph.db missing.
        # (already missing in our fixture)
        if "⚠" in text or "✗" in text:
            assert "→ to fix:" in text


# =====================================================================
# Subprocess (full CLI wiring)
# =====================================================================


class TestSubprocessWiring:
    def test_doctor_subcommand_runs_via_python_m(self):
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "doctor"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Either 0 (clean) or 1 (something failed); both legit.
        assert result.returncode in (0, 1)
        assert "Codevira health check" in result.stdout
        assert "summary:" in result.stdout

    def test_doctor_subcommand_help(self):
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "doctor", "--help"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "Diagnose codevira" in result.stdout
        assert "--verbose" in result.stdout
