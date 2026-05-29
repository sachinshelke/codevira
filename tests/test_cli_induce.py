"""
Tests for mcp_server.cli_induce — v3.1.0 M5.

Covers the deterministic induction pipeline + outcomes_writer skill
fan-out integration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.cli_induce import (
    _build_proposals,
    _jaccard,
    cmd_induce_skills,
)
from mcp_server.storage import (
    decisions_store,
    jsonl_store,
    paths,
    sessions_store,
    skills_store,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


def _seed_outcome(decision_id: str, outcome_type: str) -> None:
    """Helper: append a row to outcomes.jsonl."""
    jsonl_store.append(
        paths.outcomes_path(),
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "decision_id": decision_id,
            "outcome_type": outcome_type,
        },
    )


# ──────────────────────────────────────────────────────────────────────
# Jaccard helper
# ──────────────────────────────────────────────────────────────────────


class TestJaccard:
    def test_identical_sets_score_1(self) -> None:
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets_score_0(self) -> None:
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self) -> None:
        # {a, b} ∩ {b, c} = {b}; union = 3 → 1/3
        assert abs(_jaccard({"a", "b"}, {"b", "c"}) - (1 / 3)) < 1e-9

    def test_two_empty_sets_score_1(self) -> None:
        assert _jaccard(set(), set()) == 1.0


# ──────────────────────────────────────────────────────────────────────
# Pipeline: _build_proposals
# ──────────────────────────────────────────────────────────────────────


class TestBuildProposals:
    def test_empty_state_returns_no_proposals(self, project: Path) -> None:
        assert _build_proposals() == []

    def test_below_threshold_skipped(self, project: Path) -> None:
        """Sessions where <80% of decisions are 'kept' don't propose."""
        d1 = decisions_store.record(decision="A", tags=["t1"])
        d2 = decisions_store.record(decision="B", tags=["t1"])
        _seed_outcome(d1, "kept")
        _seed_outcome(d2, "reverted")
        # 50% kept → below threshold.
        for i in range(3):
            sessions_store.write(
                f"sess-{i}",
                task=f"task {i}",
                task_type="bug",
                decision_ids=[d1, d2],
            )
        assert _build_proposals() == []

    def test_below_min_cluster_size_skipped(self, project: Path) -> None:
        """Clusters with <3 sessions don't propose."""
        d1 = decisions_store.record(decision="A", tags=["t1", "t2"])
        _seed_outcome(d1, "kept")
        for i in range(2):  # only 2 sessions
            sessions_store.write(
                f"sess-{i}",
                task=f"task {i}",
                task_type="bug",
                decision_ids=[d1],
            )
        assert _build_proposals() == []

    def test_productive_cluster_proposes_skill(self, project: Path) -> None:
        """3+ productive sessions sharing tags → 1 proposal."""
        d1 = decisions_store.record(
            decision="Use bcrypt", file_path="auth.py", tags=["auth", "hash"]
        )
        d2 = decisions_store.record(
            decision="Rate-limit logins", file_path="auth.py", tags=["auth"]
        )
        for did in (d1, d2):
            _seed_outcome(did, "kept")
        for i in range(3):
            sessions_store.write(
                f"sess-{i}",
                task=f"harden auth flow {i}",
                task_type="bug",
                decision_ids=[d1, d2],
            )
        proposals = _build_proposals()
        assert len(proposals) == 1
        p = proposals[0]
        assert p["task_type"] == "bug"
        assert "auth" in p["tags"]
        assert p["session_count"] == 3
        assert p["source_session_ids"] == ["sess-0", "sess-1", "sess-2"]
        assert "bug:" in p["name"]
        # Procedure includes session task + decision text.
        assert "harden auth flow" in p["procedure"]
        assert "bcrypt" in p["procedure"]

    def test_distinct_task_types_form_distinct_clusters(self, project: Path) -> None:
        d1 = decisions_store.record(decision="A", tags=["x"])
        d2 = decisions_store.record(decision="B", tags=["x"])
        _seed_outcome(d1, "kept")
        _seed_outcome(d2, "kept")
        for i in range(3):
            sessions_store.write(
                f"bug-{i}",
                task="x",
                task_type="bug",
                decision_ids=[d1],
            )
        for i in range(3):
            sessions_store.write(
                f"feat-{i}",
                task="x",
                task_type="feature",
                decision_ids=[d2],
            )
        proposals = _build_proposals()
        task_types = {p["task_type"] for p in proposals}
        assert task_types == {"bug", "feature"}

    def test_low_jaccard_sessions_dont_cluster(self, project: Path) -> None:
        """Sessions whose decision tags don't overlap above the
        threshold form separate (small, dropped) clusters."""
        d1 = decisions_store.record(decision="A", tags=["alpha"])
        d2 = decisions_store.record(decision="B", tags=["beta"])
        d3 = decisions_store.record(decision="C", tags=["gamma"])
        for did in (d1, d2, d3):
            _seed_outcome(did, "kept")
        sessions_store.write(
            "alpha-1", task="alpha", task_type="refactor", decision_ids=[d1]
        )
        sessions_store.write(
            "beta-1", task="beta", task_type="refactor", decision_ids=[d2]
        )
        sessions_store.write(
            "gamma-1", task="gamma", task_type="refactor", decision_ids=[d3]
        )
        # Three sessions but 3 disjoint single-session clusters → no proposals.
        assert _build_proposals() == []


# ──────────────────────────────────────────────────────────────────────
# cmd_induce_skills: dry-run + apply paths
# ──────────────────────────────────────────────────────────────────────


def _seed_productive_cluster(project: Path) -> None:
    d1 = decisions_store.record(
        decision="Use bcrypt for password hashing",
        tags=["auth", "security"],
    )
    d2 = decisions_store.record(
        decision="Rate-limit login attempts",
        tags=["auth", "security"],
    )
    for did in (d1, d2):
        _seed_outcome(did, "kept")
    for i in range(3):
        sessions_store.write(
            f"sess-{i}",
            task=f"harden authentication {i}",
            task_type="bug",
            decision_ids=[d1, d2],
        )


class TestCmdInduceSkills:
    def test_dry_run_writes_proposals_jsonl(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_productive_cluster(project)
        rc = cmd_induce_skills(apply=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "wrote 1 proposal" in out
        proposals_path = paths.induction_proposals_path()
        assert proposals_path.is_file()
        proposals = jsonl_store.read_all(proposals_path)
        assert len(proposals) == 1
        assert proposals[0]["task_type"] == "bug"

    def test_no_candidates_returns_zero(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_induce_skills(apply=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "no induced-skill candidates" in out
        # No proposals file written.
        assert not paths.induction_proposals_path().is_file()

    def test_apply_yes_records_skill(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_productive_cluster(project)
        rc = cmd_induce_skills(apply=True, yes=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "recorded" in out

        # The new skill is in skills.jsonl with source="induced" + session refs.
        live = skills_store.list_all()
        assert len(live) >= 1
        induced = [s for s in live if s.get("source") == "induced"]
        assert len(induced) == 1
        s = induced[0]
        # The induced skill carries its trigger tags + provenance refs;
        # task_type isn't on the skill schema (it lives on the source
        # sessions instead — induce-skills uses it for clustering only).
        assert sorted(s["triggers"]["tags"]) == ["auth", "security"]
        assert s["source_session_ids"] == ["sess-0", "sess-1", "sess-2"]


# ──────────────────────────────────────────────────────────────────────
# outcomes_writer skill fan-out
# ──────────────────────────────────────────────────────────────────────


class TestOutcomesWriterFanout:
    def test_kept_classification_marks_skill_success(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When outcomes_writer classifies a session's decision as
        'kept', each skill referenced by the session gets a
        mark_used(success=True) call."""
        # Build a skill, a session referencing it, and a decision in
        # the session.
        kid = skills_store.record(name="auth-skill", procedure="p")
        d1 = decisions_store.record(
            decision="Use bcrypt",
            file_path="auth.py",
            tags=["auth"],
            session_id="sess-1",
        )
        sessions_store.write(
            "sess-1",
            task="harden auth",
            decision_ids=[d1],
            skill_ids=[kid],
            task_type="bug",
        )

        # Stub _classify_decision so it always returns 'kept' — we don't
        # want to depend on git state in unit tests.
        from mcp_server.storage import outcomes_writer

        monkeypatch.setattr(
            outcomes_writer, "_classify_decision", lambda *_a, **_kw: "kept"
        )
        monkeypatch.setattr(outcomes_writer, "_git_available", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *_a, **_kw: "deadbeefcafe"
        )

        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["skill_marks_success"] == 1
        assert summary["skill_marks_failure"] == 0

        # The skill's success_count is incremented.
        skill = skills_store.get(kid)
        assert skill is not None
        assert skill["success_count"] == 1

    def test_reverted_classification_marks_skill_failure(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kid = skills_store.record(name="auth-skill", procedure="p")
        d1 = decisions_store.record(
            decision="Use bcrypt",
            file_path="auth.py",
            tags=["auth"],
            session_id="sess-1",
        )
        sessions_store.write(
            "sess-1",
            task="harden auth",
            decision_ids=[d1],
            skill_ids=[kid],
            task_type="bug",
        )

        from mcp_server.storage import outcomes_writer

        monkeypatch.setattr(
            outcomes_writer, "_classify_decision", lambda *_a, **_kw: "reverted"
        )
        monkeypatch.setattr(outcomes_writer, "_git_available", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *_a, **_kw: "deadbeefcafe"
        )

        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["skill_marks_failure"] == 1
        assert summary["skill_marks_success"] == 0
        skill = skills_store.get(kid)
        assert skill is not None
        assert skill["failure_count"] == 1
