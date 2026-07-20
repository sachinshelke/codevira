"""
test_migration_no_data_loss_e2e.py — v3.7.1: the migration must never make a
project's decisions unreadable.

This is the end-to-end guarantee, written against the READ path rather than the
copy path — the previous fix passed its own tests while still losing data,
because it verified that files were copied into the centralized dir instead of
verifying that decisions were still READABLE afterwards.

Why that mattered: `storage/paths.py::codevira_dir()` hardcodes
`<project>/.codevira` and is the sole route for every memory file.
`get_data_dir()` is only used for graph.db / codeindex / logs / config.yaml.
So the centralized store is write-only for memory, and renaming the in-repo
store away made everything invisible (5 recorded, 0 readable).

The rule these tests encode: after ANY migration, what you could read before
you must still be able to read.
"""

from __future__ import annotations

import pytest

from mcp_server import migrate
from mcp_server.storage import paths as store_paths


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An initialized project with real decisions, global home isolated."""
    proj = tmp_path / "proj"
    cv = proj / ".codevira"
    cv.mkdir(parents=True)
    (cv / "config.yaml").write_text("schema_version: 1\nproject_name: proj\n")
    (cv / "decisions.jsonl").write_text(
        "\n".join(f'{{"id":"D{i}","decision":"decision number {i}"}}' for i in range(5))
        + "\n"
    )
    (cv / "sessions.jsonl").write_text('{"session_id":"s1"}\n')
    (cv / "skills.jsonl").write_text('{"name":"rebase"}\n')

    global_home = tmp_path / "global"
    (global_home / "projects").mkdir(parents=True)
    import mcp_server.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_global_home", lambda: global_home)
    monkeypatch.setattr(migrate, "_get_git_remote_url", lambda p: None, raising=False)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()
    yield proj
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()


def _readable_decisions(proj) -> int:
    """Count decisions via the REAL read path (not by peeking at a file)."""
    path = store_paths.decisions_path(proj)
    if not path.is_file():
        return 0
    return len([ln for ln in path.read_text().splitlines() if ln.strip()])


class TestMigrationPreservesReadableMemory:
    def test_decisions_still_readable_after_migration(self, project):
        """THE regression. Before the fix this returned 0 of 5."""
        assert _readable_decisions(project) == 5

        result = migrate.migrate_to_centralized(project)
        assert result["migrated"] is True, result

        assert (
            _readable_decisions(project) == 5
        ), "migration made decisions unreadable — the exact data-loss bug"

    def test_in_repo_store_is_not_renamed_away(self, project):
        migrate.migrate_to_centralized(project)
        assert (project / ".codevira").is_dir(), "in-repo store was moved"
        assert not (
            project / ".codevira.migrated"
        ).exists(), "migration renamed the source away — nothing may be stranded"

    def test_all_memory_files_remain_in_repo(self, project):
        migrate.migrate_to_centralized(project)
        for name in ("decisions.jsonl", "sessions.jsonl", "skills.jsonl"):
            assert (project / ".codevira" / name).is_file(), f"{name} left the repo"


class TestRecoveryRestoresIntoTheReadPath:
    def test_restores_stranded_memory_in_repo(self, project):
        """A project already broken by the old migration must self-heal into
        <project>/.codevira/ — where the read path actually looks."""
        cv = project / ".codevira"
        orphan = project / ".codevira.migrated"
        cv.rename(orphan)  # reproduce the old destructive rename
        assert _readable_decisions(project) == 0  # broken state

        assert migrate._mig_v371_recover_orphaned_memory(project) is True
        assert _readable_decisions(project) == 5, "recovery did not restore memory"

    def test_never_clobbers_a_live_in_repo_store(self, project):
        orphan = project / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "decisions.jsonl").write_text('{"id":"OLD","decision":"stale"}\n')

        migrate._mig_v371_recover_orphaned_memory(project)
        text = (project / ".codevira" / "decisions.jsonl").read_text()
        assert "stale" not in text, "stale backup clobbered the live store"
        assert _readable_decisions(project) == 5

    def test_does_not_copy_derived_artifacts_into_the_repo(self, project):
        """graph/ can be 20M+ — it must stay centralized, never restored."""
        orphan = project / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "graph").mkdir()
        (orphan / "graph" / "graph.db").write_bytes(b"x" * 1000)

        migrate._mig_v371_recover_orphaned_memory(project)
        assert not (project / ".codevira" / "graph").exists()


class TestBackupIsNotDestroyed:
    def test_cleanup_refuses_when_backup_is_the_only_copy(self, project):
        """`clean --legacy` used to rmtree this unconditionally."""
        cv = project / ".codevira"
        orphan = project / ".codevira.migrated"
        cv.rename(orphan)

        assert migrate.cleanup_legacy_dir(project) is False
        assert (orphan / "decisions.jsonl").is_file(), "the only copy was deleted"

    def test_cleanup_allowed_once_memory_is_safe_in_repo(self, project):
        orphan = project / ".codevira.migrated"
        orphan.mkdir()
        (orphan / "decisions.jsonl").write_text('{"id":"D0"}\n')
        # in-repo already has decisions.jsonl (from the fixture), so it's safe.
        assert migrate.cleanup_legacy_dir(project) is True
        assert not orphan.exists()


class TestRecoveryAlwaysRetries:
    def test_recovery_is_never_sealed_by_the_ledger(self):
        """The ledger is the idempotency key; sealing recovery meant a user
        stranded later could never be healed."""
        assert "v371_recover_orphaned_memory" in migrate._ALWAYS_RERUN
