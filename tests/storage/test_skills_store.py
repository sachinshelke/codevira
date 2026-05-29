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
