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

    def test_memory_stays_in_repo_and_is_mirrored(self, legacy_project):
        """v3.7.1: the in-repo store is authoritative and is NOT moved.

        Previously this asserted the source was renamed to .codevira.migrated/.
        That rename is the data-loss bug — the memory layer only reads
        <project>/.codevira/, so moving it made decisions invisible.
        """
        proj, gh = legacy_project
        migrate.migrate_to_centralized(proj)

        assert (
            proj / ".codevira" / "decisions.jsonl"
        ).is_file(), "memory left the repo"
        assert not (proj / ".codevira.migrated").exists(), "source was renamed away"
        # Still mirrored centrally (harmless backup; not the read path).
        assert (_centralized(gh, proj) / "decisions.jsonl").is_file()


class TestIncompleteCopyIsHarmless:
    def test_in_repo_memory_survives_a_failed_mirror(self, legacy_project, monkeypatch):
        """A failed/partial mirror must never affect the in-repo store.

        v3.7.1 made this structurally safe rather than guarded: since nothing is
        renamed, an incomplete copy can only leave the CENTRAL mirror short —
        the authoritative in-repo store is untouched either way. (The previous
        version needed an explicit guard because it was about to rename the
        source away.)
        """
        proj, gh = legacy_project

        real_copy = migrate.shutil.copy2

        def _skip_decisions(src, dst, *a, **k):
            if Path(src).name == "decisions.jsonl":
                return dst  # simulate a copy that silently didn't land
            return real_copy(src, dst, *a, **k)

        monkeypatch.setattr(migrate.shutil, "copy2", _skip_decisions)
        migrate.migrate_to_centralized(proj)

        # The store the read path uses is intact and complete.
        decisions = proj / ".codevira" / "decisions.jsonl"
        assert decisions.is_file()
        assert "keep me" in decisions.read_text()
        assert not (proj / ".codevira.migrated").exists()


class TestOrphanRecovery:
    def test_recovers_memory_stranded_by_old_migration(self, tmp_path, monkeypatch):
        """A project already broken by the old migration self-heals INTO THE
        IN-REPO STORE — where the read path actually looks.

        The first version of this recovery restored into the centralized dir,
        which is the same dead end that caused the bug, so it healed nothing.
        """
        proj = tmp_path / "broken"
        proj.mkdir()
        orphan = proj / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "decisions.jsonl").write_text('{"id":"D1","decision":"stranded"}\n')
        (orphan / "skills.jsonl").write_text('{"name":"s"}\n')

        global_home = tmp_path / "global"
        import mcp_server.paths as paths_mod

        monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)

        assert migrate._mig_v371_recover_orphaned_memory(proj) is True

        in_repo = proj / ".codevira"
        assert "stranded" in (in_repo / "decisions.jsonl").read_text()
        assert (in_repo / "skills.jsonl").is_file()

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
