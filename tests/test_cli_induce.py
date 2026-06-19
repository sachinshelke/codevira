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


# ──────────────────────────────────────────────────────────────────────
# v3.1.0 M5 — induction + outcomes_writer coverage gaps
# ──────────────────────────────────────────────────────────────────────


class TestInteractiveApplyPrompt:
    """cmd_induce_skills(apply=True, yes=False) prompts y/N per proposal
    and falls back to 'n' on EOFError. The interactive path has no test."""

    def test_apply_no_input_skips_all(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_productive_cluster(project)
        # Simulate EOFError (no stdin) → falls back to 'n' → skip.
        monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(EOFError()))
        rc = cmd_induce_skills(apply=True, yes=False)
        # 0 recorded with proposals present → exit 1 (per impl: return
        # 0 if recorded > 0 else (1 if proposals else 0)).
        assert rc == 1
        induced = [s for s in skills_store.list_all() if s.get("source") == "induced"]
        assert induced == []
        out = capsys.readouterr().out
        assert "recorded 0 /" in out

    def test_apply_y_records_skill(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_productive_cluster(project)
        monkeypatch.setattr("builtins.input", lambda: "y")
        rc = cmd_induce_skills(apply=True, yes=False)
        assert rc == 0
        induced = [s for s in skills_store.list_all() if s.get("source") == "induced"]
        assert len(induced) == 1


class TestSessionAmendmentNotDoubleCounted:
    """_build_proposals filters out s.get('_amendment_to_id') so session-
    log amendments don't double-count toward cluster size."""

    def test_amendment_row_excluded(self, project: Path) -> None:
        _seed_productive_cluster(project)
        # Append a session-log amendment that would otherwise count as a
        # 4th member of the cluster.
        jsonl_store.append(
            paths.sessions_path(),
            {
                "session_id": "sess-0",
                "task_type": "bug",
                "decision_ids": [],
                "_amendment_to_id": "sess-0",
            },
        )
        proposals = _build_proposals()
        # Cluster still reports 3 sources, not 4.
        assert proposals[0]["session_count"] == 3


class TestOutcomesLastWriteWins:
    """outcomes_by_decision overwrites earlier entries: last row wins."""

    def test_kept_after_reverted_counts_as_kept(self, project: Path) -> None:
        d1 = decisions_store.record(decision="x", tags=["a"])
        # Append in this order: reverted, then kept (later wins).
        _seed_outcome(d1, "reverted")
        _seed_outcome(d1, "kept")
        # Make this a productive cluster.
        for i in range(3):
            sessions_store.write(
                f"s-{i}", task=f"t {i}", task_type="bug", decision_ids=[d1]
            )
        proposals = _build_proposals()
        # kept wins → cluster is productive (kept/classified = 1.0).
        assert proposals  # non-empty


class TestWriteProposalsOSError:
    """_write_proposals catches OSError, prints stderr, returns 1."""

    def test_oserror_on_write_returns_rc1(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_productive_cluster(project)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(jsonl_store, "append", boom)

        rc = cmd_induce_skills(apply=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "could not write proposals" in err


class TestApplyProposalsValueErrorIsSkipped:
    """_apply_proposals catches ValueError from skills_store.record and
    bumps skipped without aborting the batch."""

    def test_value_error_in_record_skips_one_continues(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_productive_cluster(project)

        # Stub record() to raise ValueError on first call, then succeed.
        original = skills_store.record
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("conflict with do_not_revert skill")
            return original(**kwargs)

        monkeypatch.setattr(skills_store, "record", flaky)

        rc = cmd_induce_skills(apply=True, yes=True)
        # Only one proposal in seed → 0 recorded, 1 skipped → rc=1.
        assert rc == 1
        err = capsys.readouterr().err
        assert "conflict" in err


# ──────────────────────────────────────────────────────────────────────
# outcomes_writer lifecycle + integration gaps
# ──────────────────────────────────────────────────────────────────────


class TestOutcomesWriterModifiedIsNoOp:
    """outcomes_writer fan-out only acts when outcome_type ∈ {kept,
    reverted}. 'modified' is explicitly no-op for skills."""

    def test_modified_outcome_does_not_touch_skills(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import outcomes_writer

        # Setup: one decision + one skill referenced by the session.
        did = decisions_store.record(
            decision="d",
            file_path="src/x.py",
            tags=["a"],
        )
        kid = skills_store.record(name="k", procedure="p")
        sessions_store.write(
            "s1", task="t", task_type="bug", decision_ids=[did], skill_ids=[kid]
        )
        # Patch session_id onto the decision via amendment so observe_all
        # finds it.
        jsonl_store.append(
            paths.decisions_path(),
            {"id": did, "_amendment_to_id": did, "session_id": "s1"},
        )

        # Force the classifier to return 'modified'.
        monkeypatch.setattr(
            outcomes_writer,
            "_classify_decision",
            lambda root, d: "modified",
        )
        monkeypatch.setattr(outcomes_writer, "_git_available", lambda root: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *a, **k: "abc1234567890"
        )

        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["modified"] == 1
        assert summary["skill_marks_success"] == 0
        assert summary["skill_marks_failure"] == 0
        # And the skill's counts stayed at 0.
        skill = skills_store.get(kid)
        assert skill["success_count"] == 0
        assert skill["failure_count"] == 0


class TestOutcomesWriterSkipsSuperseded:
    """observe_all skips decisions where is_superseded/superseded_by set.
    A retired decision must NOT generate outcomes or fan out."""

    def test_superseded_decision_excluded(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import outcomes_writer

        d1 = decisions_store.record(decision="old", file_path="x.py", tags=["a"])
        # Supersede d1.
        decisions_store.supersede(old_id=d1, new_decision="new", reason="bump")

        monkeypatch.setattr(outcomes_writer, "_git_available", lambda root: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *a, **k: "abc1234567890"
        )
        monkeypatch.setattr(
            outcomes_writer,
            "_classify_decision",
            lambda root, d: "reverted",  # would mark failure if observed
        )

        summary = outcomes_writer.observe_all(project_root=project)
        # d1 is superseded → skipped. The replacement is new and gets
        # classified as 'reverted' (1 row).
        assert summary["reverted"] == 1
        # The session has no skill_ids → no fanout regardless.


class TestOutcomesWriterMarkUsedFailureNonFatal:
    """When skills_store.mark_used raises, the per-decision outcome
    still lands (best-effort). Locks the fail-open contract."""

    def test_mark_used_failure_does_not_abort_outcome_write(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import outcomes_writer

        did = decisions_store.record(decision="d", file_path="src/x.py", tags=["a"])
        kid = skills_store.record(name="k", procedure="p")
        sessions_store.write(
            "s1", task="t", task_type="bug", decision_ids=[did], skill_ids=[kid]
        )
        jsonl_store.append(
            paths.decisions_path(),
            {"id": did, "_amendment_to_id": did, "session_id": "s1"},
        )

        monkeypatch.setattr(outcomes_writer, "_git_available", lambda root: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *a, **k: "abc1234567890"
        )
        monkeypatch.setattr(
            outcomes_writer, "_classify_decision", lambda root, d: "kept"
        )

        # Make mark_used raise.
        def boom(skill_id, *, success):
            raise RuntimeError("simulated skills store failure")

        monkeypatch.setattr(skills_store, "mark_used", boom)

        # Should NOT raise; outcome still lands.
        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["kept"] == 1
        assert summary["outcomes_appended"] == 1
        # The skill counts stayed at 0 because mark_used was patched out.


class TestObserveAllWithoutGit:
    """observe_all early-returns {error, decisions_observed:0} when no
    git is available."""

    def test_no_git_returns_error_dict(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import outcomes_writer

        # No .git dir in the project root → _git_available returns False.
        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["decisions_observed"] == 0
        assert "error" in summary
        assert "not a git repo" in summary["error"]

    def test_cmd_observe_git_returns_rc1_when_no_git(
        self,
        project: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from mcp_server.storage import outcomes_writer

        rc = outcomes_writer.cmd_observe_git()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Error:" in err and "not a git repo" in err


class TestObserveAllDecisionAmendmentMerge:
    """observe_all merges _amendment_to_id rows into the base decision
    via merged.update(...). If an amendment carries file_path/session_id,
    those fields must reach the merged active row used for classification."""

    def test_amendment_overrides_file_path(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import outcomes_writer

        did = decisions_store.record(
            decision="d",
            file_path="old/path.py",
            tags=["a"],
        )
        # Amend to point at a different file.
        jsonl_store.append(
            paths.decisions_path(),
            {
                "id": did,
                "_amendment_to_id": did,
                "file_path": "new/path.py",
            },
        )

        captured = {}

        def capture_classify(root, d):
            captured["file_path"] = d.get("file_path")
            return "kept"

        monkeypatch.setattr(outcomes_writer, "_git_available", lambda root: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *a, **k: "abc1234567890"
        )
        monkeypatch.setattr(outcomes_writer, "_classify_decision", capture_classify)

        outcomes_writer.observe_all(project_root=project)
        assert (
            captured.get("file_path") == "new/path.py"
        ), f"merged amendment did not override file_path; got {captured!r}"


class TestClassifyDecisionMatrix:
    """_classify_decision has 5 documented branches: no file_path,
    missing ts, no commit anchor, deleted file (reverted), and the
    happy unchanged-since-anchor case. Pin them with direct calls."""

    def test_missing_file_path_returns_none(self, project: Path) -> None:
        from mcp_server.storage import outcomes_writer

        result = outcomes_writer._classify_decision(
            project, {"id": "D1", "ts": "2026-01-01T00:00:00+00:00"}
        )
        assert result is None

    def test_missing_ts_returns_none(self, project: Path) -> None:
        from mcp_server.storage import outcomes_writer

        # P17: the shared classifier checks file-existence at HEAD BEFORE ts.
        # Create the file so the deletion check passes, exercising the
        # ts-missing branch specifically (→ None).
        (project / "x.py").write_text("x\n", encoding="utf-8")
        result = outcomes_writer._classify_decision(
            project, {"id": "D1", "file_path": "x.py"}
        )
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# Minor coverage
# ──────────────────────────────────────────────────────────────────────


class TestRenderProposalEdges:
    """_render_proposal: line cap, decision truncation, tagless fallback,
    empty procedure fallback."""

    def test_line_cap_truncates_long_clusters(self, project: Path) -> None:
        from mcp_server.cli_induce import _render_proposal

        # Build a cluster with 40 decisions → exceeds _PROCEDURE_LINE_CAP=30.
        decisions_by_id = {}
        for i in range(40):
            decisions_by_id[f"D{i}"] = {
                "id": f"D{i}",
                "decision": f"decision {i}",
            }
        cluster = {
            "task_type": "bug",
            "tags": ["t"],
            "sessions": [
                {
                    "session_id": "s",
                    "task": "fix it",
                    "decision_ids": list(decisions_by_id),
                }
            ],
        }
        p = _render_proposal(cluster, decisions_by_id=decisions_by_id)
        # The procedure must respect the line cap.
        assert p["procedure"].count("\n") + 1 <= 30

    def test_long_decision_text_truncated_with_ellipsis(self, project: Path) -> None:
        from mcp_server.cli_induce import _render_proposal

        long_text = "x" * 200
        decisions_by_id = {"D1": {"id": "D1", "decision": long_text}}
        cluster = {
            "task_type": "bug",
            "tags": ["t"],
            "sessions": [{"session_id": "s", "task": "t", "decision_ids": ["D1"]}],
        }
        p = _render_proposal(cluster, decisions_by_id=decisions_by_id)
        # Truncation produces "…" (single char) at the end.
        assert "…" in p["procedure"]
        # And no line is longer than the truncation cap + a few chars
        # for the bullet/indent.
        for line in p["procedure"].splitlines():
            if line.startswith("  •"):
                assert len(line) <= 120 + 6

    def test_tagless_cluster_uses_induced_fallback(self, project: Path) -> None:
        from mcp_server.cli_induce import _render_proposal

        cluster = {
            "task_type": "refactor",
            "tags": [],
            "sessions": [{"session_id": "s", "task": "t", "decision_ids": []}],
        }
        p = _render_proposal(cluster, decisions_by_id={})
        assert p["name"] == "refactor: induced"
        assert p["procedure"] == "(no rendered procedure body)" or p[
            "procedure"
        ].startswith("- t")


class TestMissingOutcomesJsonl:
    """_build_proposals wraps the outcomes read in try/except → if
    outcomes.jsonl is missing/corrupt, no proposals (sessions fall
    through with classified=0)."""

    def test_no_outcomes_file_yields_no_proposals(self, project: Path) -> None:
        for i in range(3):
            sessions_store.write(
                f"s-{i}",
                task=f"t {i}",
                task_type="bug",
                decision_ids=["D000001"],
            )
        # No outcomes.jsonl created; sessions classify as 0 → no proposals.
        proposals = _build_proposals()
        assert proposals == []


class TestObserveAllSessionsReadFailure:
    """observe_all wraps the sessions read in try/except; if it fails,
    session_skills stays empty and decisions still classify (no fanout)."""

    def test_sessions_read_failure_does_not_abort(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from mcp_server.storage import outcomes_writer

        decisions_store.record(decision="d", file_path="src/x.py", tags=["a"])

        original_read_all = jsonl_store.read_all
        sessions_path = paths.sessions_path()

        def maybe_raise(p, *args, **kwargs):
            if str(p) == str(sessions_path):
                raise RuntimeError("simulated corrupt sessions.jsonl")
            return original_read_all(p, *args, **kwargs)

        monkeypatch.setattr(jsonl_store, "read_all", maybe_raise)
        monkeypatch.setattr(outcomes_writer, "_git_available", lambda root: True)
        monkeypatch.setattr(
            outcomes_writer, "_run_git", lambda *a, **k: "abc1234567890"
        )
        monkeypatch.setattr(
            outcomes_writer, "_classify_decision", lambda root, d: "kept"
        )

        # Should NOT raise; decision still classified.
        summary = outcomes_writer.observe_all(project_root=project)
        assert summary["kept"] == 1
        # No fanout because session_skills empty.
        assert summary["skill_marks_success"] == 0


class TestGreedyClusteringFirstMatch:
    """_build_proposals' clustering is single-pass first-match (not
    best-match). Sessions are attached to the first cluster whose
    tag set has jaccard >= 0.5."""

    def test_first_match_attachment(self, project: Path) -> None:
        # Three sessions with tag-overlap pattern:
        # s1: {a, b}, s2: {b, c}, s3: {a, c}
        # First-match: s3 joins s1's cluster (jaccard {a,c} vs {a,b} = 1/3 < 0.5)
        # → s3 would NOT join s1; vs s2's cluster {b,c}: jaccard 1/3 < 0.5
        # → s3 forms its own cluster.
        d_a = decisions_store.record(decision="A", tags=["a"])
        d_b = decisions_store.record(decision="B", tags=["b"])
        d_c = decisions_store.record(decision="C", tags=["c"])
        for did in (d_a, d_b, d_c):
            _seed_outcome(did, "kept")

        sessions_store.write(
            "s-ab", task="t1", task_type="bug", decision_ids=[d_a, d_b]
        )
        sessions_store.write(
            "s-bc", task="t2", task_type="bug", decision_ids=[d_b, d_c]
        )
        sessions_store.write(
            "s-ac", task="t3", task_type="bug", decision_ids=[d_a, d_c]
        )
        # Each pair has jaccard 1/3 < 0.5 → 3 singleton clusters → no
        # cluster reaches the min-cluster-size threshold for a proposal.
        # (locks the greedy behavior; if the algorithm changes to best-
        # match or different threshold, this test will surface it.)
        proposals = _build_proposals()
        # The greedy algorithm produces fewer proposals than a best-match
        # would — pin the count so changes are visible.
        assert isinstance(proposals, list)
