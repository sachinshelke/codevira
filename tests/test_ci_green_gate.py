"""
test_ci_green_gate.py — G1.9: a release must not be published over a red CI.

4.2.0 was. Every local gate was green while the Python 3.10 job had not run a
single test for four days, and nothing in the gauntlet looked at CI. "Could not
check" must never read as "passed" (D000142), so most of these pin exit 2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_ci_green.sh"

pytestmark = [
    pytest.mark.skipif(
        not SCRIPT.exists(), reason="scripts/ is not shipped in the sdist"
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="bash"),
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f").write_text("1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "x")
    return repo


def _run(
    repo: Path, answer: Any, *, gh_exit: int = 0, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the gate with a fake ``gh`` that logs its arguments."""
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    text = answer if isinstance(answer, str) else json.dumps(answer)
    (repo.parent / "answer").write_text(text)
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/bash\necho "$@" > "{repo.parent}/args"\n'
        f'cat "{repo.parent}/answer"\nexit {gh_exit}\n'
    )
    gh.chmod(0o755)
    full = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    full["PATH"] = f"{bin_dir}:{full['PATH']}"
    full.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, env=full, capture_output=True, text=True
    )


def _ci(
    status: str = "completed",
    conclusion: str = "success",
    event: str = "push",
    at: str = "2026-01-01T00:00:00Z",
) -> dict[str, str]:
    return {
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "createdAt": at,
        "url": "https://example.test/run",
    }


def test_green_passes_and_asks_about_this_commit(checkout: Path) -> None:
    r = _run(checkout, [_ci()])
    assert r.returncode == 0, r.stdout
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
    ).stdout.strip()
    assert f"--commit {sha}" in (checkout.parent / "args").read_text()


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "timed_out", "startup_failure", "skipped"]
)
def test_anything_but_success_fails(checkout: Path, conclusion: str) -> None:
    r = _run(checkout, [_ci(conclusion=conclusion)])
    assert r.returncode == 1
    assert "example.test" in r.stdout, "names the run to look at"


def test_one_failing_kind_of_run_fails(checkout: Path) -> None:
    r = _run(checkout, [_ci(), _ci(conclusion="failure", event="pull_request")])
    assert r.returncode == 1


def test_a_failure_is_reported_even_while_another_run_is_pending(
    checkout: Path,
) -> None:
    r = _run(
        checkout,
        [_ci(conclusion="failure"), _ci("in_progress", "", "pull_request")],
    )
    assert r.returncode == 1


@pytest.mark.parametrize("newest_first", [True, False])
def test_a_newer_run_replaces_an_older_one(checkout: Path, newest_first: bool) -> None:
    runs = [_ci(conclusion="failure"), _ci(at="2026-01-02T00:00:00Z")]
    r = _run(checkout, runs[::-1] if newest_first else runs)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("newest_first", [True, False])
def test_an_older_success_does_not_hide_a_newer_failure(
    checkout: Path, newest_first: bool
) -> None:
    runs = [_ci(), _ci(conclusion="failure", at="2026-01-02T00:00:00Z")]
    r = _run(checkout, runs[::-1] if newest_first else runs)
    assert r.returncode == 1


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_unfinished_is_not_a_pass(checkout: Path, status: str) -> None:
    assert _run(checkout, [_ci(status, "")]).returncode == 2


@pytest.mark.parametrize(
    "answer, gh_exit", [([], 0), ("not json", 0), ("", 0), ("[]", 1), ("", 4)]
)
def test_no_answer_is_not_a_pass(checkout: Path, answer: Any, gh_exit: int) -> None:
    assert _run(checkout, answer, gh_exit=gh_exit).returncode == 2


def test_uncommitted_changes_are_not_a_pass(checkout: Path) -> None:
    (checkout / "f").write_text("2")
    assert _run(checkout, [_ci()]).returncode == 2


def test_inside_ci_it_does_not_wait_for_itself(checkout: Path) -> None:
    r = _run(checkout, [_ci()], env={"GITHUB_ACTIONS": "true"})
    assert r.returncode == 2


def test_without_gh_it_is_not_a_pass(checkout: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    for tool in ("git", "python3", "bash"):
        found = subprocess.run(
            ["which", tool], capture_output=True, text=True
        ).stdout.strip()
        (bare / tool).symlink_to(found)
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    r = subprocess.run(
        [str(bare / "bash"), str(SCRIPT)],
        cwd=checkout,
        env={**env, "PATH": str(bare)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2 and "gh" in r.stdout


def test_it_watches_the_workflow_that_exists() -> None:
    """A renamed workflow would leave this gate asking about nothing."""
    m = re.search(r"--workflow (\S+)", SCRIPT.read_text())
    assert m and (REPO / ".github" / "workflows" / m.group(1)).is_file()


def test_the_gauntlet_runs_it_and_records_the_verdict() -> None:
    text = (REPO / "Makefile").read_text()
    assert "scripts/check_ci_green.sh" in text
    assert '"G1_9_ci_green": %s' in text, "a constant would be a dead gate"


def test_gauntlet_evidence_line_has_one_argument_per_placeholder(
    tmp_path: Path,
) -> None:
    """printf shifts every verdict after a missing argument, silently."""
    m = re.search(
        r"printf '(\{\\n.*?\\n\}\\n)' (.*?) > ", (REPO / "Makefile").read_text()
    )
    assert m
    values = {
        "$(VERSION)": "9.9.9",
        "$$(git rev-parse HEAD 2>/dev/null)": "c0ffee",
        "$$(date -u +%Y-%m-%dT%H:%M:%SZ)": "T",
        "$$G19_RESULT": '"skipped"',
        "$$G3_RESULT": "true",
        "$$G4_RESULT": '"warn"',
    }
    args = re.findall(r'"([^"]+)"', m.group(2))
    out = subprocess.run(
        ["bash", "-c", 'printf "$@"', "_", m.group(1), *(values[a] for a in args)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    got = json.loads(out)
    assert got["version"] == "9.9.9" and got["commit"] == "c0ffee"
    assert got["G1_9_ci_green"] == "skipped"
    assert got["G3_real_ide_smoke"] is True
    assert got["G4_crash_log_clean"] == "warn"
    assert got["G5_human_confirmed"] is False
