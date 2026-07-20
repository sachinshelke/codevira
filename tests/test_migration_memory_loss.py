"""
test_migration_memory_loss.py — v3.7.1: the centralization migration must
carry the store's MEMORY, not just config/roadmap/graph.

The pre-3.7.1 ``migrate_to_centralized`` copied config.yaml, roadmap.yaml,
graph.db and codeindex/, then renamed ``.codevira/`` to ``.codevira.migrated/``
— WITHOUT copying decisions.jsonl / sessions.jsonl / outcomes.jsonl /
skills.jsonl / digest.jsonl / manifest.yaml. Every migrated project therefore
read as ZERO decisions while its real history sat stranded in the renamed dir.
Observed live: a project with 496 decisions showed 0 after an IDE restart.

These tests pin three guarantees:
  1. migration carries every store file (by exclusion, so new files too),
  2. it refuses to rename the source away after an incomplete copy,
  3. an already-orphaned project self-heals from .codevira.migrated/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server import migrate


@pytest.fixture
def legacy_project(tmp_path, monkeypatch):
    """A project with a populated legacy in-repo store, global home isolated."""
    proj = tmp_path / "proj"
    cv = proj / ".codevira"
    cv.mkdir(parents=True)
    (cv / "config.yaml").write_text("schema_version: 1\n")
    (cv / "roadmap.yaml").write_text("project: proj\nversion: '1.0'\n")
    # The memory that used to be silently dropped:
    (cv / "decisions.jsonl").write_text(
        '{"id":"D1","decision":"keep me"}\n{"id":"D2","decision":"me too"}\n'
    )
    (cv / "sessions.jsonl").write_text('{"session_id":"s1"}\n')
    (cv / "outcomes.jsonl").write_text('{"id":"D1","outcome":"kept"}\n')
    (cv / "skills.jsonl").write_text('{"name":"rebase"}\n')
    (cv / "digest.jsonl").write_text('{"id":"D1","weight":1}\n')
    (cv / "manifest.yaml").write_text("total_decisions: 2\n")
    (cv / "stale.lock").write_text("")  # must NOT travel

    global_home = tmp_path / "global"
    (global_home / "projects").mkdir(parents=True)
    monkeypatch.setattr(migrate, "_get_git_remote_url", lambda p: None, raising=False)
    import mcp_server.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)
    return proj, global_home


def _centralized(global_home: Path, proj: Path) -> Path:
    from mcp_server.paths import _sanitize_path_key

    return global_home / "projects" / _sanitize_path_key(proj)


class TestMigrationCarriesMemory:
    def test_decisions_survive_migration(self, legacy_project):
        """THE regression: decisions.jsonl must reach the centralized store.
        FAILS before the fix — the file was never copied."""
        proj, gh = legacy_project
        result = migrate.migrate_to_centralized(proj)
        assert result["migrated"] is True, result

        dst = _centralized(gh, proj)
        decisions = dst / "decisions.jsonl"
        assert decisions.is_file(), "decisions.jsonl did not reach the new store"
        assert "keep me" in decisions.read_text()
        assert len(decisions.read_text().strip().splitlines()) == 2

    def test_all_memory_files_survive(self, legacy_project):
        proj, gh = legacy_project
        migrate.migrate_to_centralized(proj)
        dst = _centralized(gh, proj)
        for name in (
            "sessions.jsonl",
            "outcomes.jsonl",
            "skills.jsonl",
            "digest.jsonl",
            "manifest.yaml",
        ):
            assert (dst / name).is_file(), f"{name} was dropped by the migration"

    def test_lock_files_do_not_travel(self, legacy_project):
        proj, gh = legacy_project
        migrate.migrate_to_centralized(proj)
        assert not (_centralized(gh, proj) / "stale.lock").exists()

    def test_legacy_renamed_only_after_complete_copy(self, legacy_project):
        proj, gh = legacy_project
        migrate.migrate_to_centralized(proj)
        # Source renamed to the safety net, and the new store really has memory.
        assert (proj / ".codevira.migrated" / "decisions.jsonl").is_file()
        assert (_centralized(gh, proj) / "decisions.jsonl").is_file()


class TestIncompleteCopyGuard:
    def test_refuses_to_rename_when_copy_incomplete(self, legacy_project, monkeypatch):
        """If the copy fails to land a store file, the legacy dir MUST stay put
        so the next run can retry — never strand memory behind a rename."""
        proj, gh = legacy_project

        real_copy = migrate.shutil.copy2

        def _skip_decisions(src, dst, *a, **k):
            if Path(src).name == "decisions.jsonl":
                return dst  # simulate a copy that silently didn't land
            return real_copy(src, dst, *a, **k)

        monkeypatch.setattr(migrate.shutil, "copy2", _skip_decisions)
        result = migrate.migrate_to_centralized(proj)

        assert result["migrated"] is False
        assert "incomplete" in result["reason"]
        # Legacy store still intact and NOT renamed away.
        assert (proj / ".codevira" / "decisions.jsonl").is_file()
        assert not (proj / ".codevira.migrated").exists()


class TestOrphanRecovery:
    def test_recovers_memory_stranded_by_old_migration(self, tmp_path, monkeypatch):
        """A project already broken by the old migration self-heals: memory in
        .codevira.migrated/ is copied into the empty centralized store."""
        proj = tmp_path / "broken"
        proj.mkdir()
        orphan = proj / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "decisions.jsonl").write_text('{"id":"D1","decision":"stranded"}\n')
        (orphan / "skills.jsonl").write_text('{"name":"s"}\n')

        global_home = tmp_path / "global"
        import mcp_server.paths as paths_mod

        monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)
        from mcp_server.paths import _sanitize_path_key

        dst = global_home / "projects" / _sanitize_path_key(proj)
        dst.mkdir(parents=True)
        (dst / "config.yaml").write_text("schema_version: 1\n")  # empty store

        assert migrate._mig_v371_recover_orphaned_memory(proj) is True
        assert "stranded" in (dst / "decisions.jsonl").read_text()
        assert (dst / "skills.jsonl").is_file()

    def test_never_clobbers_newer_central_copy(self, tmp_path, monkeypatch):
        proj = tmp_path / "p"
        proj.mkdir()
        orphan = proj / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "decisions.jsonl").write_text('{"id":"OLD"}\n')

        global_home = tmp_path / "global"
        import mcp_server.paths as paths_mod

        monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)
        from mcp_server.paths import _sanitize_path_key

        dst = global_home / "projects" / _sanitize_path_key(proj)
        dst.mkdir(parents=True)
        (dst / "decisions.jsonl").write_text('{"id":"NEW"}\n')

        migrate._mig_v371_recover_orphaned_memory(proj)
        assert "NEW" in (dst / "decisions.jsonl").read_text()
        assert "OLD" not in (dst / "decisions.jsonl").read_text()

    def test_noop_without_orphan_dir(self, tmp_path):
        proj = tmp_path / "clean"
        proj.mkdir()
        assert migrate._mig_v371_recover_orphaned_memory(proj) is False


class TestInRepoStoreIsNotMigrated:
    """v3.7.1: a store that deliberately lives in the repo must never be
    migrated away — it would break team sharing and look like data loss."""

    def test_git_shared_store_is_left_alone(self, legacy_project):
        proj, gh = legacy_project
        cfg = proj / ".codevira" / "config.yaml"
        cfg.write_text("schema_version: 1\ngit_shared: true\n")

        result = migrate.migrate_to_centralized(proj)
        assert result["migrated"] is False
        assert "git_shared" in result["reason"]
        # Store stays exactly where the team expects it.
        assert (proj / ".codevira" / "decisions.jsonl").is_file()
        assert not (proj / ".codevira.migrated").exists()

    def test_git_tracked_store_is_left_alone(self, legacy_project, monkeypatch):
        proj, gh = legacy_project
        import mcp_server.paths as paths_mod

        monkeypatch.setattr(
            paths_mod,
            "git_tracked_memory_files",
            lambda p: [".codevira/decisions.jsonl"],
        )
        result = migrate.migrate_to_centralized(proj)
        assert result["migrated"] is False
        assert "git-tracked" in result["reason"]
        assert (proj / ".codevira" / "decisions.jsonl").is_file()
        assert not (proj / ".codevira.migrated").exists()
