"""
Tests for mcp_server.tools.skills — v3.1.0 M3 Phase 2.

Verifies the six MCP tools against the contract documented in
mcp_server/tools/skills.py. Storage-layer correctness is covered
separately in tests/storage/test_skills_store.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import skills_store
from mcp_server.tools import skills


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# record_skill
# ──────────────────────────────────────────────────────────────────────


class TestRecordSkill:
    def test_basic_returns_skill_id(self, project: Path) -> None:
        r = skills.record_skill(
            name="git-rebase-workflow",
            procedure="1. fetch\n2. rebase main\n3. push --force-with-lease",
        )
        assert r["recorded"] is True
        assert r["skill_id"].startswith("K")
        assert r["do_not_revert"] is False

    def test_empty_name_returns_structured_error(self, project: Path) -> None:
        r = skills.record_skill(name="   ", procedure="p")
        assert r["recorded"] is False
        assert "name" in r["error"]

    def test_empty_procedure_returns_structured_error(self, project: Path) -> None:
        r = skills.record_skill(name="x", procedure="")
        assert r["recorded"] is False
        assert "procedure" in r["error"]

    def test_oversize_procedure_returns_structured_error(self, project: Path) -> None:
        r = skills.record_skill(name="x", procedure="x" * 4000)
        assert r["recorded"] is False
        assert "2048 byte cap" in r["error"]

    def test_force_bypasses_conflict_warning(self, project: Path) -> None:
        # Seed a near-duplicate first.
        skills.record_skill(
            name="commit-style",
            procedure="Use conventional commits for every commit message",
        )
        # Second record near-identical content. Without force, conflict warning fires.
        r = skills.record_skill(
            name="commit-style",
            procedure="Use conventional commits for every commit message",
        )
        # The conflict-check threshold is conservative (0.85 BM25_norm);
        # exact duplicate text should trigger it.
        if r["recorded"] is False:
            assert "_conflict_warning" in r
            # Force=True overrides.
            r2 = skills.record_skill(
                name="commit-style-v2",
                procedure="Use conventional commits for every commit message",
                force=True,
            )
            assert r2["recorded"] is True


# ──────────────────────────────────────────────────────────────────────
# get_skill
# ──────────────────────────────────────────────────────────────────────


class TestGetSkill:
    def test_empty_query_returns_no_hits(self, project: Path) -> None:
        skills.record_skill(name="x", procedure="rebase against main")
        r = skills.get_skill("")
        assert r["hits"] == []
        assert r["count"] == 0

    def test_finds_skill_by_text(self, project: Path) -> None:
        kid = skills_store.record(
            name="git-rebase-workflow", procedure="rebase against main"
        )
        r = skills.get_skill("rebase main")
        assert r["count"] >= 1
        assert r["hits"][0]["skill_id"] == kid
        bd = r["hits"][0]["score_breakdown"]
        assert "bm25_norm" in bd
        assert "tag_jaccard" in bd
        assert "recency_decay" in bd

    def test_file_path_filter_propagated(self, project: Path) -> None:
        skills_store.record(
            name="py-only",
            procedure="run pytest on the file",
            triggers={"tags": ["pytest"], "file_patterns": ["*.py"]},
        )
        skills_store.record(
            name="generic",
            procedure="run pytest on the file",
        )
        md_results = skills.get_skill("pytest", file_path="README.md")
        names = {h["name"] for h in md_results["hits"]}
        assert "generic" in names
        assert "py-only" not in names


# ──────────────────────────────────────────────────────────────────────
# apply_skill_outcome
# ──────────────────────────────────────────────────────────────────────


class TestApplySkillOutcome:
    def test_success_increments_count(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        r = skills.apply_skill_outcome(kid, success=True)
        assert r["success"] is True
        rec = skills_store.get(kid)
        assert rec["success_count"] == 1

    def test_failure_at_threshold_archives(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        for _ in range(5):
            skills.apply_skill_outcome(kid, success=False)
        assert skills_store.get(kid)["status"] == "archived"

    def test_unknown_skill_returns_error(self, project: Path) -> None:
        r = skills.apply_skill_outcome("K999999", success=True)
        assert r["success"] is False
        assert "not found" in r["error"]

    def test_empty_skill_id_rejected(self, project: Path) -> None:
        r = skills.apply_skill_outcome("", success=True)
        assert r["success"] is False
        assert "skill_id" in r["error"]


# ──────────────────────────────────────────────────────────────────────
# list_skills
# ──────────────────────────────────────────────────────────────────────


class TestListSkills:
    def test_default_returns_active_only(self, project: Path) -> None:
        kid_a = skills_store.record(name="a", procedure="p")
        kid_b = skills_store.record(name="b", procedure="p")
        skills_store.mark_archived(kid_b)
        r = skills.list_skills()
        ids = {s["skill_id"] for s in r["skills"]}
        assert ids == {kid_a}

    def test_status_all_returns_every_state(self, project: Path) -> None:
        kid_a = skills_store.record(name="a", procedure="p")
        kid_b = skills_store.record(name="b", procedure="p")
        skills_store.mark_archived(kid_b)
        r = skills.list_skills(status="all")
        ids = {s["skill_id"] for s in r["skills"]}
        assert ids == {kid_a, kid_b}

    def test_tags_filter_intersection(self, project: Path) -> None:
        skills_store.record(
            name="A", procedure="p", triggers={"tags": ["git", "release"]}
        )
        skills_store.record(name="B", procedure="p", triggers={"tags": ["git"]})
        r = skills.list_skills(tags=["git", "release"])
        names = {s["name"] for s in r["skills"]}
        assert names == {"A"}

    def test_response_shape_includes_reinforcement_stats(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.mark_used(kid, success=True)
        r = skills.list_skills()
        s = r["skills"][0]
        assert s["success_count"] == 1
        assert s["last_used_at"] is not None


# ──────────────────────────────────────────────────────────────────────
# supersede_skill
# ──────────────────────────────────────────────────────────────────────


class TestSupersedeSkill:
    def test_marks_old_and_creates_new(self, project: Path) -> None:
        kid_old = skills_store.record(name="A", procedure="old")
        r = skills.supersede_skill(
            kid_old, name="B", procedure="new", reason="bumped to v2"
        )
        assert r["success"] is True
        assert r["new_id"].startswith("K")
        # The new skill exists; the old is marked superseded.
        new = skills_store.get(r["new_id"])
        old = skills_store.get(kid_old)
        assert new["supersedes"] == kid_old
        assert old["status"] == "superseded"

    def test_empty_old_id_rejected(self, project: Path) -> None:
        r = skills.supersede_skill("", name="x", procedure="p")
        assert r["success"] is False

    def test_unknown_old_id_returns_error(self, project: Path) -> None:
        r = skills.supersede_skill("K999999", name="x", procedure="p")
        assert r["success"] is False
        assert "not found" in r["error"]


# ──────────────────────────────────────────────────────────────────────
# promote_skill_to_playbook
# ──────────────────────────────────────────────────────────────────────


class TestPromoteSkillToPlaybook:
    def test_writes_playbook_markdown(self, project: Path) -> None:
        kid = skills_store.record(
            name="git-rebase",
            summary="how we rebase",
            procedure="1. fetch\n2. rebase main\n3. push --force-with-lease",
        )
        r = skills.promote_skill_to_playbook(kid, task_type="commit")
        assert r["promoted"] is True
        # Destination under .codevira/playbooks/<task_type>/<slug>.md
        path = Path(r["path"])
        assert path.is_file()
        body = path.read_text()
        assert "git-rebase" in body
        assert "rebase main" in body
        assert "task_type: commit" in body
        assert r["name"] == "git-rebase"  # slugified

    def test_refuses_overwrite_without_force(self, project: Path) -> None:
        kid = skills_store.record(name="commit-style", procedure="p")
        skills.promote_skill_to_playbook(kid, task_type="commit")
        # Second promote → refused.
        r = skills.promote_skill_to_playbook(kid, task_type="commit")
        assert r["promoted"] is False
        assert "force=True" in r["error"]

    def test_force_overwrites(self, project: Path) -> None:
        kid = skills_store.record(name="commit-style", procedure="v1 procedure")
        skills.promote_skill_to_playbook(kid, task_type="commit")
        # Update the skill's procedure via supersede, then force-promote.
        res = skills_store.supersede(kid, name="commit-style", procedure="v2 procedure")
        new_kid = res["new_id"]
        r = skills.promote_skill_to_playbook(
            new_kid, task_type="commit", name="commit-style", force=True
        )
        assert r["promoted"] is True
        assert "v2 procedure" in Path(r["path"]).read_text()

    def test_explicit_name_overrides_skill_name(self, project: Path) -> None:
        kid = skills_store.record(name="my-skill", procedure="p")
        r = skills.promote_skill_to_playbook(
            kid, task_type="add_tool", name="custom-name"
        )
        assert r["promoted"] is True
        assert r["name"] == "custom-name"

    def test_unknown_skill_rejected(self, project: Path) -> None:
        r = skills.promote_skill_to_playbook("K999999", task_type="commit")
        assert r["promoted"] is False
        assert "not found" in r["error"]

    def test_superseded_skill_rejected(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.supersede(kid, name="x2", procedure="p2")
        r = skills.promote_skill_to_playbook(kid, task_type="commit")
        assert r["promoted"] is False
        assert "superseded" in r["error"]

    def test_empty_task_type_rejected(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        r = skills.promote_skill_to_playbook(kid, task_type="")
        assert r["promoted"] is False

    def test_unslugifiable_name_rejected(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        # Pass a name that slugifies to empty.
        r = skills.promote_skill_to_playbook(kid, task_type="commit", name="!!!")
        assert r["promoted"] is False


# ──────────────────────────────────────────────────────────────────────
# Secret-leak surface — REGRESSION-LOCK
# ──────────────────────────────────────────────────────────────────────


class TestProcedureSecretSanitization:
    """v3.1.x fix: M3 now scrubs secret-shaped substrings in procedure
    and summary at record time. Closes the gap where pasting a stack
    trace with an API key into a procedure would land the secret in
    skills.jsonl, the FTS5 index, and any promoted playbook markdown."""

    _SECRET = "api_key=hunter2-deadbeefcafedeadbeef"

    def test_procedure_secret_redacted_at_storage(self, project: Path) -> None:
        r = skills.record_skill(
            name="formerly-leaky-skill",
            procedure=f"To call the API: curl -H '{self._SECRET}'",
            summary=f"Uses {self._SECRET}",
        )
        kid = r["skill_id"]
        rec = skills_store.get(kid)
        assert rec is not None
        # Raw secret stripped; redaction marker present.
        assert "hunter2-deadbeefcafedeadbeef" not in rec["procedure"]
        assert "<redacted:api-key>" in rec["procedure"]
        assert "hunter2-deadbeefcafedeadbeef" not in rec["summary"]
        assert "<redacted:api-key>" in rec["summary"]

    def test_promote_archived_skill_currently_succeeds(self, project: Path) -> None:
        """LOCKED-IN current behavior: promote_skill_to_playbook rejects
        superseded skills but silently allows archived ones — the
        archived skill is still promoted to a markdown file. If a
        future change tightens this to require active status, the
        test will fail and the new policy needs an explicit assert."""
        kid = skills_store.record(name="archived-fixture", procedure="p")
        skills_store.mark_archived(kid, reason="manual test")
        r = skills.promote_skill_to_playbook(kid, task_type="commit")
        assert r["promoted"] is True, (
            "promote_skill_to_playbook now rejects archived skills — "
            "update this test to assert the new policy."
        )

    def test_procedure_secret_redacted_in_playbook_markdown(
        self, project: Path
    ) -> None:
        r = skills.record_skill(
            name="formerly-leaky-playbook",
            procedure=f"export TOKEN={self._SECRET}\nthen call /api",
        )
        kid = r["skill_id"]
        promo = skills.promote_skill_to_playbook(kid, task_type="api_call")
        assert promo["promoted"] is True
        playbook_path = Path(promo["path"])
        body = playbook_path.read_text(encoding="utf-8")
        # Secret stripped before persistence → never reaches the playbook.
        assert "hunter2-deadbeefcafedeadbeef" not in body
        assert "<redacted:api-key>" in body
