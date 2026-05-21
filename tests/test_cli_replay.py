"""
test_cli_replay.py — End-to-end CLI subprocess tests for Hero 8.

Bug-4 lesson: don't trust unit-tests of cmd_replay; subprocess the
actual CLI against an isolated DB and verify stdout. Bug-8 lesson:
verify --project rejects invalid roots with rc=1.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_project_with_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, str]]:
    """v2.2.0+ fixture: plant decisions via the JSONL store.

    Replaces the v2.1.x SQLiteGraph planter. Bootstraps .codevira/
    in-repo (so `paths.is_initialized()` returns True), records one
    decision via decisions_store.record, and appends 5 "kept" outcome
    rows to outcomes.jsonl.
    """
    from datetime import datetime, timezone

    fake_home = tmp_path / "home"
    cv_data = fake_home / ".codevira"
    cv_data.mkdir(parents=True)
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")

    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()

    from mcp_server.storage import (
        decisions_store,
        jsonl_store,
        paths as store_paths,
    )

    store_paths.ensure_dirs()

    # Plant session summary so the replay output shows it.
    jsonl_store.append(
        store_paths.sessions_path(),
        {
            "id": "S-s1",
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": "s1",
            "summary": "Fix login flow for special-char emails",
        },
    )
    decision_id = decisions_store.record(
        "use bcrypt over argon2 — see issue #142",
        file_path="auth.py",
        session_id="s1",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for _ in range(5):
        jsonl_store.append(
            store_paths.outcomes_path(),
            {
                "ts": now_iso,
                "decision_id": decision_id,
                "outcome_type": "kept",
                "delta_summary": "test kept",
            },
        )

    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(fake_home)
    return project, env


class TestCLIReplay:
    def test_terminal_format_renders_decision_text(
        self,
        isolated_project_with_decisions,
    ):
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--ascii",
                "--since=30d",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"insights CLI failed: stderr={result.stderr!r}"
        # Lesson #19: content-verifying — the decision text must appear
        assert "use bcrypt over argon2" in result.stdout
        assert "auth.py" in result.stdout
        # Session summary surfaced
        assert "Fix login flow" in result.stdout

    def test_query_filter(self, isolated_project_with_decisions):
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--query=bcrypt",
                "--ascii",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "bcrypt" in result.stdout

    def test_query_filter_no_match(self, isolated_project_with_decisions):
        """Query that matches nothing → empty placeholder, NOT a header."""
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--query=NONEXISTENT_TERM",
                "--ascii",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "No decisions recorded yet" in result.stdout

    def test_markdown_format(self, isolated_project_with_decisions):
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--format=markdown",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "# Codevira Replay" in result.stdout
        assert "## " in result.stdout
        assert "use bcrypt" in result.stdout

    def test_html_format(self, isolated_project_with_decisions):
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--format=html",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "<!DOCTYPE html>" in result.stdout
        assert "use bcrypt" in result.stdout
        assert "<article" in result.stdout

    def test_html_format_with_out_file(
        self,
        isolated_project_with_decisions,
        tmp_path,
    ):
        project, env = isolated_project_with_decisions
        out_file = tmp_path / "timeline.html"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--format=html",
                "--out",
                str(out_file),
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert out_file.exists()
        assert "use bcrypt" in out_file.read_text()
        # stdout has the "Wrote ..." confirmation, NOT the HTML
        assert "Wrote " in result.stdout
        assert "<!DOCTYPE html>" not in result.stdout

    def test_invalid_format_rejected(self, isolated_project_with_decisions):
        project, env = isolated_project_with_decisions
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(project),
                "--format=excel",
            ],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # argparse rejects with rc != 0 (typically 2)
        assert result.returncode != 0

    def test_project_home_rejected_bug8(self, tmp_path):
        """Bug-8 parity: --project $HOME must be rejected with rc=1
        + a clear error, not silently fall through to an empty result."""
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(tmp_path / "fake_home_for_bug8")
        # Ensure HOME exists so it's a real path
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                env["HOME"],
                "--ascii",
            ],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 1, (
            f"--project $HOME must reject (rc=1); got rc={result.returncode}, "
            f"stdout={result.stdout!r}"
        )
        assert "not a valid project root" in result.stdout

    def test_empty_project(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "pyproject.toml").write_text("")

        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(tmp_path / "fake_home_empty")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server.cli",
                "replay",
                "--project",
                str(empty),
                "--ascii",
            ],
            cwd=str(empty),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        # Friendly empty case (Lesson #19)
        out = result.stdout
        assert "No codevira data" in out or "No decisions" in out
