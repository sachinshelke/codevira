"""4.0 Step 10 — a way back for a directory git cannot roll back.

`.codevira/` is gitignored (.gitignore:61). So the rollback every developer
reaches for first — `git revert`, `git checkout --`, `git stash` — does not
exist for the one directory holding a team's decision history. This is the
blocking prerequisite for any 4.0 migration: before offering to rewrite
that store, there has to be an answer to "and if that goes wrong?"

The tests that matter most here are not the ones proving restore works.
They are the ones proving restore cannot LOSE anything — a rollback tool
people trust that can destroy data is worse than none at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server import memory_undo


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    monkeypatch.chdir(root)

    from mcp_server.storage import decisions_store, paths

    paths.ensure_dirs(root)
    (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    decisions_store.invalidate_merged_cache()

    # Private global home so snapshots never land in the real one. NOTE:
    # this patches get_global_home, NOT get_data_dir — patching the latter
    # is what made `test_snapshots_live_outside_the_project` vacuous and
    # hid a real infinite-recursion bug (see that test).
    home = tmp_path / "global"
    home.mkdir()
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    return root


def _decisions(root: Path) -> list[dict]:
    p = root / ".codevira" / "decisions.jsonl"
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _record(text: str) -> str:
    from mcp_server.storage import decisions_store

    decisions_store.invalidate_merged_cache()
    return decisions_store.record(text)


class TestItActuallyRollsBack:
    def test_a_wiped_store_comes_back(self, project: Path) -> None:
        _record("the decision that must survive")
        before = _decisions(project)
        assert before

        memory_undo.take(project, note="before the disaster")
        (project / ".codevira" / "decisions.jsonl").unlink()
        assert _decisions(project) == []

        res = memory_undo.restore(project)
        assert res["success"], res
        assert _decisions(project) == before

    def test_a_bad_rewrite_comes_back(self, project: Path) -> None:
        """The realistic case: a migration rewrote the file wrongly."""
        _record("original")
        before = _decisions(project)
        memory_undo.take(project)

        (project / ".codevira" / "decisions.jsonl").write_text(
            json.dumps({"id": "D999", "decision": "a migration ate everything"}) + "\n"
        )
        memory_undo.restore(project)
        assert _decisions(project) == before

    def test_a_named_snapshot_can_be_chosen(self, project: Path) -> None:
        _record("first")
        first = memory_undo.take(project, note="one")
        _record("second")
        memory_undo.take(project, note="two")

        assert len(_decisions(project)) == 2
        memory_undo.restore(project, snapshot=first.name)
        assert len(_decisions(project)) == 1

    def test_restore_defaults_to_the_most_recent(self, project: Path) -> None:
        _record("first")
        memory_undo.take(project)
        _record("second")
        memory_undo.take(project)
        (project / ".codevira" / "decisions.jsonl").unlink()

        memory_undo.restore(project)
        assert len(_decisions(project)) == 2


class TestUndoIsItselfUndoable:
    """The property that makes this safe to hand someone mid-incident."""

    def test_restoring_captures_the_current_state_first(self, project: Path) -> None:
        _record("old state")
        memory_undo.take(project, note="the one we will roll back to")
        _record("NEW work done after the snapshot")
        current = _decisions(project)
        assert len(current) == 2

        res = memory_undo.restore(project)
        assert res["safety_snapshot"], "an undo with no safety capture can destroy work"
        assert len(_decisions(project)) == 1  # rolled back

        # ...and the work done after the snapshot is recoverable.
        recovered = memory_undo.restore(project, snapshot=res["safety_snapshot"])
        assert recovered["success"]
        assert _decisions(project) == current

    def test_the_safety_snapshot_is_labelled(self, project: Path) -> None:
        """Someone scanning `memory list` mid-incident must be able to tell
        which entries are theirs and which the tool made."""
        _record("x")
        memory_undo.take(project)
        res = memory_undo.restore(project)
        safety = next(
            s
            for s in memory_undo.list_snapshots(project)
            if s.name == res["safety_snapshot"]
        )
        assert "pre-restore" in safety.note


class TestItRefusesRatherThanGuesses:
    def test_restore_with_no_snapshots_is_an_error_not_a_wipe(
        self, project: Path
    ) -> None:
        _record("live data")
        before = _decisions(project)
        res = memory_undo.restore(project)
        assert res["success"] is False
        assert "no snapshots" in res["error"]
        assert _decisions(project) == before, "the store must be untouched"

    def test_an_unknown_snapshot_name_is_an_error_not_a_wipe(
        self, project: Path
    ) -> None:
        _record("live data")
        memory_undo.take(project)
        before = _decisions(project)
        res = memory_undo.restore(project, snapshot="20200101T000000Z")
        assert res["success"] is False
        assert _decisions(project) == before

    def test_the_error_says_what_to_do(self, project: Path) -> None:
        res = memory_undo.restore(project)
        assert res.get("fix_command")

    def test_snapshotting_an_uninitialised_project_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(bare))
        monkeypatch.setattr(
            "mcp_server.paths.get_data_dir", lambda *a, **k: tmp_path / "d"
        )
        assert memory_undo.take(bare) is None


class TestWhatItCaptures:
    def test_it_snapshots_the_canonical_store(self, project: Path) -> None:
        _record("x")
        snap = memory_undo.take(project)
        assert (snap.path / "decisions.jsonl").is_file()
        assert (snap.path / "config.yaml").is_file()

    def test_snapshots_live_outside_the_project(self, project: Path) -> None:
        """Inside .codevira/ they would count against D00012K's 2MB cap AND
        become part of the thing being snapshotted.

        This test previously passed vacuously: the fixture monkeypatched
        `get_data_dir` to a tmp dir, so it never saw that in a legacy-layout
        project get_data_dir() IS <project>/.codevira/. Running the real CLI
        on a real repo produced copytree recursing into its own destination
        until the filename overflowed. The fixture now patches only the
        global home, so this exercises the resolution that actually ships.
        """
        snap = memory_undo.take(project)
        assert snap is not None
        assert project.resolve() not in snap.path.resolve().parents

    def test_it_refuses_to_snapshot_into_itself(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hard guard. Even if path resolution regresses, this must
        return None rather than recurse until the filesystem complains."""
        monkeypatch.setattr(
            memory_undo,
            "_snapshots_dir",
            lambda *a, **k: project / ".codevira" / "snaps",
        )
        assert memory_undo.take(project) is None
        assert not (project / ".codevira" / "snaps").exists()

    def test_a_manifest_records_what_and_why(self, project: Path) -> None:
        _record("x")
        snap = memory_undo.take(project, note="before the 4.0 migration")
        meta = json.loads((snap.path / memory_undo.MANIFEST_NAME).read_text())
        assert meta["note"] == "before the 4.0 migration"
        assert meta["files"] >= 2
        assert meta["project_root"] == str(project.resolve())

    def test_the_manifest_is_not_restored_into_the_store(self, project: Path) -> None:
        """It is snapshot metadata, not project memory."""
        _record("x")
        memory_undo.take(project)
        memory_undo.restore(project)
        assert not (project / ".codevira" / memory_undo.MANIFEST_NAME).exists()


class TestHousekeeping:
    def test_snapshots_list_newest_first(self, project: Path) -> None:
        _record("a")
        first = memory_undo.take(project, note="first")
        _record("b")
        second = memory_undo.take(project, note="second")
        names = [s.name for s in memory_undo.list_snapshots(project)]
        assert names.index(second.name) < names.index(first.name)

    def test_two_snapshots_in_the_same_second_do_not_collide(
        self, project: Path
    ) -> None:
        _record("x")
        a = memory_undo.take(project)
        b = memory_undo.take(project)
        assert a.name != b.name
        assert len(memory_undo.list_snapshots(project)) == 2

    def test_old_snapshots_are_pruned(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(memory_undo, "MAX_SNAPSHOTS", 3)
        _record("x")
        for _ in range(6):
            memory_undo.take(project)
        assert len(memory_undo.list_snapshots(project)) == 3

    def test_a_half_written_snapshot_is_never_offered(self, project: Path) -> None:
        """A crash mid-copy must not leave something `undo` will restore."""
        _record("x")
        snap = memory_undo.take(project)
        partial = snap.path.parent / ".20990101T000000Z.partial"
        partial.mkdir()
        (partial / "decisions.jsonl").write_text("truncated")
        assert all(
            not s.name.startswith(".") for s in memory_undo.list_snapshots(project)
        )

    def test_list_on_a_project_with_none_is_empty_not_an_error(
        self, project: Path
    ) -> None:
        assert memory_undo.list_snapshots(project) == []


class TestTheCliIsReachable:
    def test_the_command_is_registered(self) -> None:
        """The prerequisite is `codevira memory undo` existing, not just a
        function that could back one."""
        out = subprocess.run(
            ["python3", "-m", "mcp_server.cli", "memory", "--help"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert out.returncode == 0, out.stderr
        assert "undo" in out.stdout
        assert "snapshot" in out.stdout

    def test_undo_advertises_all_projects(self) -> None:
        out = subprocess.run(
            ["python3", "-m", "mcp_server.cli", "memory", "undo", "--help"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert out.returncode == 0, out.stderr
        assert "--all-projects" in out.stdout
