"""4.0 — `working_promote(to="skill")` actually writes a skill.

The branch had been a stub since v3.1.0, returning::

    {"promoted": False, "deferred": True, "milestone": "M3",
     "hint": "Skill promotion lands in v3.1.0 M3 (skills_store)."}

`skills_store` shipped IN v3.1.0. The stub outlived the thing it was
waiting for by months, and in the meantime the one path that turns an
observation into a reusable procedure silently did nothing — while
returning success-shaped JSON an agent had no way to read as failure.

That is the mechanism behind `record_skill` sitting uncalled: the
automatic path never worked, so the library only ever grew when someone
authored a skill by hand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import paths, working_store
from mcp_server.tools.working import working_add, working_promote


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    return root


def _skills(root: Path) -> list[dict]:
    p = root / ".codevira" / "skills.jsonl"
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _add(content: str, **kw) -> str:
    return working_add(content, **kw)["entry_id"]


class TestItActuallyWrites:
    def test_a_promoted_entry_becomes_a_skill(self, project: Path) -> None:
        """The regression. Before this, skills.jsonl stayed empty."""
        eid = _add("Rebase onto main with --autostash, never merge")
        res = working_promote(eid, to="skill")

        assert res["promoted"] is True, res
        assert res["skill_id"]
        skills = _skills(project)
        assert len(skills) == 1
        assert "autostash" in skills[0]["procedure"]

    def test_it_no_longer_reports_deferred(self, project: Path) -> None:
        """Guard the exact stub shape, so a revert is loud."""
        eid = _add("some procedure")
        res = working_promote(eid, to="skill")
        assert "deferred" not in res
        assert "milestone" not in res

    def test_the_first_line_becomes_the_name(self, project: Path) -> None:
        """The agent's own summary beats anything derived here."""
        eid = _add("Use --autostash when rebasing\n\nLong explanation follows.")
        res = working_promote(eid, to="skill")
        assert res["name"] == "Use --autostash when rebasing"

    def test_tags_become_triggers(self, project: Path) -> None:
        eid = _add("procedure text")
        working_promote(eid, to="skill", tags=["git", "rebase"])
        assert set(_skills(project)[0]["triggers"]["tags"]) >= {"git", "rebase"}

    def test_the_source_entry_is_tombstoned(self, project: Path) -> None:
        """Otherwise the same observation can be promoted repeatedly."""
        eid = _add("procedure text")
        res = working_promote(eid, to="skill")
        assert res["source_tombstoned"] is True
        assert eid in working_store._tombstoned_ids()

    def test_it_is_retrievable_afterwards(self, project: Path) -> None:
        """A write nobody can read back is not a fix."""
        from mcp_server.tools.skills import get_skill

        eid = _add("Always run the migration against a copy of the real store first")
        working_promote(eid, to="skill", tags=["migration"])
        hits = get_skill(query="migration real store").get("hits") or []
        assert any("copy of the real store" in h.get("procedure", "") for h in hits)


class TestItRefusesRatherThanLosing:
    def test_a_duplicate_is_refused_and_the_entry_survives(self, project: Path) -> None:
        """If the write is refused, the observation must still be there —
        otherwise a conflict silently destroys it."""
        first = _add("Rebase onto main with --autostash, never merge")
        working_promote(first, to="skill")

        second = _add("Rebase onto main with --autostash, never merge")
        res = working_promote(second, to="skill")

        assert res["promoted"] is False
        assert res.get("conflict_warning")
        assert second not in working_store._tombstoned_ids(), (
            "a refused promotion must not tombstone the source"
        )

    def test_force_overrides_the_duplicate_check(self, project: Path) -> None:
        first = _add("Rebase onto main with --autostash, never merge")
        working_promote(first, to="skill")
        second = _add("Rebase onto main with --autostash, never merge")
        res = working_promote(second, to="skill", force=True)
        assert res["promoted"] is True
        assert len(_skills(project)) >= 2

    def test_an_empty_entry_is_refused(self, project: Path) -> None:
        eid = _add("   ")
        res = working_promote(eid, to="skill")
        assert res["promoted"] is False
        assert "no content" in res["error"]

    def test_a_missing_entry_is_refused(self, project: Path) -> None:
        res = working_promote("wm-does-not-exist", to="skill")
        assert res["promoted"] is False
        assert "not found" in res["error"]

    def test_an_already_promoted_entry_cannot_be_promoted_twice(
        self, project: Path
    ) -> None:
        eid = _add("procedure text")
        working_promote(eid, to="skill")
        again = working_promote(eid, to="skill")
        assert again["promoted"] is False
        assert "tombstoned" in again["error"]


class TestTheOtherBranchesAreUnchanged:
    def test_decision_promotion_still_works(self, project: Path) -> None:
        eid = _add("Cache TTL stays at 30s")
        res = working_promote(eid, to="decision")
        assert res.get("promoted") is True

    def test_playbook_is_still_deferred(self, project: Path) -> None:
        """Only the skill branch was implemented. The playbook branch
        needs a working-memory→task_type mapping that does not exist, and
        claiming otherwise would repeat the mistake being fixed."""
        eid = _add("procedure text")
        res = working_promote(eid, to="playbook")
        assert res["promoted"] is False
        assert res.get("deferred") is True

    def test_an_invalid_target_is_rejected(self, project: Path) -> None:
        eid = _add("x")
        res = working_promote(eid, to="nonsense")
        assert res["promoted"] is False
        assert "must be one of" in res["error"]
