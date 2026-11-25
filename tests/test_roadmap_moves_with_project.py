"""
The roadmap moves with the project.

Defect (reproduced on 4.2.0): decisions, sessions, outcomes and skills live in
``<project>/.codevira/`` and move with the folder, but the roadmap was read and
written at ``get_data_dir()/roadmap.yaml`` — for a project with a centralized
dir that is ``~/.codevira/projects/<key derived from the absolute path>/``.
Rename the folder and the key changes: ``get_roadmap`` silently served the
placeholder written in-project at init, and ``codevira prune --orphans`` then
deleted the only real copy.

Locked decision D00011Z: memory ALWAYS lives in ``<project>/.codevira/``; the
centralized dir is a mirror of derived artifacts; migrations are NON-DESTRUCTIVE.

These tests pin:
  * the roadmap is read/written at ``storage.paths.roadmap_path()``;
  * the one-time, non-destructive adoption of an existing centralized roadmap
    (every rule: winner, backup, numbered suffix, corrupt file, crash between
    steps, an old process recreating the file, worktrees, foreign key dirs,
    concurrent starts);
  * export (json ``phases`` table, setup bundle) and prune interplay.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

from mcp_server import migrate, opt_in, paths
from mcp_server.paths import _sanitize_path_key

REPO_ROOT = Path(__file__).resolve().parent.parent

REAL = {
    "project": "proj",
    "version": "1.0",
    "current_phase": {
        "number": 7,
        "name": "Real phase",
        "status": "in_progress",
        "next_action": "REAL-NEXT-ACTION",
        "description": "the roadmap the user has been using",
    },
    "upcoming_phases": [
        {"phase": 8, "name": "Upcoming eight", "priority": "high"},
    ],
    "deferred": [{"phase": 20, "name": "Deferred twenty"}],
    "completed_phases": [
        {"number": 1, "name": "Done one", "key_decisions": ["k1"]},
        {"number": 2, "name": "Done two", "key_decisions": ["k2"]},
    ],
}

PLACEHOLDER = {
    "project": "proj",
    "version": "1.0",
    "current_phase": {
        "number": 1,
        "name": "Getting Started",
        "status": "pending",
        "next_action": "PLACEHOLDER-NEXT-ACTION",
        "description": "Auto-generated stub — update this to reflect your project.",
    },
    "upcoming_phases": [],
    "deferred": [],
    "completed_phases": [],
}


# ─── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Each test is a 'fresh process' as far as warn-once state goes."""
    warned = getattr(migrate, "_ROADMAP_WARNED", None)
    if warned is not None:
        warned.clear()
    yield
    if warned is not None:
        warned.clear()


def _dump(data: dict) -> str:
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def central_dir_for(project: Path) -> Path:
    return paths.get_global_home() / "projects" / _sanitize_path_key(project.resolve())


def make_project(
    tmp_path: Path,
    name: str = "proj",
    *,
    central: dict | str | None = REAL,
    inproject: dict | str | None = PLACEHOLDER,
    central_config: bool = True,
) -> Path:
    """A project shaped like a 4.2.0 project with a centralized dir."""
    project = (tmp_path / name).resolve()
    project.mkdir()
    (project / ".git").mkdir()
    cv = project / ".codevira"
    cv.mkdir()
    (cv / "config.yaml").write_text("project:\n  name: proj\n")
    if inproject is not None:
        (cv / "roadmap.yaml").write_text(
            inproject if isinstance(inproject, str) else _dump(inproject)
        )
    cdir = central_dir_for(project)
    cdir.mkdir(parents=True)
    if central_config:
        (cdir / "config.yaml").write_text("project:\n  name: proj\n")
    (cdir / "metadata.json").write_text(
        json.dumps({"path_key": cdir.name, "original_path": str(project)})
    )
    if central is not None:
        (cdir / "roadmap.yaml").write_text(
            central if isinstance(central, str) else _dump(central)
        )
    return project


def enter(project: Path, monkeypatch) -> None:
    """Make ``project`` the current project, with every cache cold."""
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project)
    paths.reset_pinned_root()
    paths._data_dir_cache.clear()
    opt_in.invalidate_opt_in_cache()


def _env() -> dict:
    env = dict(os.environ)
    env["CODEVIRA_HOME"] = str(paths.get_global_home())
    env["CODEVIRA_NO_UPDATE_CHECK"] = "1"
    env.pop("CODEVIRA_PROJECT_DIR", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


_GET = (
    "import json\n"
    "from mcp_server.tools import roadmap\n"
    "print('RESULT:' + json.dumps(roadmap.get_roadmap()))\n"
)


def run_py(project: Path, code: str, timeout: int = 90) -> dict:
    """Run ``code`` in a brand-new interpreter inside ``project``; parse RESULT."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(project),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:") :])
    raise AssertionError(f"no RESULT in output: {proc.stdout!r}\n{proc.stderr[-1000:]}")


def next_action(result: dict) -> str:
    return result["current_phase"]["next_action"]


def inproject_roadmap(project: Path) -> Path:
    return project / ".codevira" / "roadmap.yaml"


def central_roadmap(project: Path) -> Path:
    return central_dir_for(project) / "roadmap.yaml"


def moved_marker(project: Path) -> Path:
    return central_dir_for(project) / "roadmap.yaml.moved-into-project"


def adopt(project: Path) -> str:
    return migrate.adopt_centralized_roadmap(project)


# ─── the headline defect ──────────────────────────────────────────────────


class TestRenameKeepsRoadmap:
    def test_rename_then_restart_still_serves_the_real_roadmap(self, tmp_path):
        """THE defect. A true restart (new interpreter) before and after a
        folder rename must serve the real next_action and completed phases."""
        project = make_project(tmp_path, "before")
        first = run_py(project, _GET)
        assert next_action(first) == "REAL-NEXT-ACTION"

        renamed = tmp_path / "after-rename"
        project.rename(renamed)

        second = run_py(renamed, _GET)
        assert next_action(second) == "REAL-NEXT-ACTION"
        assert second["completed_phases_count"] == 2
        assert second["current_phase"]["name"] == "Real phase"

    def test_rename_in_process_through_tool_functions(self, tmp_path, monkeypatch):
        from mcp_server.tools import roadmap

        project = make_project(tmp_path, "before")
        enter(project, monkeypatch)
        assert roadmap.get_roadmap()["current_phase"]["next_action"] == (
            "REAL-NEXT-ACTION"
        )
        renamed = tmp_path / "renamed"
        project.rename(renamed)
        enter(renamed, monkeypatch)
        got = roadmap.get_roadmap()
        assert got["current_phase"]["next_action"] == "REAL-NEXT-ACTION"
        assert got["completed_phases_count"] == 2

    def test_roadmap_file_is_the_in_project_path(self, tmp_path, monkeypatch):
        from mcp_server.storage import paths as storage_paths
        from mcp_server.tools import roadmap

        project = make_project(tmp_path)
        enter(project, monkeypatch)
        assert roadmap._roadmap_file() == storage_paths.roadmap_path()
        assert roadmap._roadmap_file() == project / ".codevira" / "roadmap.yaml"
        assert roadmap._roadmap_lock_path() == (
            project / ".codevira" / "roadmap.yaml.lock"
        )

    def test_mutator_as_first_call_does_not_deadlock(self, tmp_path):
        """update_next_action is the FIRST roadmap call in a fresh process:
        adoption must happen under the lock without re-taking it."""
        project = make_project(tmp_path)
        code = (
            "import json\n"
            "from mcp_server.tools import roadmap\n"
            "roadmap.update_next_action('MUTATED')\n"
            "print('RESULT:' + json.dumps(roadmap.get_roadmap()))\n"
        )
        got = run_py(project, code, timeout=40)
        assert next_action(got) == "MUTATED"
        assert got["completed_phases_count"] == 2  # the REAL roadmap was adopted
        assert moved_marker(project).is_file()

    def test_updates_after_adoption_persist_and_survive_a_rename(self, tmp_path):
        project = make_project(tmp_path, "before")
        run_py(project, _GET)  # adoption
        code = (
            "import json\n"
            "from mcp_server.tools import roadmap\n"
            "roadmap.update_next_action('AFTER-ADOPTION')\n"
            "print('RESULT:' + json.dumps(roadmap.get_roadmap()))\n"
        )
        run_py(project, code)
        on_disk = yaml.safe_load(inproject_roadmap(project).read_text())
        assert on_disk["current_phase"]["next_action"] == "AFTER-ADOPTION"
        # Nothing may re-create the centralized copy.
        assert not central_roadmap(project).exists()

        renamed = tmp_path / "later"
        project.rename(renamed)
        assert next_action(run_py(renamed, _GET)) == "AFTER-ADOPTION"


# ─── one-time adoption rules ──────────────────────────────────────────────


class TestAdoption:
    def test_central_wins_and_differing_placeholder_is_kept_aside(self, tmp_path):
        project = make_project(tmp_path)
        placeholder_bytes = inproject_roadmap(project).read_bytes()
        central_bytes = central_roadmap(project).read_bytes()

        assert adopt(project) == "adopted"

        assert inproject_roadmap(project).read_bytes() == central_bytes
        backup = project / ".codevira" / "roadmap.yaml.before-adoption"
        assert backup.read_bytes() == placeholder_bytes
        # The centralized file is renamed, never deleted.
        assert not central_roadmap(project).exists()
        assert moved_marker(project).read_bytes() == central_bytes

    def test_backup_never_overwrites_an_existing_one(self, tmp_path):
        project = make_project(tmp_path)
        existing = project / ".codevira" / "roadmap.yaml.before-adoption"
        existing.write_text("EARLIER BACKUP\n")
        placeholder_bytes = inproject_roadmap(project).read_bytes()

        assert adopt(project) == "adopted"

        assert existing.read_text() == "EARLIER BACKUP\n"  # untouched
        numbered = sorted(
            p.name
            for p in (project / ".codevira").iterdir()
            if p.name.startswith("roadmap.yaml.before-adoption")
        )
        assert len(numbered) == 2
        other = [n for n in numbered if n != "roadmap.yaml.before-adoption"][0]
        assert other == "roadmap.yaml.before-adoption.1"
        assert (project / ".codevira" / other).read_bytes() == placeholder_bytes

    def test_second_numbered_backup_increments(self, tmp_path):
        project = make_project(tmp_path)
        cv = project / ".codevira"
        (cv / "roadmap.yaml.before-adoption").write_text("A\n")
        (cv / "roadmap.yaml.before-adoption.1").write_text("B\n")
        adopt(project)
        assert (cv / "roadmap.yaml.before-adoption").read_text() == "A\n"
        assert (cv / "roadmap.yaml.before-adoption.1").read_text() == "B\n"
        assert (cv / "roadmap.yaml.before-adoption.2").is_file()

    def test_identical_in_project_file_needs_no_backup(self, tmp_path):
        project = make_project(tmp_path)
        inproject_roadmap(project).write_bytes(central_roadmap(project).read_bytes())
        assert adopt(project) == "adopted"
        assert not (project / ".codevira" / "roadmap.yaml.before-adoption").exists()
        assert moved_marker(project).is_file()

    def test_missing_in_project_file_is_created_without_backup(self, tmp_path):
        project = make_project(tmp_path, inproject=None)
        central_bytes = central_roadmap(project).read_bytes()
        assert adopt(project) == "adopted"
        assert inproject_roadmap(project).read_bytes() == central_bytes
        assert not (project / ".codevira" / "roadmap.yaml.before-adoption").exists()

    def test_no_central_roadmap_is_a_noop(self, tmp_path):
        project = make_project(tmp_path, central=None)
        before = inproject_roadmap(project).read_bytes()
        assert adopt(project) == "none"
        assert inproject_roadmap(project).read_bytes() == before
        assert not moved_marker(project).exists()

    def test_in_repo_only_project_is_not_adopted(self, tmp_path):
        """data dir is the in-repo store (no centralized config.yaml): a stray
        centralized roadmap is not what the user has been reading."""
        project = make_project(tmp_path, central_config=False)
        before = inproject_roadmap(project).read_bytes()
        assert adopt(project) == "none"
        assert inproject_roadmap(project).read_bytes() == before
        assert central_roadmap(project).is_file()

    def test_adoption_is_idempotent(self, tmp_path):
        project = make_project(tmp_path)
        assert adopt(project) == "adopted"
        content = inproject_roadmap(project).read_bytes()
        assert adopt(project) == "none"
        assert inproject_roadmap(project).read_bytes() == content


class TestCorruptCentralFile:
    @pytest.mark.parametrize(
        "bad", ["key: [unclosed\n  - : :\n", "- just\n- a list\n", "42\n", ""]
    )
    def test_unreadable_central_leaves_both_alone(self, tmp_path, caplog, bad):
        project = make_project(tmp_path, central=bad)
        before_in = inproject_roadmap(project).read_bytes()
        with caplog.at_level("WARNING"):
            assert adopt(project) == "unreadable"
        assert inproject_roadmap(project).read_bytes() == before_in
        assert central_roadmap(project).read_text() == bad
        assert not moved_marker(project).exists()
        assert not (project / ".codevira" / "roadmap.yaml.before-adoption").exists()
        assert any("roadmap" in r.getMessage().lower() for r in caplog.records)

    def test_in_project_roadmap_keeps_being_served(self, tmp_path, monkeypatch):
        from mcp_server.tools import roadmap

        project = make_project(tmp_path, central="key: [unclosed\n")
        enter(project, monkeypatch)
        assert next_action(roadmap.get_roadmap()) == "PLACEHOLDER-NEXT-ACTION"
        assert central_roadmap(project).read_text() == "key: [unclosed\n"


class TestCrashBetweenSteps:
    def _fail_central_rename(self, monkeypatch, project: Path, exc: BaseException):
        real_replace = os.replace
        marker = str(moved_marker(project))

        def fake(src, dst, *a, **k):
            if str(dst) == marker:
                raise exc
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", fake)
        monkeypatch.setattr(os, "rename", fake)  # either spelling must be covered
        return real_replace

    def test_hard_crash_loses_nothing_and_a_rerun_converges(
        self, tmp_path, monkeypatch
    ):
        class Boom(BaseException):
            pass

        project = make_project(tmp_path)
        central_bytes = central_roadmap(project).read_bytes()
        with monkeypatch.context() as m:
            self._fail_central_rename(m, project, Boom("power cut"))
            with pytest.raises(Boom):
                adopt(project)

        # The in-project file was written FIRST: the real data is there, and the
        # centralized original is still where it was.
        assert inproject_roadmap(project).read_bytes() == central_bytes
        assert central_roadmap(project).read_bytes() == central_bytes
        assert not moved_marker(project).exists()
        backups = sorted(
            p.name for p in (project / ".codevira").glob("*before-adoption*")
        )
        assert backups == ["roadmap.yaml.before-adoption"]

        # Re-run converges: marker present, no second backup of our own write.
        assert adopt(project) == "adopted"
        assert moved_marker(project).read_bytes() == central_bytes
        assert not central_roadmap(project).exists()
        assert inproject_roadmap(project).read_bytes() == central_bytes
        backups = sorted(
            p.name for p in (project / ".codevira").glob("*before-adoption*")
        )
        assert backups == ["roadmap.yaml.before-adoption"]
        assert adopt(project) == "none"

    def test_failed_rename_is_reported_not_raised_and_keeps_data(
        self, tmp_path, monkeypatch, caplog
    ):
        project = make_project(tmp_path)
        central_bytes = central_roadmap(project).read_bytes()
        with monkeypatch.context() as m:
            self._fail_central_rename(m, project, OSError("read-only filesystem"))
            with caplog.at_level("WARNING"):
                outcome = adopt(project)
        assert outcome != "adopted"
        assert inproject_roadmap(project).read_bytes() == central_bytes
        assert central_roadmap(project).read_bytes() == central_bytes
        assert any("roadmap" in r.getMessage().lower() for r in caplog.records)
        assert adopt(project) == "adopted"  # converges once the fault clears

    def test_in_project_file_is_written_before_the_central_one_is_touched(
        self, tmp_path, monkeypatch
    ):
        project = make_project(tmp_path)
        central_bytes = central_roadmap(project).read_bytes()
        seen: dict = {}
        real_replace = os.replace
        marker = str(moved_marker(project))

        def spy(src, dst, *a, **k):
            if str(dst) == marker:
                seen["inproject_at_rename"] = inproject_roadmap(project).read_bytes()
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", spy)
        monkeypatch.setattr(os, "rename", spy)
        adopt(project)
        assert seen["inproject_at_rename"] == central_bytes


class TestOldProcessRecreatesTheFile:
    def test_recreated_central_never_overwrites_and_warns_once(self, tmp_path, caplog):
        project = make_project(tmp_path)
        assert adopt(project) == "adopted"
        adopted = inproject_roadmap(project).read_bytes()

        # A 4.2.0 process still running writes its roadmap centrally again.
        stale = _dump(
            {
                **REAL,
                "current_phase": {
                    **REAL["current_phase"],
                    "next_action": "STALE-FROM-OLD-PROCESS",
                },
            }
        )
        central_roadmap(project).write_text(stale)

        with caplog.at_level("WARNING"):
            assert adopt(project) == "stale"
            assert adopt(project) == "stale"
            assert adopt(project) == "stale"
        assert inproject_roadmap(project).read_bytes() == adopted
        assert central_roadmap(project).read_text() == stale  # left alone
        msgs = [
            r.getMessage()
            for r in caplog.records
            if str(central_roadmap(project)) in r.getMessage()
        ]
        assert len(msgs) == 1  # once per process
        assert str(inproject_roadmap(project)) in msgs[0]

    def test_tool_access_ignores_the_recreated_file(self, tmp_path, monkeypatch):
        from mcp_server.tools import roadmap

        project = make_project(tmp_path)
        enter(project, monkeypatch)
        roadmap.get_roadmap()  # adopt
        stale = _dump(PLACEHOLDER)
        central_roadmap(project).write_text(stale)
        assert next_action(roadmap.get_roadmap()) == "REAL-NEXT-ACTION"
        roadmap.update_next_action("KEEP-ME")
        assert next_action(roadmap.get_roadmap()) == "KEEP-ME"
        assert central_roadmap(project).read_text() == stale

    def test_marker_present_means_no_second_backup_either(self, tmp_path):
        project = make_project(tmp_path)
        adopt(project)
        central_roadmap(project).write_text(_dump(PLACEHOLDER))
        adopt(project)
        names = sorted(
            p.name for p in (project / ".codevira").glob("*before-adoption*")
        )
        assert names == ["roadmap.yaml.before-adoption"]


class TestWorktreeAndForeignDirs:
    def _worktree(self, tmp_path: Path, main: Path) -> Path:
        wt = (tmp_path / "wt").resolve()
        wt.mkdir()
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        return wt

    def test_worktree_central_roadmap_never_overwrites_main(self, tmp_path):
        main = make_project(tmp_path, "main", central=None, inproject=REAL)
        wt = self._worktree(tmp_path, main)
        # The worktree has its OWN centralized dir with a different roadmap.
        wdir = central_dir_for(wt)
        wdir.mkdir(parents=True)
        (wdir / "config.yaml").write_text("x: 1\n")
        wt_roadmap = _dump(PLACEHOLDER)
        (wdir / "roadmap.yaml").write_text(wt_roadmap)
        main_before = inproject_roadmap(main).read_bytes()

        assert adopt(wt) == "none"

        assert inproject_roadmap(main).read_bytes() == main_before
        assert (wdir / "roadmap.yaml").read_text() == wt_roadmap
        assert not (wdir / "roadmap.yaml.moved-into-project").exists()
        assert not (main / ".codevira" / "roadmap.yaml.before-adoption").exists()

    def test_main_checkouts_own_central_dir_is_adopted_from_a_worktree(self, tmp_path):
        main = make_project(tmp_path, "main")
        wt = self._worktree(tmp_path, main)
        central_bytes = central_roadmap(main).read_bytes()
        assert adopt(wt) == "adopted"
        assert inproject_roadmap(main).read_bytes() == central_bytes
        assert moved_marker(main).is_file()

    def test_worktree_tool_calls_see_the_main_roadmap(self, tmp_path, monkeypatch):
        from mcp_server.tools import roadmap

        main = make_project(tmp_path, "main", central=None, inproject=REAL)
        wt = self._worktree(tmp_path, main)
        wdir = central_dir_for(wt)
        wdir.mkdir(parents=True)
        (wdir / "config.yaml").write_text("x: 1\n")
        (wdir / "roadmap.yaml").write_text(_dump(PLACEHOLDER))
        enter(wt, monkeypatch)
        assert next_action(roadmap.get_roadmap()) == "REAL-NEXT-ACTION"
        assert (wdir / "roadmap.yaml").is_file()

    def test_foreign_key_dir_is_never_adopted(self, tmp_path, monkeypatch):
        """get_data_dir's git-remote lookup can return ANOTHER path's dir.
        Adoption only trusts the dir whose key is this project's own."""
        project = (tmp_path / "proj").resolve()
        project.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@example.com:acme/shared.git"],
            cwd=project,
            check=True,
        )
        cv = project / ".codevira"
        cv.mkdir()
        placeholder = _dump(PLACEHOLDER)
        (cv / "roadmap.yaml").write_text(placeholder)
        foreign = paths.get_global_home() / "projects" / "elsewhere_deadbeef"
        foreign.mkdir(parents=True)
        (foreign / "config.yaml").write_text("x: 1\n")
        (foreign / "metadata.json").write_text(
            json.dumps(
                {
                    "path_key": "elsewhere_deadbeef",
                    "original_path": str(tmp_path / "other-checkout"),
                    "git_remote": "git@example.com:acme/shared.git",
                }
            )
        )
        foreign_roadmap = _dump(REAL)
        (foreign / "roadmap.yaml").write_text(foreign_roadmap)

        # Sanity: the lookup really would return the foreign dir.
        enter(project, monkeypatch)
        assert paths.get_data_dir() == foreign

        assert adopt(project) == "none"
        assert (cv / "roadmap.yaml").read_text() == placeholder
        assert (foreign / "roadmap.yaml").read_text() == foreign_roadmap
        assert not (foreign / "roadmap.yaml.moved-into-project").exists()


class TestStartupMigration:
    def test_startup_migration_adopts_without_any_roadmap_access(
        self, tmp_path, monkeypatch
    ):
        project = make_project(tmp_path)
        enter(project, monkeypatch)
        migrate.run_startup_migrations(project)
        assert moved_marker(project).is_file()
        assert not central_roadmap(project).exists()
        assert (
            yaml.safe_load(inproject_roadmap(project).read_text())["current_phase"][
                "next_action"
            ]
            == "REAL-NEXT-ACTION"
        )

    def test_startup_migration_is_registered_and_always_rerun(self):
        names = [n for n, _ in migrate._STARTUP_MIGRATIONS]
        adoption = [n for n in names if "roadmap" in n]
        assert len(adoption) == 1
        # An old process can recreate the file later; never seal it in the ledger.
        assert adoption[0] in migrate._ALWAYS_RERUN

    def test_startup_migration_honours_the_opt_in_gate(self, tmp_path, monkeypatch):
        """A project the user never init-ed must not be touched at startup."""
        project = (tmp_path / "ghost").resolve()
        project.mkdir()
        (project / ".git").mkdir()
        cdir = central_dir_for(project)
        cdir.mkdir(parents=True)
        (cdir / "roadmap.yaml").write_text(_dump(REAL))  # no config.yaml
        enter(project, monkeypatch)
        migrate.run_startup_migrations(project)
        assert (cdir / "roadmap.yaml").is_file()
        assert not (project / ".codevira").exists()


class TestConcurrentStarts:
    def test_eight_processes_starting_at_once(self, tmp_path):
        project = make_project(tmp_path)
        placeholder_bytes = inproject_roadmap(project).read_bytes()
        central_bytes = central_roadmap(project).read_bytes()
        go = tmp_path / "GO"
        code = (
            "import json, os, sys, time\n"
            "from mcp_server.tools import roadmap\n"
            "while not os.path.exists(sys.argv[1]):\n"
            "    time.sleep(0.002)\n"
            "print('RESULT:' + json.dumps(roadmap.get_roadmap()))\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(go)],
                cwd=str(project),
                env=_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        time.sleep(4)  # let every interpreter finish importing and reach the barrier
        go.write_text("go")
        outs = [p.communicate(timeout=90) for p in procs]
        for p, (out, err) in zip(procs, outs):
            assert p.returncode == 0, err[-1500:]
            line = [x for x in out.splitlines() if x.startswith("RESULT:")][0]
            assert next_action(json.loads(line[7:])) == "REAL-NEXT-ACTION"

        assert moved_marker(project).read_bytes() == central_bytes
        assert not central_roadmap(project).exists()
        cv = project / ".codevira"
        assert sorted(p.name for p in cv.glob("*before-adoption*")) == [
            "roadmap.yaml.before-adoption"
        ]
        assert (cv / "roadmap.yaml.before-adoption").read_bytes() == placeholder_bytes
        on_disk = yaml.safe_load(inproject_roadmap(project).read_text())
        assert on_disk["current_phase"]["name"] == "Real phase"
        assert len(on_disk["completed_phases"]) == 2


# ─── other readers / writers ──────────────────────────────────────────────


class TestOtherSurfaces:
    def test_invalid_root_still_raises_loudly(self, monkeypatch):
        from mcp_server.tools import roadmap

        home = Path.home().resolve()
        monkeypatch.setattr(paths, "get_project_root", lambda: home)
        with pytest.raises(ValueError):
            roadmap._roadmap_file()
        with pytest.raises(ValueError):
            roadmap.get_roadmap()
        with pytest.raises(ValueError):
            roadmap.update_next_action("x")

    def test_bootstrap_roadmap_writes_the_in_project_file(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        project = make_project(tmp_path, central=None, inproject=None)
        enter(project, monkeypatch)
        from indexer.index_codebase import cmd_bootstrap_roadmap

        with patch("indexer.graph_generator.generate_roadmap_stub") as stub:
            cmd_bootstrap_roadmap()
        assert stub.call_args[0][1] == str(project / ".codevira" / "roadmap.yaml")

    def test_bootstrap_roadmap_sees_an_existing_in_project_file(
        self, tmp_path, monkeypatch, capsys
    ):
        from unittest.mock import patch

        project = make_project(tmp_path, central=None)
        enter(project, monkeypatch)
        from indexer.index_codebase import cmd_bootstrap_roadmap

        with patch("indexer.graph_generator.generate_roadmap_stub") as stub:
            cmd_bootstrap_roadmap()
        stub.assert_not_called()
        assert "already exists" in capsys.readouterr().out

    def test_drift_default_is_the_in_project_roadmap(self, tmp_path):
        from mcp_server.roadmap_drift import _resolve_reference_time

        project = make_project(tmp_path, central=None)
        got = _resolve_reference_time(
            current_phase=None, roadmap_path=None, project_root=project
        )
        mtime = inproject_roadmap(project).stat().st_mtime
        assert got is not None and abs(got.timestamp() - mtime) < 1

    def test_drift_default_follows_a_worktree_to_the_main_roadmap(self, tmp_path):
        """Same file the roadmap tools read: in a linked worktree that is the
        MAIN worktree's .codevira/roadmap.yaml, not <worktree>/.codevira/."""
        from mcp_server.roadmap_drift import _resolve_reference_time

        main = make_project(tmp_path, "main", central=None)
        wt = (tmp_path / "wt").resolve()
        wt.mkdir()
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        os.utime(inproject_roadmap(main), (1_700_000_000, 1_700_000_000))
        got = _resolve_reference_time(
            current_phase=None, roadmap_path=None, project_root=wt
        )
        assert got is not None and abs(got.timestamp() - 1_700_000_000) < 1

    def test_inventory_counts_a_moved_roadmap_as_real_state(self, tmp_path):
        """A slug dir holding only the adopted copy is a backup, not litter:
        it must classify as ghost (needs an explicit decision), not stale."""
        from mcp_server._project_inventory import _inspect_disk

        slug = tmp_path / "slug_dir"
        slug.mkdir()
        (slug / "roadmap.yaml.moved-into-project").write_text("a: 1\n")
        entry = _inspect_disk(slug, {}, {})
        assert entry.has_roadmap is True
        assert entry.status == "ghost"
        bare = tmp_path / "bare"
        bare.mkdir()
        assert _inspect_disk(bare, {}, {}).has_roadmap is False

    def test_migrate_to_centralized_does_not_strand_the_roadmap(
        self, tmp_path, monkeypatch
    ):
        project = (tmp_path / "legacy").resolve()
        cv = project / ".codevira"
        cv.mkdir(parents=True)
        (cv / "config.yaml").write_text("project:\n  name: legacy\n")
        (cv / "roadmap.yaml").write_text(_dump(REAL))
        enter(project, monkeypatch)
        assert migrate.migrate_to_centralized(project)["migrated"] is True
        assert not central_roadmap(project).exists()
        assert yaml.safe_load(inproject_roadmap(project).read_text()) == REAL


class TestPruneOrphansAfterRename:
    def test_prune_orphans_leaves_the_real_roadmap_intact(self, tmp_path, monkeypatch):
        from mcp_server import cli

        project = make_project(tmp_path, "before")
        run_py(project, _GET)  # server start / first access: adoption
        renamed = tmp_path / "after"
        project.rename(renamed)
        old_central = central_dir_for(project)
        assert old_central.is_dir()  # the orphan prune will remove

        enter(renamed, monkeypatch)
        cli._cmd_clean_orphans(dry_run=False, yes=True)

        assert not old_central.exists()  # prune really ran on the orphan
        got = run_py(renamed, _GET)
        assert next_action(got) == "REAL-NEXT-ACTION"
        assert got["completed_phases_count"] == 2


# ─── export ───────────────────────────────────────────────────────────────


class TestExport:
    def _export(self, project: Path, monkeypatch) -> dict:
        from mcp_server.cli_export import export_decisions_to_path

        enter(project, monkeypatch)
        out = project / "backup.json"
        summary = export_decisions_to_path(out, format="json", target="decisions")
        return {"summary": summary, "payload": json.loads(out.read_text())}

    def test_json_phases_come_from_the_roadmap(self, tmp_path, monkeypatch):
        project = make_project(tmp_path, central=None, inproject=REAL)
        got = self._export(project, monkeypatch)
        assert got["summary"]["table_sources"]["phases"] == "roadmap"
        assert got["payload"]["table_sources"]["phases"] == "roadmap"
        rows = got["payload"]["tables"]["phases"]
        assert got["summary"]["tables"]["phases"] == len(rows) == 5
        by_state: dict = {}
        for r in rows:
            by_state.setdefault(r["state"], []).append(r)
        assert sorted(by_state) == ["completed", "current", "deferred", "upcoming"]
        assert [r["name"] for r in by_state["current"]] == ["Real phase"]
        assert [r["name"] for r in by_state["upcoming"]] == ["Upcoming eight"]
        assert [r["name"] for r in by_state["deferred"]] == ["Deferred twenty"]
        assert sorted(r["name"] for r in by_state["completed"]) == [
            "Done one",
            "Done two",
        ]
        assert by_state["current"][0]["number"] == 7

    def test_json_export_adopts_a_pending_centralized_roadmap(
        self, tmp_path, monkeypatch
    ):
        """`codevira export` can run before any server start."""
        project = make_project(tmp_path)
        got = self._export(project, monkeypatch)
        names = [r["name"] for r in got["payload"]["tables"]["phases"]]
        assert "Real phase" in names
        assert "Getting Started" not in names

    def test_json_export_does_not_write_the_roadmap(self, tmp_path, monkeypatch):
        project = make_project(tmp_path, central=None, inproject=REAL)
        before = inproject_roadmap(project).read_bytes()
        self._export(project, monkeypatch)
        assert inproject_roadmap(project).read_bytes() == before

    def test_unreadable_roadmap_falls_back_to_legacy_sqlite(
        self, tmp_path, monkeypatch
    ):
        import sqlite3

        project = make_project(tmp_path, central=None, inproject="key: [unclosed\n")
        legacy_db = project / "legacy-graph.db"
        conn = sqlite3.connect(legacy_db)
        conn.execute("CREATE TABLE phases (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO phases (name) VALUES ('legacy phase')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            "mcp_server.cli_export._resolve_graph_db_path", lambda: legacy_db
        )
        got = self._export(project, monkeypatch)
        assert got["summary"]["table_sources"]["phases"] == "sqlite-legacy"

    def test_no_roadmap_file_reports_missing(self, tmp_path, monkeypatch):
        project = make_project(tmp_path, central=None, inproject=None)
        got = self._export(project, monkeypatch)
        assert got["summary"]["table_sources"]["phases"] == "missing"
        assert got["summary"]["tables"]["phases"] == 0

    def test_export_setup_bundle_contains_the_real_roadmap(self, tmp_path, monkeypatch):
        from mcp_server.cli_transfer import cmd_export_setup

        project = make_project(tmp_path)  # real roadmap still centralized
        enter(project, monkeypatch)
        out = tmp_path / "setup.tar.gz"
        assert cmd_export_setup(out=str(out)) == 0
        with tarfile.open(out) as tar:
            member = tar.extractfile(".codevira/roadmap.yaml")
            assert member is not None
            body = yaml.safe_load(member.read())
        assert body["current_phase"]["next_action"] == "REAL-NEXT-ACTION"
        assert len(body["completed_phases"]) == 2

    def test_export_setup_dry_run_changes_nothing(self, tmp_path, monkeypatch):
        from mcp_server.cli_transfer import cmd_export_setup

        project = make_project(tmp_path)
        enter(project, monkeypatch)
        assert cmd_export_setup(out=str(tmp_path / "x.tar.gz"), dry_run=True) == 0
        assert central_roadmap(project).is_file()
        assert not moved_marker(project).exists()
