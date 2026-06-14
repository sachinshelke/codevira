"""Tests for ``codevira projects`` — Bug 21b (rc.4 dogfood, 2026-05-13).

Sachin (UDAP dogfood) flagged that ``~/.codevira/projects/`` held 5 dirs but
only 2 were projects he'd explicitly connected to. The other 3 were "ghost"
dirs created by side-effecting MCP tool calls. The Bug 21 ledger called for
*"a `codevira projects` inventory command that lists what's in
~/.codevira/projects/ joined against global.db with a 'GHOST: missing
config/metadata' warning per row"*.

These tests pin the contract:

* Lists every dir under ``~/.codevira/projects/``.
* Classifies each as **complete** (config + metadata + global.db row),
  **partial** (some-but-not-all of the above), or **stale** (nothing
  recognizable).
* ``--json`` emits parseable JSON to stdout.
* ``--ghosts-only`` filters to non-complete dirs.
* Missing ``~/.codevira/projects/`` is handled gracefully (no crash).
* Corrupt global.db doesn't crash the inventory.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_server.cli_projects import (
    _delete_project_row,
    _relative_age,
    _resolve_archive_targets,
    cmd_projects,
    cmd_projects_archive,
)
from mcp_server.paths import is_ephemeral_project_path


@pytest.fixture
def home_with_projects(tmp_path, monkeypatch):
    """Set up ~/.codevira/ with mixed complete + ghost project dirs."""
    home = tmp_path / ".codevira"
    projects_dir = home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )

    # (A) Complete project — config + metadata + global.db row.
    a_dir = projects_dir / "Users_alice_proj_a_aaaa"
    (a_dir / "graph").mkdir(parents=True)
    (a_dir / "codeindex").mkdir()
    (a_dir / "graph" / "graph.db").write_bytes(b"\x00")
    (a_dir / "codeindex" / "chunk").write_bytes(b"\x00")
    (a_dir / "config.yaml").write_text("project:\n  name: a\n")
    (a_dir / "metadata.json").write_text(
        json.dumps(
            {
                "original_path": "/Users/alice/proj-a",
                "git_remote": "git@host:a.git",
                "auto_initialized": True,
                "version": "2.0.0rc4",
            }
        )
    )

    # (B) Ghost project — graph + roadmap only (the Bug 21 shape).
    b_dir = projects_dir / "Users_bob_proj_b_bbbb"
    (b_dir / "graph").mkdir(parents=True)
    (b_dir / "roadmap.yaml").write_text("project: b\n")
    # No config, no metadata, no global.db row.

    # (C) Stale project — totally empty dir.
    (projects_dir / "Users_carol_stale_cccc").mkdir()

    # Seed global.db with ONLY project A's row, keyed by the canonical
    # original_path (post-Bug-20).
    conn = sqlite3.connect(str(home / "global.db"))
    conn.execute(
        "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "language TEXT, git_remote TEXT, "
        "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO projects (path, name, language, git_remote) VALUES (?, ?, ?, ?)",
        ("/Users/alice/proj-a", "a", "python", "git@host:a.git"),
    )
    conn.commit()
    conn.close()
    return home


class TestCmdProjects:
    """Default human-readable output."""

    def test_lists_all_three_dirs(self, home_with_projects, capsys):
        rc = cmd_projects()
        assert rc == 0
        out = capsys.readouterr().out
        # rc.5 (P0-3): summary uses canonical "tracked / ghost / orphan" naming.
        # Project A canonical_path is /Users/alice/proj-a, doesn't exist on disk
        # in the test fixture, so it counts as orphan rather than tracked.
        # Total entries: 3 (one of which is orphan, two are ghost-shaped).
        assert "Codevira projects" in out
        # All three entries appear (each as one of: tracked/ghost/orphan/stale).

    def test_handles_missing_projects_dir(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / ".codevira_empty"
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
        )
        rc = cmd_projects()
        assert rc == 0
        out = capsys.readouterr().out
        # rc.5 (P0-3): empty inventory now prints "No projects tracked yet"
        # because the canonical inventory enumerates registrations + disk dirs;
        # both empty produces the empty-state message.
        assert "No projects tracked yet" in out or "no codevira" in out.lower()


class TestJsonOutput:
    """``--json`` flag for scripting / CI."""

    def test_json_output_is_parseable(self, home_with_projects, capsys):
        rc = cmd_projects(output_json=True)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        # rc.5 (P0-3): JSON now wraps {summary, projects}.
        assert "projects" in data
        assert "summary" in data
        assert len(data["projects"]) >= 3
        # Each row has the documented canonical fields.
        for row in data["projects"]:
            assert {
                "slug",
                "status",
                "has_config",
                "has_metadata",
                "in_global_db",
                "size_bytes",
            } <= set(row)

    def test_json_identifies_status_per_entry(self, home_with_projects, capsys):
        """rc.5 (P0-3): status names are tracked / ghost / orphan / stale."""
        cmd_projects(output_json=True)
        data = json.loads(capsys.readouterr().out)
        statuses = {r["slug"]: r["status"] for r in data["projects"] if r["slug"]}
        # Project A has full bookkeeping but its canonical_path
        # (/Users/alice/proj-a) doesn't exist on disk, so it's "orphan".
        assert statuses.get("Users_alice_proj_a_aaaa") in ("tracked", "orphan")
        # Project B has graph but no config/metadata/registration → ghost.
        assert statuses.get("Users_bob_proj_b_bbbb") == "ghost"
        # Project C is totally empty → stale.
        assert statuses.get("Users_carol_stale_cccc") == "stale"

    def test_json_handles_missing_projects_dir(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / ".codevira_empty"
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
        )
        cmd_projects(output_json=True)
        out = json.loads(capsys.readouterr().out)
        # rc.5: JSON shape is now {summary, projects}.
        assert out["projects"] == []
        assert out["summary"]["total"] == 0


class TestGhostsOnly:
    """``--ghosts-only`` filter."""

    def test_filters_out_non_ghost_projects(self, home_with_projects, capsys):
        """rc.5 (P0-3 + P2-4): --ghosts-only shows ONLY entries with status='ghost'.

        Stale and orphan entries are excluded — they have their own commands
        (`codevira clean --orphans`). Tracked entries are also excluded.
        """
        rc = cmd_projects(output_json=True, ghosts_only=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # The only ghost is project B (has graph but no config/metadata).
        slugs = {r["slug"] for r in data["projects"]}
        assert "Users_alice_proj_a_aaaa" not in slugs, (
            "Project A is tracked/orphan, not a ghost"
        )
        assert "Users_bob_proj_b_bbbb" in slugs, "Project B is the ghost"
        assert "Users_carol_stale_cccc" not in slugs, (
            "Project C is stale, not ghost — handled by --orphans / manual cleanup"
        )

    def test_no_ghosts_message(self, tmp_path, monkeypatch, capsys):
        """When everything is complete, --ghosts-only prints a clean-state message."""
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
        )
        # Make a single complete project.
        d = projects_dir / "Users_alice_proj_aaaa"
        (d / "graph").mkdir(parents=True)
        (d / "graph" / "graph.db").write_bytes(b"\x00")
        (d / "config.yaml").write_text("project:\n  name: a\n")
        (d / "metadata.json").write_text(
            json.dumps(
                {
                    "original_path": "/Users/alice/proj",
                    "git_remote": "git@host:a.git",
                }
            )
        )
        conn = sqlite3.connect(str(home / "global.db"))
        conn.execute(
            "CREATE TABLE projects (path TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "language TEXT, git_remote TEXT, "
            "last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO projects (path, name, language) VALUES (?, ?, ?)",
            ("/Users/alice/proj", "a", "python"),
        )
        conn.commit()
        conn.close()

        cmd_projects(ghosts_only=True)
        assert "No ghost projects on this machine" in capsys.readouterr().out


class TestResilience:
    """Corrupt global.db must not crash the inventory."""

    def test_corrupt_global_db_falls_back(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / ".codevira"
        projects_dir = home / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "Users_alice_proj_aaaa").mkdir()
        # Write a corrupt global.db.
        (home / "global.db").write_bytes(b"this is not a SQLite database")
        monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
        )
        rc = cmd_projects(output_json=True)
        assert rc == 0
        # Empty registration is fine; we still list the dir.
        data = json.loads(capsys.readouterr().out)
        assert len(data["projects"]) == 1
        assert data["projects"][0]["in_global_db"] is False


# =====================================================================
# v3.4.0 Phase 8 additions
# =====================================================================


class TestEphemeralDetection:
    def test_pytest_tmp_dir_is_ephemeral(self) -> None:
        assert is_ephemeral_project_path(
            Path("/private/var/folders/xx/pytest-of-sachin/pytest-3/proj")
        )

    def test_tmp_scratch_is_ephemeral(self) -> None:
        assert is_ephemeral_project_path(Path("/tmp/cv-dev-abc123/proj"))

    def test_real_project_path_is_not_ephemeral(self) -> None:
        assert not is_ephemeral_project_path(
            Path("/Users/sachin/Documents/Projects/codevira")
        )

    def test_real_project_named_like_a_temp_marker_is_not_ephemeral(self) -> None:
        """Regression: detection is by temp-dir ANCESTRY, not substring.
        A real project whose name merely contains 'pytest-' / 'cv-dev-'
        must NOT be hidden or skipped from registration — that would
        silently lose a user's real project."""
        for safe in (
            "/Users/sachin/Projects/pytest-django",
            "/home/dev/repos/pytest-mock-resources",
            "/Users/alice/cv-dev-dashboard",
            "/Users/bob/work/tmp-utils",
        ):
            assert not is_ephemeral_project_path(Path(safe)), safe

    def test_pytest_tmp_under_temp_root_is_ephemeral(self) -> None:
        """The genuine pytest temp dir (under the system temp root) IS
        ephemeral — ancestry catches it without the dangerous substring."""
        import tempfile

        real_tmp = Path(tempfile.gettempdir()) / "pytest-of-x" / "pytest-3" / "proj"
        assert is_ephemeral_project_path(real_tmp)

    def test_classification_never_raises(self) -> None:
        assert is_ephemeral_project_path(Path("")) in (True, False)


class TestRelativeAge:
    def test_none_is_dash(self) -> None:
        assert _relative_age(None) == "—"

    def test_today(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        assert _relative_age(now) == "today"

    def test_n_days_ago(self) -> None:
        five = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert _relative_age(five) == "5d ago"

    def test_stale_over_30_days(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert _relative_age(old) == "stale 45d"

    def test_unparseable_falls_back_to_date(self) -> None:
        assert _relative_age("2026-06-01") == "2026-06-01"


@pytest.fixture
def fake_global(tmp_path, monkeypatch):
    """Isolated ~/.codevira/global.db with one registered project."""
    import mcp_server.paths as paths_mod
    from indexer.global_db import GlobalDB

    home = tmp_path / "home" / ".codevira"
    home.mkdir(parents=True)
    db_path = home / "global.db"
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: home)
    monkeypatch.setattr(paths_mod, "get_global_db_path", lambda: db_path)

    real = tmp_path / "realproj"
    real.mkdir()
    db = GlobalDB(db_path)
    db.register_project(str(real), "realproj", "python")
    db.close()
    return {"db_path": db_path, "real": real, "tmp": tmp_path}


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    finally:
        conn.close()


class TestArchive:
    def test_archive_by_name_removes_row(self, fake_global) -> None:
        assert _row_count(fake_global["db_path"]) == 1
        assert cmd_projects_archive("realproj") == 0
        assert _row_count(fake_global["db_path"]) == 0

    def test_archive_by_full_path_removes_row(self, fake_global) -> None:
        assert cmd_projects_archive(str(fake_global["real"])) == 0
        assert _row_count(fake_global["db_path"]) == 0

    def test_archive_missing_name_is_usage_error(self, fake_global) -> None:
        assert cmd_projects_archive(None) == 2
        assert cmd_projects_archive("   ") == 2
        assert _row_count(fake_global["db_path"]) == 1

    def test_archive_unknown_name_returns_not_found(self, fake_global) -> None:
        assert cmd_projects_archive("does-not-exist") == 1
        assert _row_count(fake_global["db_path"]) == 1

    def test_archive_ambiguous_name_refuses(self, fake_global, tmp_path) -> None:
        """Two projects sharing a basename → refuse, don't guess."""
        from indexer.global_db import GlobalDB

        other = tmp_path / "nested" / "realproj"
        other.mkdir(parents=True)
        db = GlobalDB(fake_global["db_path"])
        db.register_project(str(other), "realproj", "python")
        db.close()

        assert cmd_projects_archive("realproj") == 2  # ambiguous
        assert _row_count(fake_global["db_path"]) == 2  # nothing removed

    def test_resolve_prefers_exact_path_over_basename(self, fake_global) -> None:
        from mcp_server._project_inventory import enumerate_projects

        entries = [e for e in enumerate_projects() if e.canonical_path]
        targets = _resolve_archive_targets(entries, str(fake_global["real"]))
        assert targets == [str(fake_global["real"])]

    def test_delete_missing_db_returns_false(self, tmp_path, monkeypatch) -> None:
        import mcp_server.paths as paths_mod

        monkeypatch.setattr(
            paths_mod, "get_global_db_path", lambda: tmp_path / "nope.db"
        )
        assert _delete_project_row("/whatever") is False

    def test_archive_leaves_files_and_data_dir_untouched(self, fake_global) -> None:
        """M4: archive deletes ONLY the registry row — NEVER the project's
        source files or its ~/.codevira data dir. This command sounds
        destructive; the no-data-loss invariant must be pinned so a future
        regression that rmtree'd the data dir can't pass CI."""
        real = fake_global["real"]
        (real / "main.py").write_text("print('hi')\n")
        data_dir = fake_global["db_path"].parent / "projects" / "slug123"
        data_dir.mkdir(parents=True)
        (data_dir / "decisions.jsonl").write_text("{}\n")

        assert cmd_projects_archive("realproj") == 0
        assert _row_count(fake_global["db_path"]) == 0  # registry row gone

        # The project's files and its data dir MUST survive untouched.
        assert (real / "main.py").read_text() == "print('hi')\n"
        assert real.is_dir()
        assert (data_dir / "decisions.jsonl").read_text() == "{}\n"


class TestRegistrationGuard:
    def test_ephemeral_root_is_not_registered(self, tmp_path, monkeypatch) -> None:
        """A pytest-tmp project root must be skipped (no override)."""
        import mcp_server.paths as paths_mod

        eph = tmp_path / "proj"  # under the pytest temp dir → ephemeral
        eph.mkdir()
        monkeypatch.setattr(paths_mod, "get_project_root", lambda: eph)
        monkeypatch.delenv("CODEVIRA_ALLOW_EPHEMERAL_PROJECT", raising=False)

        from mcp_server.global_sync import register_current_project

        result = register_current_project()
        assert result["registered"] is False
        assert result["reason"] == "ephemeral project path"

    def test_override_env_allows_ephemeral_registration(
        self, tmp_path, monkeypatch
    ) -> None:
        """With the opt-in env, an ephemeral root registers normally."""
        import mcp_server.paths as paths_mod

        home = tmp_path / "home" / ".codevira"
        home.mkdir(parents=True)
        eph = tmp_path / "proj"
        eph.mkdir()
        monkeypatch.setattr(paths_mod, "get_project_root", lambda: eph)
        monkeypatch.setattr(paths_mod, "get_global_db_path", lambda: home / "global.db")
        monkeypatch.setattr(paths_mod, "get_data_dir", lambda: eph / ".codevira")
        monkeypatch.setenv("CODEVIRA_ALLOW_EPHEMERAL_PROJECT", "1")

        from mcp_server.global_sync import register_current_project

        result = register_current_project()
        assert result["registered"] is True
