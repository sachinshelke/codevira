"""4.0 — the other half of the skill loop: git-derived reinforcement.

Promotion (tests/test_working_promote_skill.py) gets a procedure INTO the
library. This is what makes it improve: `outcomes_writer.observe_all()`
classifies each decision from git history and fans the result out to the
skills used in that decision's session — `kept` is a success signal,
`reverted` a failure.

I expected to find this broken. It was not: the code was correct and had
no input, because nothing could create a skill until the promotion stub
was fixed. So these tests exist to pin a path that was never exercised in
production rather than to fix one — which is exactly the situation where
a regression goes unnoticed.

`modified` deliberately signals nothing. The decision's intent survived a
later edit, so neither "the skill worked" nor "the skill failed" is
supported by the evidence, and inventing a signal there would poison the
auto-archive sweep with noise.

One real (minor) product characteristic surfaced while writing these:
the classifier anchors on the decision's TIMESTAMP, so a decision and a
commit made within the same second are indistinguishable and classify as
`kept`. Sub-second decision → commit sequences therefore under-report.
The tests back-date the decision to match what a real
decide-then-revert-later sequence looks like, rather than working around
the check.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import (
    decisions_store,
    outcomes_writer,
    paths,
    sessions_store,
    skills_store,
)
from mcp_server.tools.working import working_add, working_promote


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")

    (root / "rebase.py").write_text("def rebase():\n    return 'autostash'\n")
    _commit(root, "init")
    return root


def _commit(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=root, check=True, capture_output=True
    )


def _skill_from_working(tags: list[str] | None = None) -> str:
    e = working_add("Use --autostash when rebasing onto main", kind="observation")
    return working_promote(e["entry_id"], to="skill", tags=tags or ["git"])["skill_id"]


def _backdate(root: Path, decision_id: str, ts: str) -> None:
    """Move a decision's timestamp back.

    The classifier anchors on the decision's ``ts`` to find commits that
    came AFTER it. A test writes the decision and the commit inside the
    same second, so without this every case classifies as ``kept``. This
    makes the fixture look like a decision made a day before the commit,
    which is the situation the classifier was built for.
    """
    p = root / ".codevira" / "decisions.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    for r in rows:
        if r.get("id") == decision_id:
            r["ts"] = ts
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    decisions_store.invalidate_merged_cache()


def _decision_in_session(session_id: str, skill_id: str) -> str:
    did = decisions_store.record(
        "Rebase with --autostash", file_path="rebase.py", session_id=session_id
    )
    sessions_store.write(
        session_id, task="rebase work", decision_ids=[did], skill_ids=[skill_id]
    )
    return did


class TestTheLoopIsLive:
    def test_a_kept_decision_reinforces_the_skill(self, repo: Path) -> None:
        """The whole point: a skill improves without anyone calling
        apply_skill_outcome by hand."""
        kid = _skill_from_working()
        _decision_in_session("s1", kid)

        # Later work does NOT touch the decision's file — it held.
        (repo / "other.py").write_text("y = 2\n")
        _commit(repo, "fix: unrelated change")

        summary = outcomes_writer.observe_all()
        assert summary["kept"] == 1
        assert summary["skill_marks_success"] == 1
        assert skills_store.get(kid)["success_count"] == 1

    def test_the_session_join_is_what_carries_it(self, repo: Path) -> None:
        """A skill in a DIFFERENT session must not be reinforced by this
        decision — the fan-out keys on session_id."""
        used = _skill_from_working(tags=["used"])
        unrelated = working_promote(
            working_add("Something else entirely", kind="observation")["entry_id"],
            to="skill",
            tags=["other"],
        )["skill_id"]
        _decision_in_session("s1", used)

        (repo / "other.py").write_text("y = 2\n")
        _commit(repo, "fix: unrelated change")
        outcomes_writer.observe_all()

        assert skills_store.get(used)["success_count"] == 1
        assert skills_store.get(unrelated)["success_count"] == 0


class TestItOnlySignalsWhatTheEvidenceSupports:
    def test_a_reverted_decision_marks_failure(self, repo: Path) -> None:
        """The other fan-out branch: the decision was undone, so the skill
        that produced it gets a failure mark. Five of these auto-archive
        the skill, which is the mechanism that stops a bad procedure from
        being recommended forever.

        The decision is back-dated because the classifier anchors on its
        timestamp, and a test writes the decision and the commit inside
        the same second. Back-dating is what a real revert-a-day-later
        looks like to the classifier — it is making the test match
        reality, not working around the check.
        """
        kid = _skill_from_working()
        did = _decision_in_session("s1", kid)
        _backdate(repo, did, "2020-01-01T00:00:00+00:00")

        (repo / "rebase.py").write_text("def rebase():\n    return 'merge'\n")
        _commit(repo, "revert the autostash change")

        summary = outcomes_writer.observe_all()
        assert summary["reverted"] == 1
        assert summary["skill_marks_failure"] == 1
        assert skills_store.get(kid)["failure_count"] == 1

    def test_modified_signals_nothing(self, repo: Path) -> None:
        """A later edit that is NOT a revert. The decision's intent
        survived, so neither "worked" nor "failed" is supported, and
        inventing a signal would poison the auto-archive sweep."""
        kid = _skill_from_working()
        did = _decision_in_session("s1", kid)
        _backdate(repo, did, "2020-01-01T00:00:00+00:00")

        (repo / "rebase.py").write_text("def rebase():\n    return 'autostash2'\n")
        _commit(repo, "fix: tweak the flag")

        summary = outcomes_writer.observe_all()
        assert summary["modified"] == 1
        assert summary["skill_marks_success"] == 0
        assert summary["skill_marks_failure"] == 0
        assert skills_store.get(kid)["success_count"] == 0

    def test_a_deleted_file_counts_as_reverted(self, repo: Path) -> None:
        """No file left to hold the decision."""
        kid = _skill_from_working()
        _decision_in_session("s1", kid)
        (repo / "rebase.py").unlink()
        _commit(repo, "remove the module")

        summary = outcomes_writer.observe_all()
        assert summary["reverted"] == 1
        assert skills_store.get(kid)["failure_count"] == 1

    def test_a_decision_with_no_file_path_is_unclassified(self, repo: Path) -> None:
        """Classification reads git history for a file. No file, no
        evidence, no signal."""
        kid = _skill_from_working()
        did = decisions_store.record("No file attached", session_id="s1")
        sessions_store.write("s1", decision_ids=[did], skill_ids=[kid])

        (repo / "other.py").write_text("y = 2\n")
        _commit(repo, "fix: unrelated")

        summary = outcomes_writer.observe_all()
        assert summary["skill_marks_success"] == 0
        assert skills_store.get(kid)["success_count"] == 0

    def test_a_session_with_no_skills_is_a_no_op(self, repo: Path) -> None:
        _decision_in_session_without_skill = decisions_store.record(
            "Rebase with --autostash", file_path="rebase.py", session_id="s1"
        )
        sessions_store.write("s1", decision_ids=[_decision_in_session_without_skill])
        (repo / "other.py").write_text("y = 2\n")
        _commit(repo, "fix: unrelated")

        summary = outcomes_writer.observe_all()
        assert summary["kept"] == 1
        assert summary["skill_marks_success"] == 0


class TestItNeverBreaksTheDecisionOutcome:
    def test_a_skills_failure_does_not_lose_the_decision_outcome(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fan-out is best-effort by design. A broken skills_store
        must not cost the user their decision-outcome classification,
        which is the primary product of this pass."""
        kid = _skill_from_working()
        _decision_in_session("s1", kid)
        (repo / "other.py").write_text("y = 2\n")
        _commit(repo, "fix: unrelated")

        def boom(*a, **k):
            raise RuntimeError("skills_store is down")

        monkeypatch.setattr(skills_store, "mark_used", boom)
        summary = outcomes_writer.observe_all()

        assert summary["kept"] == 1, "the decision outcome must still land"
        assert summary["skill_marks_success"] == 0
