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
)


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
