"""
Tests for mcp_server.storage.skills_store — v3.1.0 M3 Phase 1.

Coverage:
  - record() input validation (name, procedure, summary, source)
  - schema (K-id, _schema_v: 1, origin stamp, normalized tags)
  - mark_used: success / failure / auto-archive at threshold / revive
  - set_flag: do_not_revert + tags
  - mark_archived + do_not_revert refusal
  - supersede chain + back-reference
  - list_all: status / source / tags filters
  - decay_sweep: auto-archive on unused threshold; do_not_revert exempt
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import jsonl_store, paths, skills_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# Record + schema
# ──────────────────────────────────────────────────────────────────────


class TestRecord:
    _ID_PATTERN = re.compile(r"^K\d{6}$")

    def test_basic_returns_k_id(self, project: Path) -> None:
        kid = skills_store.record(
            name="git-rebase-workflow",
            procedure="1. Fetch origin\n2. Rebase against main\n3. Push --force-with-lease",
        )
        assert self._ID_PATTERN.match(kid), kid

    def test_record_has_schema_v_and_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        skills_store.record(name="x", procedure="step 1", summary="short desc")
        rows = jsonl_store.read_all(paths.skills_path())
        rec = rows[0]
        assert rec["_schema_v"] == 1
        assert rec["origin"]["ide"] == "claude_code"
        assert rec["status"] == "active"
        assert rec["source"] == "explicit"

    def test_tags_lowercased_and_sorted(self, project: Path) -> None:
        skills_store.record(
            name="x",
            procedure="p",
            triggers={"tags": ["Z-Tag", "a-tag", "B-Tag"], "file_patterns": ["*.py"]},
        )
        rec = jsonl_store.read_all(paths.skills_path())[0]
        assert rec["triggers"]["tags"] == ["a-tag", "b-tag", "z-tag"]
        assert rec["triggers"]["file_patterns"] == ["*.py"]

    def test_empty_name_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="name"):
            skills_store.record(name="   ", procedure="p")

    def test_empty_procedure_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="procedure"):
            skills_store.record(name="x", procedure="")

    def test_oversize_procedure_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="2048 byte cap"):
            skills_store.record(name="x", procedure="x" * 2049)

    def test_oversize_summary_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="256 byte cap"):
            skills_store.record(name="x", procedure="p", summary="s" * 257)

    def test_invalid_source_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="source"):
            skills_store.record(name="x", procedure="p", source="hand-crafted")

    def test_procedure_token_estimate_populated(self, project: Path) -> None:
        skills_store.record(name="x", procedure="some procedure text here")
        rec = jsonl_store.read_all(paths.skills_path())[0]
        assert rec["procedure_token_estimate"] > 0


# ──────────────────────────────────────────────────────────────────────
# mark_used: reinforcement loop
# ──────────────────────────────────────────────────────────────────────


class TestMarkUsed:
    def test_success_increments_count(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        res = skills_store.mark_used(kid, success=True)
        assert res["success"] is True
        rec = skills_store.get(kid)
        assert rec["success_count"] == 1
        assert rec["failure_count"] == 0
        assert rec["consecutive_failures"] == 0
        assert rec["last_used_at"] is not None

    def test_failure_increments_count_and_consecutive(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        for _ in range(3):
            skills_store.mark_used(kid, success=False)
        rec = skills_store.get(kid)
        assert rec["failure_count"] == 3
        assert rec["consecutive_failures"] == 3
        assert rec["status"] == "active"  # below threshold

    def test_success_resets_consecutive_failures(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        for _ in range(3):
            skills_store.mark_used(kid, success=False)
        skills_store.mark_used(kid, success=True)
        rec = skills_store.get(kid)
        assert rec["consecutive_failures"] == 0
        assert rec["failure_count"] == 3
        assert rec["success_count"] == 1

    def test_auto_archive_at_5_consecutive_failures(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        for _ in range(5):
            skills_store.mark_used(kid, success=False)
        rec = skills_store.get(kid)
        assert rec["status"] == "archived"

    def test_do_not_revert_exempt_from_auto_archive(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p", do_not_revert=True)
        for _ in range(10):
            skills_store.mark_used(kid, success=False)
        rec = skills_store.get(kid)
        # do_not_revert protects from auto-archive even past the threshold.
        assert rec["status"] == "active"
        assert rec["consecutive_failures"] == 10

    def test_revival_after_archive(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        for _ in range(5):
            skills_store.mark_used(kid, success=False)
        # Auto-archived now.
        res = skills_store.mark_used(kid, success=True)
        assert res["revived"] is True
        rec = skills_store.get(kid)
        assert rec["status"] == "active"

    def test_unknown_skill_returns_error(self, project: Path) -> None:
        res = skills_store.mark_used("K999999", success=True)
        assert res["success"] is False
        assert "not found" in res["error"]


# ──────────────────────────────────────────────────────────────────────
# set_flag + mark_archived
# ──────────────────────────────────────────────────────────────────────


class TestSetFlag:
    def test_toggle_do_not_revert(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.set_flag(kid, do_not_revert=True)
        rec = skills_store.get(kid)
        assert rec["do_not_revert"] is True

    def test_update_tags(self, project: Path) -> None:
        kid = skills_store.record(
            name="x", procedure="p", triggers={"tags": ["old"], "file_patterns": []}
        )
        skills_store.set_flag(kid, tags=["new-tag", "another"])
        rec = skills_store.get(kid)
        assert sorted(rec["triggers"]["tags"]) == ["another", "new-tag"]

    def test_no_updates_is_noop(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        res = skills_store.set_flag(kid)
        assert res["updates"] == {}


class TestMarkArchived:
    def test_archive_active_skill(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.mark_archived(kid, reason="manual")
        rec = skills_store.get(kid)
        assert rec["status"] == "archived"

    def test_refuse_archive_do_not_revert(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p", do_not_revert=True)
        res = skills_store.mark_archived(kid)
        assert res["success"] is False
        assert "do_not_revert" in res["error"]


# ──────────────────────────────────────────────────────────────────────
# Supersession
# ──────────────────────────────────────────────────────────────────────


class TestSupersede:
    def test_supersede_marks_old_and_creates_new(self, project: Path) -> None:
        kid_old = skills_store.record(
            name="git-workflow-v1",
            procedure="rebase the manual way",
            triggers={"tags": ["git"], "file_patterns": ["*.py"]},
        )
        res = skills_store.supersede(
            kid_old,
            name="git-workflow-v2",
            procedure="rebase via the new alias",
            reason="moved to git-rebase-bot helper",
        )
        assert res["success"] is True
        kid_new = res["new_id"]
        assert kid_new != kid_old

        old = skills_store.get(kid_old)
        new = skills_store.get(kid_new)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == kid_new
        assert new["supersedes"] == kid_old
        # Triggers inherited from the old skill.
        assert new["triggers"]["tags"] == ["git"]
        assert new["triggers"]["file_patterns"] == ["*.py"]

    def test_supersede_explicit_triggers_override_inheritance(
        self, project: Path
    ) -> None:
        kid_old = skills_store.record(
            name="x",
            procedure="p",
            triggers={"tags": ["old"], "file_patterns": []},
        )
        res = skills_store.supersede(
            kid_old,
            name="x2",
            procedure="p2",
            triggers={"tags": ["new-tag"], "file_patterns": ["*.md"]},
        )
        new = skills_store.get(res["new_id"])
        assert new["triggers"]["tags"] == ["new-tag"]
        assert new["triggers"]["file_patterns"] == ["*.md"]

    def test_supersede_unknown_skill_rejected(self, project: Path) -> None:
        res = skills_store.supersede("K999999", name="x", procedure="p")
        assert res["success"] is False


# ──────────────────────────────────────────────────────────────────────
# list_all
# ──────────────────────────────────────────────────────────────────────


class TestListAll:
    def test_default_returns_active_only(self, project: Path) -> None:
        kid_a = skills_store.record(name="a", procedure="p")
        kid_b = skills_store.record(name="b", procedure="p")
        skills_store.mark_archived(kid_b)
        live = skills_store.list_all()
        assert [r["id"] for r in live] == [kid_a]

    def test_status_filter_archived(self, project: Path) -> None:
        kid_a = skills_store.record(name="a", procedure="p")
        skills_store.mark_archived(kid_a)
        archived = skills_store.list_all(status="archived")
        assert [r["id"] for r in archived] == [kid_a]

    def test_status_none_returns_all(self, project: Path) -> None:
        kid_a = skills_store.record(name="a", procedure="p")
        kid_b = skills_store.record(name="b", procedure="p")
        skills_store.mark_archived(kid_a)
        ids = {r["id"] for r in skills_store.list_all(status=None)}
        assert ids == {kid_a, kid_b}

    def test_source_filter(self, project: Path) -> None:
        skills_store.record(name="explicit", procedure="p", source="explicit")
        skills_store.record(name="induced", procedure="p", source="induced")
        only_induced = skills_store.list_all(source="induced")
        assert [r["name"] for r in only_induced] == ["induced"]

    def test_tags_filter_is_intersection(self, project: Path) -> None:
        skills_store.record(
            name="A", procedure="p", triggers={"tags": ["git", "release"]}
        )
        skills_store.record(name="B", procedure="p", triggers={"tags": ["git"]})
        skills_store.record(name="C", procedure="p", triggers={"tags": ["release"]})
        only_both = skills_store.list_all(tags=["git", "release"])
        assert [r["name"] for r in only_both] == ["A"]


# ──────────────────────────────────────────────────────────────────────
# decay_sweep
# ──────────────────────────────────────────────────────────────────────


class TestDecaySweep:
    def test_unused_skill_archived(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kid = skills_store.record(name="x", procedure="p")
        # 100 days later → past the 90-day cutoff.
        future = datetime(2027, 1, 1, tzinfo=timezone.utc) + timedelta(days=100)
        res = skills_store.decay_sweep(now=future)
        assert kid in res["archived"]
        rec = skills_store.get(kid)
        assert rec["status"] == "archived"

    def test_recently_used_not_archived(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.mark_used(kid, success=True)  # last_used_at = now
        res = skills_store.decay_sweep(now=datetime.now(timezone.utc))
        assert kid not in res["archived"]
        rec = skills_store.get(kid)
        assert rec["status"] == "active"

    def test_do_not_revert_skill_exempt_from_sweep(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p", do_not_revert=True)
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        res = skills_store.decay_sweep(now=future)
        assert kid not in res["archived"]
        assert skills_store.get(kid)["status"] == "active"

    def test_archived_skill_not_re_archived(self, project: Path) -> None:
        kid = skills_store.record(name="x", procedure="p")
        skills_store.mark_archived(kid)
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        res = skills_store.decay_sweep(now=future)
        # Already archived → skipped (not double-counted).
        assert kid not in res["archived"]


# ──────────────────────────────────────────────────────────────────────
# search (composite ranking — v3.1.0 M3 Phase 2)
# ──────────────────────────────────────────────────────────────────────


class TestSearch:
    """Composite ranking:
    score = 0.5 × BM25_norm + 0.3 × tag_jaccard + 0.2 × recency_decay
    """

    def test_empty_query_returns_empty(self, project: Path) -> None:
        skills_store.record(name="x", procedure="step 1\nstep 2")
        assert skills_store.search("") == []
        assert skills_store.search("   ") == []

    def test_finds_skill_by_procedure_text(self, project: Path) -> None:
        skills_store.record(
            name="git-rebase-workflow",
            procedure="Fetch origin then rebase against main",
            summary="how we rebase",
            triggers={"tags": ["git", "rebase"]},
        )
        results = skills_store.search("rebase main")
        assert len(results) == 1
        assert results[0]["name"] == "git-rebase-workflow"
        assert results[0]["score"] > 0
        # Composite breakdown surfaces for debug.
        bd = results[0]["score_breakdown"]
        assert "bm25_norm" in bd
        assert "tag_jaccard" in bd
        assert "recency_decay" in bd

    def test_excludes_archived_skills(self, project: Path) -> None:
        kid_a = skills_store.record(name="alpha", procedure="rebase against main")
        kid_b = skills_store.record(name="beta", procedure="rebase against main")
        skills_store.mark_archived(kid_b)
        results = skills_store.search("rebase main")
        ids = {r["id"] for r in results}
        assert kid_a in ids
        assert kid_b not in ids

    def test_excludes_superseded_skills(self, project: Path) -> None:
        kid_a = skills_store.record(name="v1", procedure="old way to rebase main")
        skills_store.supersede(kid_a, name="v2", procedure="new way to rebase main")
        results = skills_store.search("rebase main")
        ids = {r["id"] for r in results}
        assert kid_a not in ids
        # v2 still appears.
        assert any(r["name"] == "v2" for r in results)

    def test_tag_jaccard_boosts_score(self, project: Path) -> None:
        # Skill A: matches text only; no relevant tags.
        skills_store.record(
            name="A",
            procedure="run pytest with coverage",
            triggers={"tags": ["unrelated"]},
        )
        # Skill B: matches text AND shares tags with the query terms.
        skills_store.record(
            name="B",
            procedure="run pytest with coverage",
            triggers={"tags": ["pytest", "coverage"]},
        )
        results = skills_store.search("pytest coverage")
        # B should rank ABOVE A because tag_jaccard adds to the composite.
        names = [r["name"] for r in results]
        assert names.index("B") < names.index("A")

    def test_recency_decay_uses_last_used_at(self, project: Path) -> None:
        """Older skills decay; recently-used ones rank higher even at
        equal BM25."""
        skills_store.record(name="A", procedure="touch files")  # never used
        kid_new = skills_store.record(name="B", procedure="touch files")
        # Mark B as recently used to set last_used_at to ~now.
        skills_store.mark_used(kid_new, success=True)
        results = skills_store.search("touch files")
        # B (just used) should rank above A (never used).
        if len(results) == 2:
            assert results[0]["id"] == kid_new

    def test_file_path_filter(self, project: Path) -> None:
        # Skill with a Python-only file_patterns trigger.
        skills_store.record(
            name="py-specific",
            procedure="run pytest on the file",
            triggers={"tags": ["pytest"], "file_patterns": ["*.py"]},
        )
        # Skill with no patterns — matches anything.
        skills_store.record(
            name="generic",
            procedure="run pytest on the file",
            triggers={"tags": ["pytest"]},
        )
        # Searching for a Python file: both surface.
        py_results = skills_store.search("pytest", file_path="src/auth.py")
        py_names = {r["name"] for r in py_results}
        assert py_names == {"py-specific", "generic"}

        # Searching for a Markdown file: the py-specific skill is filtered out.
        md_results = skills_store.search("pytest", file_path="README.md")
        md_names = {r["name"] for r in md_results}
        assert md_names == {"generic"}

    def test_ranking_weights_overridable(self, project: Path) -> None:
        skills_store.record(
            name="A",
            procedure="rebase main",
            triggers={"tags": ["rebase", "main"]},
        )
        # All weight on tag jaccard — score should equal tag overlap.
        results = skills_store.search(
            "rebase main",
            ranking_weights={"bm25": 0.0, "tag": 1.0, "recency": 0.0},
        )
        assert len(results) == 1
        # With weights={0, 1, 0}, the composite = tag_jaccard.
        breakdown = results[0]["score_breakdown"]
        assert abs(results[0]["score"] - breakdown["tag_jaccard"]) < 1e-3

    def test_top_k_caps_results(self, project: Path) -> None:
        for i in range(10):
            skills_store.record(name=f"skill-{i}", procedure="some procedure text")
        results = skills_store.search("procedure", top_k=3)
        assert len(results) == 3

    def test_search_lazy_rebuild_on_stale_index(self, project: Path) -> None:
        """First search() on a fresh project rebuilds the index from
        skills.jsonl rather than returning empty."""
        kid = skills_store.record(name="x", procedure="rebase against main")
        # Force the FTS5 index to be stale by deleting it.
        from mcp_server.storage import paths as _paths

        if _paths.fts5_path().is_file():
            _paths.fts5_path().unlink()
        # Search should still work — rebuild kicks in.
        results = skills_store.search("rebase")
        assert any(r["id"] == kid for r in results)
