"""
test_cli_transfer.py — v3.3.0 Phase 6: `codevira export setup` / `codevira import`.

Round-trips real tar.gz archives through real filesystems and a real
global.db (no mocks on the storage layer) so format drift fails fast.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from mcp_server.cli_transfer import cmd_export_setup, cmd_import_setup


@pytest.fixture()
def machine_a(tmp_path, monkeypatch):
    """Source machine: project with .codevira/ + a global.db with learning."""
    project = tmp_path / "machine-a" / "proj"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    cv = project / ".codevira"
    cv.mkdir()
    (cv / "decisions.jsonl").write_text(
        json.dumps({"id": "D000001", "decision": "Use bcrypt"}) + "\n",
        encoding="utf-8",
    )
    (cv / "config.yaml").write_text("project: proj\n", encoding="utf-8")

    home = tmp_path / "machine-a" / "home" / ".codevira"
    home.mkdir(parents=True)
    _seed_global_db(home / "global.db")

    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(project))
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )
    monkeypatch.chdir(project)
    return project


def _seed_global_db(path: Path) -> None:
    from indexer.global_db import GlobalDB

    db = GlobalDB(path)
    db.upsert_preference(
        category="style",
        signal="short answers",
        example="keep explanations short",
        source_project="proj",
        frequency=4,
    )
    db.upsert_rule(
        rule_text="always run ruff before commit",
        confidence=0.8,
        source_project="proj",
        category="workflow",
        language="python",
    )
    db.close()


def _switch_to_machine_b(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Fresh machine: empty project, empty home. Returns (project, global_db)."""
    project = tmp_path / "machine-b" / "proj"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    home = tmp_path / "machine-b" / "home" / ".codevira"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(project))
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )
    monkeypatch.chdir(project)
    # A new machine is a new process: reset the process-lifetime project-root
    # pin (D000118) so the import re-resolves to machine B, not machine A.
    import mcp_server.paths as _paths

    _paths.invalidate_data_dir_cache()
    return project, home / "global.db"


class TestExportSetup:
    def test_export_bundles_memory_and_learning(self, machine_a, tmp_path) -> None:
        out = tmp_path / "setup.tar.gz"
        rc = cmd_export_setup(out=str(out))
        assert rc == 0
        assert out.is_file()

        with tarfile.open(out) as tar:
            names = set(tar.getnames())
        assert "codevira-setup.json" in names
        assert ".codevira/decisions.jsonl" in names
        assert "global/preferences.jsonl" in names
        assert "global/rules.jsonl" in names

    def test_export_without_global_db_still_works(
        self, machine_a, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path",
            lambda: tmp_path / "nonexistent" / "global.db",
        )
        out = tmp_path / "setup.tar.gz"
        rc = cmd_export_setup(out=str(out))
        assert rc == 0
        with tarfile.open(out) as tar:
            names = set(tar.getnames())
        assert ".codevira/decisions.jsonl" in names
        assert "global/preferences.jsonl" not in names

    def test_export_nothing_exits_2(self, tmp_path, monkeypatch) -> None:
        empty = tmp_path / "empty-proj"
        empty.mkdir()
        (empty / ".git").mkdir()
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(empty))
        monkeypatch.setattr(
            "mcp_server.paths.get_global_db_path",
            lambda: tmp_path / "nonexistent" / "global.db",
        )
        monkeypatch.chdir(empty)
        rc = cmd_export_setup(out=str(tmp_path / "x.tar.gz"))
        assert rc == 2
        assert not (tmp_path / "x.tar.gz").exists()


class TestRoundTrip:
    def test_full_machine_transfer(self, machine_a, tmp_path, monkeypatch) -> None:
        out = tmp_path / "setup.tar.gz"
        assert cmd_export_setup(out=str(out)) == 0

        project_b, global_db_b = _switch_to_machine_b(tmp_path, monkeypatch)
        rc = cmd_import_setup(str(out))
        assert rc == 0

        # Project memory restored verbatim.
        restored = project_b / ".codevira" / "decisions.jsonl"
        assert restored.is_file()
        assert json.loads(restored.read_text())["id"] == "D000001"

        # Global learning merged into machine B's global.db.
        conn = sqlite3.connect(global_db_b)
        prefs = conn.execute(
            "SELECT signal, frequency FROM global_preferences"
        ).fetchall()
        rules = conn.execute("SELECT rule_text FROM global_rules").fetchall()
        conn.close()
        assert ("short answers", 4) in prefs
        assert ("always run ruff before commit",) in rules

    def test_import_merges_not_overwrites(
        self, machine_a, tmp_path, monkeypatch
    ) -> None:
        out = tmp_path / "setup.tar.gz"
        assert cmd_export_setup(out=str(out)) == 0

        _, global_db_b = _switch_to_machine_b(tmp_path, monkeypatch)
        _seed_b = global_db_b
        from indexer.global_db import GlobalDB

        db = GlobalDB(_seed_b)
        db.upsert_rule(
            rule_text="machine B's own rule",
            confidence=0.9,
            source_project="other",
        )
        db.close()

        assert cmd_import_setup(str(out)) == 0

        conn = sqlite3.connect(global_db_b)
        rules = {r[0] for r in conn.execute("SELECT rule_text FROM global_rules")}
        conn.close()
        # Both survive: B's pre-existing learning AND A's imported learning.
        assert "machine B's own rule" in rules
        assert "always run ruff before commit" in rules


class TestImportSafety:
    def test_refuses_existing_codevira_without_force(
        self, machine_a, tmp_path, monkeypatch
    ) -> None:
        out = tmp_path / "setup.tar.gz"
        assert cmd_export_setup(out=str(out)) == 0

        project_b, _ = _switch_to_machine_b(tmp_path, monkeypatch)
        existing = project_b / ".codevira"
        existing.mkdir()
        (existing / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

        rc = cmd_import_setup(str(out))
        assert rc == 1  # refused
        assert (existing / "decisions.jsonl").read_text() == "{}\n"  # untouched

    def test_force_backs_up_existing(self, machine_a, tmp_path, monkeypatch) -> None:
        out = tmp_path / "setup.tar.gz"
        assert cmd_export_setup(out=str(out)) == 0

        project_b, _ = _switch_to_machine_b(tmp_path, monkeypatch)
        existing = project_b / ".codevira"
        existing.mkdir()
        (existing / "decisions.jsonl").write_text(
            json.dumps({"id": "B-LOCAL"}) + "\n", encoding="utf-8"
        )

        rc = cmd_import_setup(str(out), force=True)
        assert rc == 0
        backups = list(project_b.glob(".codevira.pre-import-*"))
        assert len(backups) == 1
        assert (
            json.loads((backups[0] / "decisions.jsonl").read_text())["id"] == "B-LOCAL"
        )
        assert (
            json.loads((project_b / ".codevira" / "decisions.jsonl").read_text())["id"]
            == "D000001"
        )

    def test_rejects_path_traversal(self, tmp_path, monkeypatch) -> None:
        project_b, _ = _switch_to_machine_b(tmp_path, monkeypatch)
        evil = tmp_path / "evil.tar.gz"
        import io

        with tarfile.open(evil, "w:gz") as tar:
            manifest = json.dumps({"schema_version": 1}).encode()
            info = tarfile.TarInfo("codevira-setup.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
            payload = b"owned"
            info = tarfile.TarInfo("../../outside.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        rc = cmd_import_setup(str(evil))
        assert rc == 1
        assert not (tmp_path / "outside.txt").exists()

    def test_rejects_non_codevira_archive(self, tmp_path, monkeypatch) -> None:
        project_b, _ = _switch_to_machine_b(tmp_path, monkeypatch)
        random_tar = tmp_path / "random.tar.gz"
        import io

        with tarfile.open(random_tar, "w:gz") as tar:
            data = b"hello"
            info = tarfile.TarInfo("readme.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        rc = cmd_import_setup(str(random_tar))
        assert rc == 1

    def test_missing_archive_errors(self, tmp_path, monkeypatch) -> None:
        _switch_to_machine_b(tmp_path, monkeypatch)
        rc = cmd_import_setup(str(tmp_path / "nope.tar.gz"))
        assert rc == 1
