"""C13 — doctor must be able to see what is registered, not only what is bound.

WHY THIS EXISTS
---------------
On 2026-09-10 a single bad row made `register-all` discover ONE project and
discard eleven real ones as "nested sub-stores". `codevira doctor` reported
everything healthy throughout, because nothing it ran looked at the registry:
C3 checks the CURRENT binding and C5 checks that global.db opens. Neither
reads the rows inside it.

That gap is why 4.2.0's upgrade guidance had to tell users to run
`codevira register-all --dry-run` and interpret the output themselves — three
commands, in order, one of which (`untrack`) deletes a data directory if you
skip its dry-run. This check plus its auto-fixer replaces that.

THE SAFETY PROPERTY THIS FILE DEFENDS
-------------------------------------
The fixer removes REGISTRY ROWS ONLY and never a data directory.

A user wrongly bound to `$HOME` recorded their decisions into `$HOME`'s data
directory. `codevira untrack` removes IDE entries *and the data dir*, so
"fixing" the registration by untracking it would delete the memory the fix
exists to recover. Rows are bookkeeping and `register-all` rebuilds them;
decisions are not rebuildable. Several tests below exist only to hold that
line.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_server.doctor import (
    _AUTOFIXERS,
    _autofix_registry_rows,
    _bad_registry_rows,
    check_registry_rows,
)


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )


def _make_global_db(home: Path, rows: list[tuple[str, str]]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(home / "global.db"))
    conn.execute(
        "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "language TEXT, git_remote TEXT, "
        "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.executemany("INSERT INTO projects (path, name) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _rows(home: Path) -> set[str]:
    conn = sqlite3.connect(str(home / "global.db"))
    try:
        return {r[0] for r in conn.execute("SELECT path FROM projects")}
    finally:
        conn.close()


def _real_project(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    (p / ".codevira").mkdir(parents=True)
    (p / ".git").mkdir()
    return p


class TestTheCheckSeesBadRows:
    def test_a_data_directory_registered_as_a_project_is_flagged(
        self, tmp_path, monkeypatch
    ):
        """The row found on the maintainer's real machine.

        `is_invalid_project_root` ALLOWS paths under the codevira home, so
        nothing else catches this. It is deliberately not being tightened —
        blast radius 186 files — so the rule lives in this check.
        """
        home = tmp_path / "cvhome"
        datadir = home / "projects" / "Users_x_proj_abc123"
        datadir.mkdir(parents=True)
        _make_global_db(home, [(str(datadir), "proj")])
        _patch_home(monkeypatch, home)

        bad = _bad_registry_rows()
        assert [p for p, _ in bad] == [str(datadir)], bad
        assert "data directory" in bad[0][1]
        assert check_registry_rows().state == "WARN"

    def test_a_refused_root_is_flagged(self, tmp_path, monkeypatch):
        """$HOME, or any root get_data_dir would refuse. The path EXISTS, so
        check_ghost_projects can never see it."""
        home = tmp_path / "cvhome"
        _make_global_db(home, [("/", "slash")])
        _patch_home(monkeypatch, home)

        bad = _bad_registry_rows()
        assert [p for p, _ in bad] == ["/"], bad
        assert check_registry_rows().state == "WARN"

    def test_a_real_project_is_not_flagged(self, tmp_path, monkeypatch):
        home = tmp_path / "cvhome"
        proj = _real_project(tmp_path, "goodproj")
        _make_global_db(home, [(str(proj), "goodproj")])
        _patch_home(monkeypatch, home)

        assert _bad_registry_rows() == []
        r = check_registry_rows()
        assert r.state == "PASS", r.message

    def test_a_vanished_directory_is_left_to_the_ghost_check(
        self, tmp_path, monkeypatch
    ):
        """Two checks claiming one row means the user fixes it twice, and the
        second fix reports nothing to do. check_ghost_projects owns this.

        is_invalid_project_root is STUBBED to refuse everything here, and that
        is the whole point. Today it allows every non-existent path, so the
        `is_dir()` guard never fires and this test passed with the guard
        deleted — it proved nothing. Stubbing makes the guard load-bearing and
        pins the boundary against the day is_invalid_project_root is tightened
        to refuse vanished paths, which is a change deliberately NOT made in
        4.3.0 because paths.py has a blast radius of 186 files.
        """
        home = tmp_path / "cvhome"
        gone = tmp_path / "deleted-long-ago"
        _make_global_db(home, [(str(gone), "gone")])
        _patch_home(monkeypatch, home)
        monkeypatch.setattr(
            "mcp_server.paths.is_invalid_project_root",
            lambda p: "refused (stubbed: would reject anything)",
        )

        assert _bad_registry_rows() == [], (
            "a row whose directory is gone was reported here as well as by "
            "check_ghost_projects — the user would be told to fix it twice"
        )

    def test_a_row_that_exists_and_is_refused_is_still_caught(
        self, tmp_path, monkeypatch
    ):
        """The other side of that boundary: the is_dir() guard must not
        swallow a refused root that is actually present on disk."""
        home = tmp_path / "cvhome"
        present = tmp_path / "present"
        present.mkdir()
        _make_global_db(home, [(str(present), "here")])
        _patch_home(monkeypatch, home)
        monkeypatch.setattr(
            "mcp_server.paths.is_invalid_project_root",
            lambda p: "refused (stubbed)",
        )

        assert [p for p, _ in _bad_registry_rows()] == [str(present)]


class TestTheFixerRemovesRowsAndNothingElse:
    def test_it_removes_the_bad_row(self, tmp_path, monkeypatch):
        home = tmp_path / "cvhome"
        datadir = home / "projects" / "Users_x_proj_abc123"
        datadir.mkdir(parents=True)
        _make_global_db(home, [(str(datadir), "proj")])
        _patch_home(monkeypatch, home)

        summary = _autofix_registry_rows()

        assert summary and "removed 1" in summary
        assert _rows(home) == set()
        assert check_registry_rows().state == "PASS"

    def test_it_never_deletes_a_data_directory(self, tmp_path, monkeypatch):
        """THE line this file exists for.

        A user wrongly bound to $HOME wrote their decisions into that data
        dir. `untrack` would delete it. If this fixer ever starts calling
        untrack — or removing directories itself — it destroys the memory it
        was meant to recover, and it does so on a `doctor --fix` the user ran
        to be helpful.
        """
        home = tmp_path / "cvhome"
        datadir = home / "projects" / "Users_x_proj_abc123"
        datadir.mkdir(parents=True)
        decisions = datadir / "decisions.jsonl"
        decisions.write_text('{"id":"D1","decision":"precious"}\n')
        _make_global_db(home, [(str(datadir), "proj")])
        _patch_home(monkeypatch, home)

        _autofix_registry_rows()

        assert datadir.is_dir(), "the fixer deleted a data directory"
        assert decisions.read_text() == '{"id":"D1","decision":"precious"}\n', (
            "the fixer destroyed recorded decisions — this is the failure mode "
            "the whole check was designed around"
        )

    def test_it_leaves_real_projects_registered(self, tmp_path, monkeypatch):
        """A fixer that over-removes would unregister working projects and the
        user would discover it as 'my memory disappeared'."""
        home = tmp_path / "cvhome"
        good = _real_project(tmp_path, "keepme")
        datadir = home / "projects" / "Users_x_bad_abc123"
        datadir.mkdir(parents=True)
        _make_global_db(home, [(str(good), "keepme"), (str(datadir), "bad")])
        _patch_home(monkeypatch, home)

        _autofix_registry_rows()

        assert _rows(home) == {str(good)}, "a real project was unregistered"

    def test_a_clean_registry_is_a_noop(self, tmp_path, monkeypatch):
        """None means 'nothing to change' — apply_autofixes prints that
        differently from a change, so returning a summary here would report
        work that never happened."""
        home = tmp_path / "cvhome"
        proj = _real_project(tmp_path, "fine")
        _make_global_db(home, [(str(proj), "fine")])
        _patch_home(monkeypatch, home)

        assert _autofix_registry_rows() is None


class TestItIsActuallyWiredIn:
    def test_the_fixer_is_registered_under_the_checks_name(self):
        """A fixer that exists but is not in _AUTOFIXERS never runs, and
        `doctor --fix` reports 'no auto-fixable issues found' while the check
        beside it says WARN. That is the shape of every bug this release
        fixed: correct code, not on the path."""
        assert _AUTOFIXERS.get("registry_rows") is _autofix_registry_rows
        assert check_registry_rows().name == "registry_rows"

    def test_the_check_runs_in_the_default_report(self):
        from mcp_server.doctor import _CHECKS

        assert check_registry_rows in _CHECKS

    def test_the_fix_command_does_not_tell_users_to_untrack(self):
        """4.2.0's guidance routed users through `untrack`, which deletes a
        data dir. The whole point of C13 is that they no longer need to."""
        home_cmd = check_registry_rows().fix_command
        assert "untrack" not in home_cmd, home_cmd


@pytest.mark.parametrize("broken", ["not-a-db", ""])
def test_an_unreadable_registry_warns_instead_of_raising(broken, tmp_path, monkeypatch):
    """A doctor check that tracebacks takes the whole report with it."""
    home = tmp_path / "cvhome"
    home.mkdir(parents=True)
    (home / "global.db").write_text(broken)
    _patch_home(monkeypatch, home)

    r = check_registry_rows()
    assert r.state in ("PASS", "WARN"), r.state
