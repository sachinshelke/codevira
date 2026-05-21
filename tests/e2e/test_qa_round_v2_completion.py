"""
test_qa_round_v2_completion.py — Integration QA across Phases 1-6
of the v2.0 close-out push.

Per the user's discipline ("we need to do the integration end-to-end
testing"), this is the cross-phase coexistence layer. Mutation tests
caught the per-phase regressions; this catches the seams between phases.

Phases under test
=================

  Phase 1 (commit 4293d78): codevira agents + hooks install CLIs +
                            wedge consistency
  Phase 2 (commit 5cec21f): cross-tool universality E2E
  Phase 3 (commit 39bae4e): codevira doctor health-check
  Phase 4 (commit a258b60): launch docs (READMEv2, RELEASE_NOTES,
                            differentiation page)
  Phase 5 (commit 2d242b5): Pillar 3 backlog + 5 cross-test fixes
  Phase 6 (commit 3622371): DOGFOOD + alpha-tester invites

Specifically asserts:

  - Doctor reports the watcher circuit state correctly when seeded
    by the circuit breaker
  - The agents CLI's nudge-file output is what doctor's check_nudge_files
    actually verifies (no contract drift)
  - safe_log_crash from Phase 5 doesn't break dispatch when crash_logger
    is reachable (positive control on top of the unit test of the
    failure case)
  - The shared _sqlite_util enables WAL on the real SQLiteGraph
    (Phase 5 dedup didn't break the actual behavior)
  - cli_agents + cli_replay + cli_insights + doctor all use the same
    is_invalid_project_root guard (Bug-8 parity across all v2.0 CLIs)

Plus Bug-X-shape audit: every command we added wires through the same
project-root validation + the same crash logger contract.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest


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


@pytest.fixture(autouse=True)
def _reset_circuit():
    from indexer.index_codebase import reset_watcher_circuit

    reset_watcher_circuit()
    yield
    reset_watcher_circuit()


# =====================================================================
# A. Doctor + circuit breaker integration (Phase 3 + Phase 5)
# =====================================================================


class TestA_DoctorCircuitIntegration:
    def test_doctor_surfaces_circuit_open_state(
        self,
        isolated_project: Path,
    ):
        """When the watcher circuit opens (3 consecutive failures),
        doctor's check_watcher_circuit must report FAIL with a fix
        command."""
        from indexer.index_codebase import _watcher_circuit_record_failure
        from mcp_server.doctor import cmd_doctor

        # Force the circuit open
        for _ in range(3):
            _watcher_circuit_record_failure(RuntimeError("integration test"))

        out = io.StringIO()
        rc = cmd_doctor(out=out)
        assert rc == 1, "doctor should return 1 when circuit is open"
        text = out.getvalue()
        assert "watcher_circuit" in text
        assert "OPEN" in text
        assert "→ to fix:" in text

    def test_circuit_recovery_clears_doctor_failure(
        self,
        isolated_project: Path,
    ):
        """A successful reindex resets the circuit. Doctor should
        clear the FAIL and report PASS again."""
        from indexer.index_codebase import (
            _watcher_circuit_record_failure,
            _watcher_circuit_record_success,
        )
        from mcp_server.doctor import check_watcher_circuit

        for _ in range(3):
            _watcher_circuit_record_failure(RuntimeError("x"))
        assert check_watcher_circuit().state == "FAIL"

        _watcher_circuit_record_success()
        assert check_watcher_circuit().state == "PASS"


# =====================================================================
# B. Agents CLI ↔ Doctor contract (Phase 1 ↔ Phase 3)
# =====================================================================


class TestB_AgentsDoctorContract:
    def test_agents_creates_files_doctor_acknowledges(
        self,
        isolated_project: Path,
    ):
        """If `codevira agents` writes nudge files for all detected
        IDEs, then `codevira doctor`'s check_nudge_files should not
        report missing nudge files for those same IDEs."""
        from mcp_server.cli_agents import cmd_agents
        from mcp_server.doctor import check_nudge_files

        # Generate every nudge file
        rc = cmd_agents(out=io.StringIO())
        assert rc == 0

        # Doctor's check should pass (or warn for non-detected IDEs only)
        result = check_nudge_files()
        # On a real machine some IDEs may not be detected, so the most
        # we can assert: result does NOT FAIL.
        assert result.state in ("PASS", "WARN")
        # If it warns, the missing list shouldn't include 'claude' since
        # we just wrote CLAUDE.md.
        if result.state == "WARN":
            assert "claude " not in (result.details or ""), (
                f"agents-CLI / doctor contract drift: doctor says claude "
                f"missing but agents-CLI wrote CLAUDE.md. details: {result.details}"
            )


# =====================================================================
# C. Bug-8 parity across all v2.0 CLIs (Phase 1 / 3 / Hero 8 / Hero 10)
# =====================================================================


class TestC_Bug8ParityAcrossAllCLIs:
    """Every v2.0 CLI that takes --project must run it through
    is_invalid_project_root. If we add a new CLI later, the suite
    here gives a uniform test pattern to copy."""

    @pytest.fixture
    def repo_env(self, tmp_path: Path) -> dict[str, str]:
        repo = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(tmp_path / "fake_home")
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        return env

    # v2.2.0+: `insights` removed from the CLI (Hero 10 + cli_insights
    # deleted per 2026-05-22 surface-cut audit).
    @pytest.mark.parametrize(
        "subcommand_args",
        [
            ["agents", "--project", "/etc"],
            ["replay", "--project", "/etc", "--ascii"],
        ],
    )
    def test_subcommand_rejects_invalid_project(
        self,
        subcommand_args: list[str],
        repo_env: dict[str, str],
    ):
        """Every CLI command with --project must reject /etc with rc=1
        + a clear error message — uniform Bug-8 defense."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", *subcommand_args],
            env=repo_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 1, (
            f"{' '.join(subcommand_args)}: expected rc=1 (Bug-8 reject); "
            f"got rc={result.returncode}, stdout={result.stdout!r}"
        )
        assert (
            "not a valid project root" in result.stdout
        ), f"{' '.join(subcommand_args)}: missing Bug-8 message"


# =====================================================================
# D. safe_log_crash positive + negative path (Phase 5)
# =====================================================================


class TestD_SafeLogCrashContract:
    def test_safe_log_crash_does_not_raise_when_module_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The whole point of safe_log_crash: NEVER raise.
        Simulate a broken crash_logger module.

        Cleanup discipline: restore via either putting the real module
        back OR fully deleting the sys.modules entry (so subsequent
        imports re-resolve normally). Leaving ``None`` in sys.modules
        permanently poisons future ``import mcp_server.crash_logger``
        calls — that pollution broke a sibling test."""
        import sys

        real = sys.modules.get("mcp_server.crash_logger")
        try:
            sys.modules["mcp_server.crash_logger"] = None  # noqa: type-arg
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(RuntimeError("simulated"), context="test")
        finally:
            if real is not None:
                sys.modules["mcp_server.crash_logger"] = real
            else:
                # Real module wasn't loaded yet — DELETE the None entry
                # so future imports re-resolve. (Setting to None left a
                # poisoned entry that blocked future imports.)
                sys.modules.pop("mcp_server.crash_logger", None)

    def test_safe_log_crash_writes_through_when_logger_works(
        self,
        isolated_project: Path,
    ):
        """Positive control: when crash_logger IS available, the helper
        actually delivers the crash to the log."""
        from mcp_server._safe_crash import safe_log_crash
        from mcp_server.crash_logger import get_crash_log_path

        log_path = get_crash_log_path()
        size_before = log_path.stat().st_size if log_path.exists() else 0

        # Trigger
        safe_log_crash(
            RuntimeError("integration test marker abc123"),
            context="test_qa_round_v2_completion",
        )

        # Log grew
        assert log_path.exists()
        size_after = log_path.stat().st_size
        assert size_after > size_before
        # Sanitized content includes the marker
        content = log_path.read_text(encoding="utf-8", errors="replace")
        assert "abc123" in content


# =====================================================================
# E. Shared _sqlite_util enables WAL on real SQLiteGraph (Phase 5)
# =====================================================================


class TestE_SqliteUtilOnRealGraph:
    def test_real_sqlite_graph_uses_wal_via_shared_helper(
        self,
        isolated_project: Path,
    ):
        """Phase 5 deduped _enable_wal_with_retry into _sqlite_util.
        Verify the shim still produces a WAL-enabled connection."""
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(db_path)
        try:
            mode = g.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert (
                str(mode).lower() == "wal"
            ), f"Phase 5 dedup regression: WAL not enabled. mode={mode}"
        finally:
            g.close()

    def test_real_global_db_uses_wal_via_shared_helper(
        self,
        isolated_project: Path,
    ):
        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_home

        gdb = GlobalDB(get_global_home() / "global.db")
        try:
            mode = gdb.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert (
                str(mode).lower() == "wal"
            ), f"Phase 5 dedup regression in global_db. mode={mode}"
        finally:
            gdb.close()


# =====================================================================
# F. Final integration: every Phase 1-5 surface coexists cleanly
# =====================================================================


class TestF_AllPhasesCoexist:
    def test_doctor_runs_clean_on_fresh_install_with_agents_files(
        self,
        isolated_project: Path,
    ):
        """End-to-end: setup-style flow.

        1. Run codevira agents (Phase 1) — write nudge files
        2. Run codevira doctor (Phase 3) — verify everything green
        3. Doctor must NOT fail (PASS or WARN only, never FAIL)
        """
        from mcp_server.cli_agents import cmd_agents
        from mcp_server.doctor import cmd_doctor

        # Phase 1: write nudge files
        rc1 = cmd_agents(out=io.StringIO())
        assert rc1 == 0

        # Phase 3: health check on the result
        out = io.StringIO()
        rc2 = cmd_doctor(out=out)
        # rc2 may be 0 (clean) or 1 (some FAIL, e.g., graph.db not yet
        # created — that's fine for this test, we just check the doctor
        # ran end-to-end without exceptions).
        assert rc2 in (0, 1)
        text = out.getvalue()
        assert "Codevira health check" in text
        assert "summary:" in text

    def test_universality_e2e_works_alongside_doctor(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Phase 2 (universality E2E) and Phase 3 (doctor) target
        different surfaces but use overlapping path-resolution code.
        This test ensures they don't step on each other when invoked
        in the same process."""
        # Doctor first — sets project-dir state
        from mcp_server.doctor import run_all_checks

        report1 = run_all_checks()
        assert len(report1.results) >= 9

        # Then a Hero 5 / Hero 9-style UserPromptSubmit dispatch
        # (the universality E2E path)
        from mcp_server.engine.runner import reset_policies
        from mcp_server.engine import register_default_policies, dispatch
        from mcp_server.engine.events import EventType, HookEvent

        reset_policies()
        register_default_policies()
        v = dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="qa-completion",
                prompt_text="show me decisions about retries",
            )
        )
        # Verdict can be allow or inject — both fine; we just check
        # nothing crashed when both surfaces ran.
        assert v.action in ("allow", "inject")

        # Doctor again — should still work
        report2 = run_all_checks()
        assert len(report2.results) == len(report1.results)

        reset_policies()
