"""
Tests for mcp_server/global_sync.py — cross-project intelligence sync.

Creates both GlobalDB and SQLiteGraph in tmp_path. Patches path helpers
in mcp_server.paths (where they are defined) since global_sync.py uses
lazy imports from that module inside each function.
"""
from __future__ import annotations

import yaml
from unittest.mock import patch

from indexer.global_db import GlobalDB
from indexer.sqlite_graph import SQLiteGraph
from mcp_server.global_sync import (
    import_global_to_project,
    export_project_to_global,
    get_global_stats,
    _get_project_language,
)


def _setup_env(tmp_path):
    """Create isolated project + global database files and return paths."""
    project_root = tmp_path / "my-project"
    project_root.mkdir()

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph").mkdir(parents=True)

    global_db_path = tmp_path / "global.db"

    # Write a config.yaml so _get_project_language() works
    config = {"project": {"name": "test", "language": "python"}}
    (data_dir / "config.yaml").write_text(yaml.dump(config))

    return project_root, data_dir, global_db_path


def _apply_patches(project_root, data_dir, global_db_path):
    """Return a combined context manager patching the three path helpers at their source."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("mcp_server.paths.get_global_db_path", return_value=global_db_path))
    stack.enter_context(patch("mcp_server.paths.get_data_dir", return_value=data_dir))
    stack.enter_context(patch("mcp_server.paths.get_project_root", return_value=project_root))
    return stack


# =====================================================================
# import_global_to_project
# =====================================================================

class TestImportGlobalToProject:
    def test_import_prefs_above_threshold(self, tmp_path):
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_preference("naming", "snake_case", None, "/other", frequency=3)
        gdb.upsert_preference("naming", "camelCase", None, "/other", frequency=1)  # below threshold
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = import_global_to_project()

        assert stats["preferences_imported"] == 1

    def test_import_rules_above_confidence(self, tmp_path):
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_rule("Always use type hints", 0.9, "/other", category="patterns", language="python")
        gdb.upsert_rule("Low confidence rule", 0.3, "/other", category="patterns", language="python")
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = import_global_to_project()

        assert stats["rules_imported"] == 1

    def test_confidence_decay_on_import(self, tmp_path):
        """Imported rules get 0.8x confidence decay: 0.8 * 0.8 = 0.64."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_rule("Type hint rule", 0.8, "/other", category="patterns", language="python")
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            import_global_to_project()

        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        rules = pdb.get_learned_rules(min_confidence=0.0)
        pdb.close()
        matching = [r for r in rules if r["rule_text"] == "Type hint rule"]
        assert len(matching) == 1
        assert abs(matching[0]["confidence"] - 0.64) < 0.01

    def test_import_skips_existing_preference(self, tmp_path):
        """Re-importing the same preference does not duplicate it."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_preference("naming", "snake_case", None, "/other", frequency=5)
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.record_preference("naming", "snake_case")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = import_global_to_project()

        assert stats["preferences_imported"] == 0

    def test_import_skips_existing_rule(self, tmp_path):
        """Re-importing the same rule does not duplicate it."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_rule("Use type hints", 0.9, "/other", category="patterns", language="python")
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.add_learned_rule("Use type hints", 0.7, ["s1"], category="patterns")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = import_global_to_project()

        assert stats["rules_imported"] == 0

    def test_no_global_db_returns_empty_stats(self, tmp_path):
        """When global.db doesn't exist, import returns zeros."""
        project_root, data_dir, _ = _setup_env(tmp_path)
        nonexistent = tmp_path / "no-such-global.db"

        with patch("mcp_server.paths.get_global_db_path", return_value=nonexistent), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_project_root", return_value=project_root):
            stats = import_global_to_project()

        assert stats == {"preferences_imported": 0, "rules_imported": 0}

    def test_no_project_db_returns_empty_stats(self, tmp_path):
        """When project graph.db doesn't exist, import returns zeros."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.close()
        import shutil
        shutil.rmtree(data_dir / "graph")

        with patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir), \
             patch("mcp_server.paths.get_project_root", return_value=project_root):
            stats = import_global_to_project()

        assert stats == {"preferences_imported": 0, "rules_imported": 0}

    def test_no_qualifying_rules_imports_nothing(self, tmp_path):
        """Rules all below confidence threshold => nothing imported."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        gdb = GlobalDB(global_db_path)
        gdb.upsert_rule("Weak rule", 0.3, "/other", language="python")
        gdb.upsert_rule("Weak rule 2", 0.5, "/other", language="python")
        gdb.close()
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = import_global_to_project()

        assert stats["rules_imported"] == 0


# =====================================================================
# export_project_to_global
# =====================================================================

class TestExportProjectToGlobal:
    def test_export_prefs_above_threshold(self, tmp_path):
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        # Record preference 3x so frequency = 3 (>= 2 threshold)
        pdb.record_preference("naming", "snake_case")
        pdb.record_preference("naming", "snake_case")
        pdb.record_preference("naming", "snake_case")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = export_project_to_global()

        assert stats["preferences_exported"] == 1

    def test_export_rules_above_confidence(self, tmp_path):
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.add_learned_rule("Good rule", 0.7, ["s1"], category="patterns")
        pdb.add_learned_rule("Bad rule", 0.3, ["s1"], category="patterns")  # below 0.5
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = export_project_to_global()

        assert stats["rules_exported"] == 1

    def test_no_project_db_returns_empty_stats(self, tmp_path):
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        import shutil
        shutil.rmtree(data_dir / "graph")

        with _apply_patches(project_root, data_dir, global_db_path):
            stats = export_project_to_global()

        assert stats == {"preferences_exported": 0, "rules_exported": 0}

    def test_export_then_import_roundtrip(self, tmp_path):
        """Export from project A, import into project B, verify data arrives."""
        project_root, data_dir, global_db_path = _setup_env(tmp_path)
        pdb = SQLiteGraph(data_dir / "graph" / "graph.db")
        pdb.add_learned_rule("Always lint before commit", 0.85, ["s1"], category="patterns")
        pdb.close()

        with _apply_patches(project_root, data_dir, global_db_path):
            export_stats = export_project_to_global()
        assert export_stats["rules_exported"] == 1

        # Project B: fresh project DB
        data_dir_b = tmp_path / "data-b"
        data_dir_b.mkdir()
        (data_dir_b / "graph").mkdir(parents=True)
        config_b = {"project": {"name": "project-b", "language": "python"}}
        (data_dir_b / "config.yaml").write_text(yaml.dump(config_b))
        project_root_b = tmp_path / "project-b"
        project_root_b.mkdir()
        pdb_b = SQLiteGraph(data_dir_b / "graph" / "graph.db")
        pdb_b.close()

        with patch("mcp_server.paths.get_global_db_path", return_value=global_db_path), \
             patch("mcp_server.paths.get_data_dir", return_value=data_dir_b), \
             patch("mcp_server.paths.get_project_root", return_value=project_root_b):
            import_stats = import_global_to_project()

        assert import_stats["rules_imported"] == 1

        pdb_b = SQLiteGraph(data_dir_b / "graph" / "graph.db")
        rules = pdb_b.get_learned_rules(min_confidence=0.0)
        pdb_b.close()
        matching = [r for r in rules if r["rule_text"] == "Always lint before commit"]
        assert len(matching) == 1
        # 0.85 * 0.8 = 0.68
        assert abs(matching[0]["confidence"] - 0.68) < 0.01


# =====================================================================
# get_global_stats
# =====================================================================

class TestGetGlobalStats:
    def test_returns_stats_when_db_exists(self, tmp_path):
        global_db_path = tmp_path / "global.db"
        gdb = GlobalDB(global_db_path)
        gdb.register_project("/p1", "proj1", "python")
        gdb.register_project("/p2", "proj2", "go")
        gdb.upsert_preference("naming", "snake", None, "/p1")
        gdb.upsert_rule("Use type hints", 0.8, "/p1")
        gdb.close()

        with patch("mcp_server.paths.get_global_db_path", return_value=global_db_path):
            stats = get_global_stats()

        assert stats is not None
        assert stats["project_count"] == 2
        assert stats["total_preferences"] == 1
        assert stats["total_rules"] == 1

    def test_returns_none_when_no_global_db(self, tmp_path):
        nonexistent = tmp_path / "no-such.db"
        with patch("mcp_server.paths.get_global_db_path", return_value=nonexistent):
            stats = get_global_stats()
        assert stats is None


# =====================================================================
# _get_project_language
# =====================================================================

class TestGetProjectLanguage:
    def test_reads_language_from_config(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config = {"project": {"name": "test", "language": "go"}}
        (data_dir / "config.yaml").write_text(yaml.dump(config))

        with patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            lang = _get_project_language()
        assert lang == "go"

    def test_missing_config_returns_none(self, tmp_path):
        data_dir = tmp_path / "empty-data"
        data_dir.mkdir()

        with patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            lang = _get_project_language()
        assert lang is None

    def test_config_without_language_key_returns_none(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config = {"project": {"name": "test"}}
        (data_dir / "config.yaml").write_text(yaml.dump(config))

        with patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            lang = _get_project_language()
        assert lang is None

    def test_corrupt_yaml_returns_none(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("{{invalid yaml: [")

        with patch("mcp_server.paths.get_data_dir", return_value=data_dir):
            lang = _get_project_language()
        assert lang is None
