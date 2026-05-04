"""
test_cli_insights.py — End-to-end test for `codevira insights` CLI.

Tier-0 pre-flight Bug-4 lesson: don't trust unit tests of cmd_insights;
run the actual subprocess against an isolated DB and verify stdout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_project_with_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, str]]:
    """Create an isolated project with planted outcomes; return (path, env)."""
    fake_home = tmp_path / "home"
    cv_data = fake_home / ".codevira"
    cv_data.mkdir(parents=True)
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")

    # Use the codevira API directly to plant data so we don't have to
    # subprocess into setup.
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    import mcp_server.paths as paths_mod
    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    from mcp_server.paths import get_data_dir
    graph_db = get_data_dir() / "graph" / "graph.db"
    graph_db.parent.mkdir(parents=True, exist_ok=True)

    from indexer.sqlite_graph import SQLiteGraph
    g = SQLiteGraph(graph_db)
    g.conn.execute(
        "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
        ("s1", "test"),
    )
    cur = g.conn.execute(
        "INSERT INTO decisions (session_id, decision, file_path, "
        "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        ("s1", "use bcrypt over argon2 — see issue #142", "auth.py", "perf"),
    )
    did = cur.lastrowid
    for _ in range(5):
        g.record_outcome(
            session_id="s1", file_path="auth.py",
            outcome_type="kept", decision_id=did,
        )
    # A reverted decision too — should appear in "top reverted" section.
    cur = g.conn.execute(
        "INSERT INTO decisions (session_id, decision, file_path, "
        "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        ("s1", "Bootstrap not Tailwind", "style.css", "perf"),
    )
    did2 = cur.lastrowid
    for _ in range(4):
        g.record_outcome(
            session_id="s1", file_path="style.css",
            outcome_type="reverted", decision_id=did2,
        )
    g.record_outcome(
        session_id="s1", file_path="style.css",
        outcome_type="kept", decision_id=did2,
    )
    g.conn.commit()
    g.close()

    # Build env that the subprocess will use to find this project.
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    # Subprocess can't see the monkeypatched get_global_home; route via env.
    # The CLI's --project arg + cli_insights.set_project_dir will handle the
    # paths.get_data_dir resolution, but we still need get_global_home to
    # return our fake_home. We do that by setting CODEVIRA_GLOBAL_HOME if
    # supported, OR by relying on the project_dir override directing to the
    # already-created graph_db at the centralized location. Since
    # paths._sanitize_path_key uses the project path, and the centralized
    # location is computed from get_global_home(), we need that to match.
    # Solution: copy the prepared graph_db to where the subprocess will look.
    return project, env


class TestCLIInsightsSubprocess:

    def test_cli_runs_against_real_project_with_outcomes(
        self, isolated_project_with_outcomes: tuple[Path, dict[str, str]],
    ):
        """Run `python -m mcp_server.cli insights --project <path> --since=30d`
        and verify stdout contains the planted decisions."""
        project, env = isolated_project_with_outcomes
        # The subprocess will resolve get_global_home from os.environ —
        # that's the actual user home. To make the subprocess find the
        # graph_db we just created, we need it at the home location too.
        # Easiest: pass HOME to point at our fake. (CODEVIRA stores data
        # under ~/.codevira/ via get_global_home, which respects HOME on
        # POSIX.)
        env["HOME"] = str(project.parent / "home")

        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "insights",
             "--project", str(project), "--since=30d", "--ascii"],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"insights CLI failed: stderr={result.stderr!r} stdout={result.stdout!r}"
        )
        out = result.stdout
        # Stable section
        assert "bcrypt over argon2" in out, (
            f"Stable decision missing from CLI output. stdout: {out!r}"
        )
        # Reverted section
        assert "Bootstrap not Tailwind" in out, (
            f"Reverted decision missing from CLI output. stdout: {out!r}"
        )
        # Suggestion text on reverted (not locked) decision
        assert "consider locking" in out.lower() or "do_not_revert" in out.lower()

    def test_cli_runs_against_empty_project(
        self, tmp_path: Path,
    ):
        """Empty project — friendly message, exit 0."""
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "pyproject.toml").write_text("")

        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(tmp_path / "fake_home")

        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "insights",
             "--project", str(empty), "--ascii"],
            cwd=str(empty),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        # Either "No codevira data found" or "No outcomes recorded yet"
        # depending on whether the graph_db exists.
        assert (
            "No codevira data" in result.stdout
            or "No outcomes recorded" in result.stdout
        ), f"Empty-project CLI message missing. stdout: {result.stdout!r}"
