"""
test_review_round2.py — defects found by an adversarial review of the FIRST
round of v3.7.1 fixes. Every one of these is a bug I introduced while fixing
something else, so they are pinned explicitly.

1. `clean --legacy` still permanently deleted data. The deletion guard checked
   only top-level ``.jsonl``/``.yaml``, while recovery restored a 10-name
   allowlist. Anything in neither set — ``checkpoints/``, ``learned_weights.json``,
   ``working_archived/`` (documented as canonical and team-shareable) — was
   deleted with no warning. A name in one set but not the other could also wedge
   the guard shut forever while advising a recovery that would never restore it.

2. Recovery fabricated an in-repo ``config.yaml`` from the CENTRALIZED dir,
   violating ``opt_in.py``'s stated invariant that a config.yaml is written only
   by explicit init — silently marking a centralized-only project "initialized".

3. ``_is_provisional`` re-resolved in full on EVERY call for a not-yet-inited
   project (a git subprocess + a metadata scan), a permanent 100% cache miss.
"""

from __future__ import annotations

import pytest

import mcp_server.paths as paths
from mcp_server import migrate


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".codevira").mkdir(parents=True)
    (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    home = tmp_path / "global"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setattr(paths, "get_global_home", lambda: home)
    paths.invalidate_data_dir_cache()
    yield proj
    paths.invalidate_data_dir_cache()


class TestCleanupNeverDeletesUnrestorableData:
    @pytest.mark.parametrize(
        "name,is_dir",
        [
            ("checkpoints", True),
            ("working_archived", True),
            ("learned_weights.json", False),
            ("neighborhoods.yaml", False),
            ("induction_proposals.jsonl", False),
            ("decisions.jsonl.bak-2026-06-16", False),
        ],
    )
    def test_refuses_when_backup_holds_unique_data(self, project, name, is_dir):
        """THE regression: these were deleted permanently, with no warning."""
        backup = project / ".codevira.migrated"
        backup.mkdir()
        if is_dir:
            (backup / name).mkdir()
            (backup / name / "x.jsonl").write_text('{"a":1}\n')
        else:
            (backup / name).write_text("precious")

        assert migrate.cleanup_legacy_dir(project) is False
        assert (backup / name).exists(), f"{name} was permanently deleted"

    def test_derived_artifacts_never_block_deletion(self, project):
        """graph/codeindex/logs are rebuildable — they must not wedge the guard."""
        backup = project / ".codevira.migrated"
        (backup / "graph").mkdir(parents=True)
        (backup / "graph" / "graph.db").write_bytes(b"x" * 100)
        (backup / "codeindex").mkdir()
        (backup / "logs").mkdir()
        (backup / "config.yaml").write_text("schema_version: 1\n")  # exists in-repo

        assert migrate.cleanup_legacy_dir(project) is True
        assert not backup.exists()

    def test_guard_and_recovery_agree(self, project):
        """No wedge: whatever the guard demands, recovery can restore.

        Previously a name could be in the guard's set but not recovery's, so the
        guard refused forever while telling the user to run a recovery that
        would never satisfy it.
        """
        backup = project / ".codevira.migrated"
        backup.mkdir()
        for n in ("neighborhoods.yaml", "learned_weights.json"):
            (backup / n).write_text("x")
        (backup / "checkpoints").mkdir()
        (backup / "checkpoints" / "c1.json").write_text("{}")

        assert migrate.cleanup_legacy_dir(project) is False  # protected
        migrate._mig_v371_recover_orphaned_memory(project)  # heals
        assert migrate.cleanup_legacy_dir(project) is True  # now safe
        assert (project / ".codevira" / "neighborhoods.yaml").is_file()
        assert (project / ".codevira" / "checkpoints" / "c1.json").is_file()


class TestRecoveryDoesNotFabricateConfig:
    def test_centralized_config_is_not_copied_in_repo(self, tmp_path, monkeypatch):
        """opt_in.py: a config.yaml is written ONLY by explicit init."""
        proj = tmp_path / "central_only"
        proj.mkdir()
        home = tmp_path / "global"
        monkeypatch.setattr(paths, "get_global_home", lambda: home)
        central = home / "projects" / paths._sanitize_path_key(proj)
        central.mkdir(parents=True)
        (central / "config.yaml").write_text("schema_version: 1\n")

        migrate._mig_v371_recover_orphaned_memory(proj)

        assert not (proj / ".codevira" / "config.yaml").exists(), (
            "recovery fabricated an in-repo config.yaml, marking a "
            "centralized-only project as initialized"
        )

    def test_config_from_a_real_in_repo_backup_is_restored(self, tmp_path, monkeypatch):
        """A config.yaml inside .codevira.migrated/ WAS an in-repo store — that
        one is legitimate to restore."""
        proj = tmp_path / "stranded"
        backup = proj / ".codevira.migrated"
        backup.mkdir(parents=True)
        (backup / "config.yaml").write_text("schema_version: 1\n")
        (backup / "decisions.jsonl").write_text('{"id":"D1"}\n')
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path / "global")

        migrate._mig_v371_recover_orphaned_memory(proj)

        assert (proj / ".codevira" / "config.yaml").is_file()
        assert (proj / ".codevira" / "decisions.jsonl").is_file()


class TestProvisionalCacheIsCheap:
    def test_no_subprocess_when_nothing_appeared(self, tmp_path, monkeypatch):
        """THE regression: every call re-ran a git subprocess + metadata scan.

        This is the test the reviewer called theatre in its first form — it
        previously mocked out the very cost it claimed to avoid. Here the git
        lookup is instrumented, not neutralized.
        """
        proj = tmp_path / "uninited"
        proj.mkdir()
        home = tmp_path / "global"
        (home / "projects").mkdir(parents=True)
        monkeypatch.setattr(paths, "get_global_home", lambda: home)

        calls = {"n": 0}

        def _counting_remote(p):
            calls["n"] += 1
            return None

        monkeypatch.setattr(paths, "_get_git_remote_url", _counting_remote)
        monkeypatch.setattr(paths, "get_project_root", lambda: proj)
        paths.invalidate_data_dir_cache()

        for _ in range(20):
            paths.get_data_dir()

        assert calls["n"] <= 1, (
            f"resolution ran {calls['n']} git lookups for 20 calls — "
            "the provisional cache is a permanent miss"
        )

    def test_still_picks_up_a_store_created_later(self, tmp_path, monkeypatch):
        """Cheapness must not cost correctness: the whole point of the
        provisional entry is to notice a store created by another process."""
        proj = tmp_path / "later"
        proj.mkdir()
        home = tmp_path / "global"
        (home / "projects").mkdir(parents=True)
        monkeypatch.setattr(paths, "get_global_home", lambda: home)
        monkeypatch.setattr(paths, "_get_git_remote_url", lambda p: None)
        monkeypatch.setattr(paths, "get_project_root", lambda: proj)
        paths.invalidate_data_dir_cache()

        first = paths.get_data_dir()
        assert first != proj / ".codevira"

        (proj / ".codevira").mkdir()
        (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")

        assert (
            paths.get_data_dir() == proj / ".codevira"
        ), "a store created in another process was never noticed"
