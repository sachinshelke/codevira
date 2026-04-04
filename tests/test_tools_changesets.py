"""
Tests for mcp_server/tools/changesets.py — multi-file changeset lifecycle.

Uses the `project_env` fixture from conftest.py which provides
(project_root, data_dir, db).
"""
from __future__ import annotations

import yaml
from pathlib import Path

from mcp_server.tools.changesets import (
    start_changeset,
    update_changeset_progress,
    complete_changeset,
    get_changeset,
    list_open_changesets,
    update_node_after_change,
)
from mcp_server.tools import graph


# =====================================================================
# start_changeset
# =====================================================================

class TestStartChangeset:
    def test_creates_yaml_file(self, project_env):
        _project, data_dir, _db = project_env
        result = start_changeset("fix-auth", "Fix auth bug", ["src/auth.py", "src/middleware.py"])
        assert result["success"] is True
        assert result["changeset_id"] == "fix-auth"
        yaml_path = data_dir / "graph" / "changesets" / "fix-auth.yaml"
        assert yaml_path.exists()

    def test_yaml_content_is_correct(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("feat-x", "Add feature X", ["a.py", "b.py"], trigger="large_change")
        yaml_path = data_dir / "graph" / "changesets" / "feat-x.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        assert data["id"] == "feat-x"
        assert data["status"] == "in_progress"
        assert data["trigger"] == "large_change"
        assert data["description"] == "Add feature X"
        assert data["files_pending"] == ["a.py", "b.py"]
        assert data["files_modified"] == []
        assert data["blocker"] is None
        assert data["decisions"] == []

    def test_returns_tracked_files(self, project_env):
        _project, _data_dir, _db = project_env
        result = start_changeset("cs-1", "desc", ["x.py", "y.py", "z.py"])
        assert result["tracking"] == ["x.py", "y.py", "z.py"]

    def test_default_trigger_is_medium_change(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("cs-def", "desc", ["a.py"])
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "cs-def.yaml").read_text())
        assert data["trigger"] == "medium_change"

    def test_duplicate_changeset_returns_error(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("dup-cs", "first", ["a.py"])
        result = start_changeset("dup-cs", "second", ["b.py"])
        assert result["success"] is False
        assert "already exists" in result["message"]

    def test_creates_changesets_dir_if_missing(self, project_env):
        """Even if the changesets directory doesn't exist, start_changeset creates it."""
        _project, data_dir, _db = project_env
        changesets_dir = data_dir / "graph" / "changesets"
        # Remove the pre-existing directory
        import shutil
        shutil.rmtree(changesets_dir)
        assert not changesets_dir.exists()
        result = start_changeset("new-cs", "desc", ["f.py"])
        assert result["success"] is True
        assert changesets_dir.exists()


# =====================================================================
# update_changeset_progress
# =====================================================================

class TestUpdateChangesetProgress:
    def test_moves_file_from_pending_to_modified(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("up-1", "desc", ["a.py", "b.py"])
        result = update_changeset_progress("up-1", "a.py")
        assert result["success"] is True
        assert result["files_done"] == 1
        assert result["files_remaining"] == 1
        # Check YAML on disk
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "up-1.yaml").read_text())
        assert "a.py" in data["files_modified"]
        assert "a.py" not in data["files_pending"]

    def test_non_existent_changeset_returns_error(self, project_env):
        _project, _data_dir, _db = project_env
        result = update_changeset_progress("ghost-cs", "a.py")
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_duplicate_file_done_is_idempotent(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("idem-1", "desc", ["a.py", "b.py"])
        update_changeset_progress("idem-1", "a.py")
        update_changeset_progress("idem-1", "a.py")  # second call
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "idem-1.yaml").read_text())
        assert data["files_modified"].count("a.py") == 1
        assert "a.py" not in data["files_pending"]

    def test_file_not_in_pending_still_added_to_modified(self, project_env):
        """A file not originally in pending can still be marked as modified."""
        _project, data_dir, _db = project_env
        start_changeset("extra-1", "desc", ["a.py"])
        result = update_changeset_progress("extra-1", "extra.py")
        assert result["success"] is True
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "extra-1.yaml").read_text())
        assert "extra.py" in data["files_modified"]

    def test_blocker_is_tracked(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("blk-1", "desc", ["a.py", "b.py"])
        result = update_changeset_progress("blk-1", "a.py", blocker="Waiting on API spec")
        assert result["blocker"] == "Waiting on API spec"
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "blk-1.yaml").read_text())
        assert data["blocker"] == "Waiting on API spec"

    def test_blocker_none_does_not_overwrite_existing(self, project_env):
        """Passing blocker=None (the default) should NOT clear an existing blocker."""
        _project, data_dir, _db = project_env
        start_changeset("blk-2", "desc", ["a.py", "b.py"])
        update_changeset_progress("blk-2", "a.py", blocker="Blocked on CI")
        # Next call with no blocker arg should leave the existing one intact
        update_changeset_progress("blk-2", "b.py")
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "blk-2.yaml").read_text())
        assert data["blocker"] == "Blocked on CI"

    def test_all_files_done_shows_zero_remaining(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("all-done", "desc", ["a.py", "b.py"])
        update_changeset_progress("all-done", "a.py")
        result = update_changeset_progress("all-done", "b.py")
        assert result["files_remaining"] == 0
        assert result["files_done"] == 2


# =====================================================================
# complete_changeset
# =====================================================================

class TestCompleteChangeset:
    def test_complete_with_all_files_done(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("comp-1", "desc", ["a.py"])
        update_changeset_progress("comp-1", "a.py")
        result = complete_changeset("comp-1", ["Used REST instead of gRPC"])
        assert result["success"] is True
        assert result["decisions_recorded"] == 1
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "comp-1.yaml").read_text())
        assert data["status"] == "complete"
        assert data["decisions"] == ["Used REST instead of gRPC"]
        assert data["blocker"] is None
        assert "completed" in data

    def test_complete_with_files_pending_returns_error(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("comp-fail", "desc", ["a.py", "b.py"])
        update_changeset_progress("comp-fail", "a.py")
        result = complete_changeset("comp-fail", ["decision"])
        assert result["success"] is False
        assert "1 files still pending" in result["message"]
        assert "b.py" in result["message"]
        assert "hint" in result

    def test_complete_non_existent_changeset(self, project_env):
        _project, _data_dir, _db = project_env
        result = complete_changeset("no-such-cs", ["decision"])
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_complete_with_empty_decisions(self, project_env):
        _project, data_dir, _db = project_env
        start_changeset("comp-empty", "desc", ["a.py"])
        update_changeset_progress("comp-empty", "a.py")
        result = complete_changeset("comp-empty", [])
        assert result["success"] is True
        assert result["decisions_recorded"] == 0

    def test_complete_clears_blocker(self, project_env):
        """Completing a changeset sets blocker to None even if one was previously set."""
        _project, data_dir, _db = project_env
        start_changeset("blk-comp", "desc", ["a.py"])
        update_changeset_progress("blk-comp", "a.py", blocker="Was blocked")
        complete_changeset("blk-comp", ["done"])
        data = yaml.safe_load((data_dir / "graph" / "changesets" / "blk-comp.yaml").read_text())
        assert data["blocker"] is None


# =====================================================================
# get_changeset
# =====================================================================

class TestGetChangeset:
    def test_get_existing_changeset(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("get-1", "A changeset", ["f.py"])
        result = get_changeset("get-1")
        assert result["found"] is True
        assert result["changeset"]["id"] == "get-1"
        assert result["changeset"]["description"] == "A changeset"

    def test_get_non_existent_changeset(self, project_env):
        _project, _data_dir, _db = project_env
        result = get_changeset("nope")
        assert result["found"] is False
        assert "not found" in result["message"]

    def test_get_reflects_progress_updates(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("get-prog", "desc", ["a.py", "b.py"])
        update_changeset_progress("get-prog", "a.py")
        result = get_changeset("get-prog")
        cs = result["changeset"]
        assert "a.py" in cs["files_modified"]
        assert "b.py" in cs["files_pending"]

    def test_get_reflects_completion(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("get-comp", "desc", ["a.py"])
        update_changeset_progress("get-comp", "a.py")
        complete_changeset("get-comp", ["dec1", "dec2"])
        result = get_changeset("get-comp")
        assert result["changeset"]["status"] == "complete"
        assert result["changeset"]["decisions"] == ["dec1", "dec2"]


# =====================================================================
# list_open_changesets
# =====================================================================

class TestListOpenChangesets:
    def test_list_when_no_changesets_exist(self, project_env):
        _project, _data_dir, _db = project_env
        result = list_open_changesets()
        assert result["count"] == 0
        assert result["open_changesets"] == []
        assert result["warning"] is None

    def test_list_returns_in_progress_changesets(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("open-1", "first", ["a.py"])
        start_changeset("open-2", "second", ["b.py"])
        result = list_open_changesets()
        assert result["count"] == 2
        ids = {cs["id"] for cs in result["open_changesets"]}
        assert ids == {"open-1", "open-2"}
        assert result["warning"] is not None  # has open changesets

    def test_completed_changeset_not_in_open_list(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("done-1", "will complete", ["a.py"])
        update_changeset_progress("done-1", "a.py")
        complete_changeset("done-1", ["done"])
        start_changeset("still-open", "still going", ["b.py"])
        result = list_open_changesets()
        assert result["count"] == 1
        assert result["open_changesets"][0]["id"] == "still-open"

    def test_list_includes_blocker_info(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("blk-list", "desc", ["a.py", "b.py"])
        update_changeset_progress("blk-list", "a.py", blocker="API spec pending")
        result = list_open_changesets()
        assert result["count"] == 1
        assert result["open_changesets"][0]["blocker"] == "API spec pending"

    def test_list_includes_pending_files(self, project_env):
        _project, _data_dir, _db = project_env
        start_changeset("pend-list", "desc", ["a.py", "b.py", "c.py"])
        update_changeset_progress("pend-list", "a.py")
        result = list_open_changesets()
        pending = result["open_changesets"][0]["files_pending"]
        assert set(pending) == {"b.py", "c.py"}

    def test_all_completed_returns_empty(self, project_env):
        """After completing all changesets, list_open returns empty."""
        _project, _data_dir, _db = project_env
        start_changeset("all-1", "desc", ["a.py"])
        update_changeset_progress("all-1", "a.py")
        complete_changeset("all-1", ["d1"])
        result = list_open_changesets()
        assert result["count"] == 0
        assert result["warning"] is None


# =====================================================================
# Ported from test_stability.py: update_node_after_change
# =====================================================================

class TestUpdateNodeAfterChange:
    def test_update_node_after_change_updates_sqlite_graph(self, project_env):
        """Ported from test_stability.py: update_node_after_change should persist
        new_rules, new_connections, key_functions, and do_not_revert into the
        SQLite graph node."""
        _project, _data_dir, _db = project_env

        graph.add_node(
            file_path="src/example.py",
            role="Example module",
            layer="service",
            rules=["Keep responses stable"],
        )

        result = update_node_after_change(
            "src/example.py",
            {
                "new_rules": ["Preserve legacy payload shape"],
                "new_connections": [{"target": "src/other.py", "edge": "uses", "via": "import"}],
                "do_not_revert": True,
                "key_functions": ["run"],
            },
        )
        node = graph.get_node("src/example.py")["node"]

        assert result["success"] is True
        assert "Preserve legacy payload shape" in node["rules"]
        assert any(dep["target"] == "src/other.py" for dep in node["dependencies"])
        assert "run" in node["key_functions"]
        assert bool(node["do_not_revert"]) is True


# =====================================================================
# list_open_changesets — corrupt YAML (lines 169-174)
# =====================================================================

class TestListOpenChangesetsCorruptYaml:
    def test_corrupt_yaml_is_skipped_and_valid_returned(self, project_env):
        """list_open_changesets skips corrupt YAML files and still returns valid ones."""
        from unittest.mock import patch
        _project, data_dir, _db = project_env
        changesets_dir = data_dir / "graph" / "changesets"
        changesets_dir.mkdir(parents=True, exist_ok=True)

        # Write a valid in-progress changeset
        valid_cs = changesets_dir / "cs-valid.yaml"
        valid_cs.write_text(
            "id: cs-valid\ndescription: Valid\nstatus: in_progress\n"
            "files_pending: []\ncreated: '2026-01-01'\n"
        )

        # Write a corrupt YAML file
        corrupt_cs = changesets_dir / "cs-corrupt.yaml"
        corrupt_cs.write_text("{{corrupt: yaml: [missing\n")

        result = list_open_changesets()

        assert isinstance(result, dict)
        assert "open_changesets" in result
        # Corrupt file is skipped; valid changeset is returned
        ids = [cs["id"] for cs in result["open_changesets"]]
        assert "cs-valid" in ids
        assert "cs-corrupt" not in ids

    def test_all_corrupt_returns_empty_list(self, project_env):
        """When all changeset files are corrupt, returns empty open_changesets list."""
        _project, data_dir, _db = project_env
        changesets_dir = data_dir / "graph" / "changesets"
        changesets_dir.mkdir(parents=True, exist_ok=True)

        (changesets_dir / "bad1.yaml").write_text("{{bad yaml\n")
        (changesets_dir / "bad2.yaml").write_text(": : :\n")

        result = list_open_changesets()
        assert result["count"] == 0
        assert result["open_changesets"] == []


# =====================================================================
# update_node_after_change — error and last_changed_by branches
# (lines 208-211, 218-221)
# =====================================================================

class TestUpdateNodeAfterChangeBranches:
    def test_error_in_update_node_returns_failure(self, project_env):
        """update_node_after_change returns success=False when update_node returns an error."""
        from unittest.mock import patch
        _project, _data_dir, _db = project_env

        with patch("mcp_server.tools.graph.update_node",
                   return_value={"error": "node not found"}):
            result = update_node_after_change("src/missing.py", {"stability": "high"})

        assert result["success"] is False
        assert "node not found" in result["message"]

    def test_last_changed_by_adds_note_to_response(self, project_env):
        """When last_changed_by is in changes, a note is added to the response."""
        from unittest.mock import patch
        _project, _data_dir, _db = project_env

        with patch("mcp_server.tools.graph.update_node",
                   return_value={"updated": True}):
            result = update_node_after_change(
                "src/api.py",
                {"last_changed_by": "agent-001", "stability": "high"},
            )

        assert result["success"] is True
        assert "note" in result
        assert "last_changed_by" in result["note"] or "not persisted" in result["note"]

    def test_no_last_changed_by_no_note(self, project_env):
        """Without last_changed_by in changes, no note key is added."""
        from unittest.mock import patch
        _project, _data_dir, _db = project_env

        with patch("mcp_server.tools.graph.update_node",
                   return_value={"updated": True}):
            result = update_node_after_change("src/api.py", {"stability": "high"})

        assert result["success"] is True
        assert "note" not in result
