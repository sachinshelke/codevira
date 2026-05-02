"""Tests for indexer.fix_history — minimal Week-1 baseline.

Week 2 expands with git log scanning; this test covers the manual-record
path + lookup + the is_revert heuristic that Hero 2 will use.
"""
from __future__ import annotations

import pytest

from indexer.fix_history import (
    FixRecord,
    is_revert,
    lookup,
    record_fix,
    reset,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Use tmp_path as a fake project; redirect global home into tmp."""
    proj = tmp_path / "myproject"
    proj.mkdir()
    fake_home = tmp_path / "global"
    fake_home.mkdir()
    monkeypatch.setattr(
        "mcp_server.paths.get_global_home", lambda: fake_home,
    )
    yield proj
    reset(proj)


class TestRecordAndLookup:
    def test_lookup_empty_when_no_records(self, project):
        assert lookup(project, "src/foo.py") == []

    def test_record_then_lookup(self, project):
        rid = record_fix(
            project,
            file_path="src/foo.py",
            line_start=10,
            line_end=15,
            description="connection retry was infinite-looping",
            source="manual",
        )
        assert rid > 0
        records = lookup(project, "src/foo.py")
        assert len(records) == 1
        assert records[0]["description"].startswith("connection")
        assert records[0]["source"] == "manual"

    def test_multiple_fixes_newest_first(self, project):
        record_fix(project, "src/x.py", 1, 1, "first", source="manual")
        record_fix(project, "src/x.py", 2, 2, "second", source="manual")
        records = lookup(project, "src/x.py")
        assert len(records) == 2
        assert records[0]["description"] == "second"  # newest first

    def test_lookup_unrelated_file_returns_empty(self, project):
        record_fix(project, "src/a.py", 1, 1, "fix", source="manual")
        assert lookup(project, "src/b.py") == []


class TestRecordValidation:
    def test_invalid_source_raises(self, project):
        with pytest.raises(ValueError, match="manual.*git"):
            record_fix(project, "f.py", 1, 1, "x", source="bogus")

    def test_git_source_requires_commit_sha(self, project):
        with pytest.raises(ValueError, match="commit_sha"):
            record_fix(project, "f.py", 1, 1, "x", source="git")

    def test_line_end_before_start_raises(self, project):
        with pytest.raises(ValueError, match="line_end"):
            record_fix(project, "f.py", 10, 5, "x", source="manual")


class TestIsRevertHeuristic:
    def test_no_diff_means_not_revert(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="x", source="manual",
        )
        assert is_revert("", fix) is False

    def test_unrelated_diff_not_revert(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="x", source="manual",
        )
        # Diff in unrelated line range — heuristic says not a revert.
        diff = "@@ -100,5 +100,5 @@\n-old\n+new\n"
        assert is_revert(diff, fix) is False

    def test_diff_in_fix_range_with_deletion_flagged_as_revert(self):
        fix = FixRecord(
            id=1, file_path="f.py", line_start=10, line_end=15,
            description="x", source="manual",
        )
        diff = "@@ -10,3 +10,1 @@\n-fixed_line()\n+old_buggy_line()\n"
        assert is_revert(diff, fix) is True

    def test_works_with_dict_fix(self):
        fix_dict = {
            "id": 1, "file_path": "f.py", "line_start": 10,
            "line_end": 15, "description": "x", "source": "manual",
        }
        diff = "@@ -10,3 +10,1 @@\n-fixed\n+broken\n"
        assert is_revert(diff, fix_dict) is True
