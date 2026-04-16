"""
Tests for mcp_server/tools/playbook.py — curated rule playbooks by task type.

Standalone replacement for the playbook portion of test_playbook_and_auto_init.py.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mcp_server.tools.playbook import get_playbook, PLAYBOOKS


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _create_all_rule_files(rules_dir):
    """Create every rule file referenced in PLAYBOOKS so all lookups succeed."""
    rules_dir.mkdir(exist_ok=True)
    all_files = set()
    for rule_list in PLAYBOOKS.values():
        all_files.update(rule_list)
    for f in all_files:
        (rules_dir / f).write_text(f"# {f}\nContent for {f}.")


def _patch_data_dir(tmp_path):
    """Patch get_package_data_dir to return tmp_path."""
    return patch("mcp_server.tools.playbook.get_package_data_dir", return_value=tmp_path)


# ---------------------------------------------------------------
# Valid task types return rules
# ---------------------------------------------------------------

class TestValidTaskTypes:
    def test_add_tool(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_tool")

        assert result["found"] is True
        assert result["task_type"] == "add_tool"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "coding-standards.md"
        assert "coding-standards.md" in result["rules"][0]["content"]

    def test_add_service(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_service")

        assert result["found"] is True
        assert result["task_type"] == "add_service"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "coding-standards.md"
        assert result["rules"][1]["file"] == "resilience-observability.md"

    def test_add_schema(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_schema")

        assert result["found"] is True
        assert result["task_type"] == "add_schema"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "coding-standards.md"
        assert result["rules"][1]["file"] == "persistence.md"

    def test_debug_pipeline(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("debug_pipeline")

        assert result["found"] is True
        assert result["task_type"] == "debug_pipeline"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "resilience-observability.md"
        assert result["rules"][1]["file"] == "coding-standards.md"

    def test_commit(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("commit")

        assert result["found"] is True
        assert result["task_type"] == "commit"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "git_commits.md"
        assert result["rules"][1]["file"] == "git-cicd-governance.md"

    def test_write_test(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("write_test")

        assert result["found"] is True
        assert result["task_type"] == "write_test"
        assert len(result["rules"]) == 2
        assert result["rules"][0]["file"] == "testing-standards.md"
        assert result["rules"][1]["file"] == "smoke-testing.md"


# ---------------------------------------------------------------
# Unknown task type
# ---------------------------------------------------------------

class TestUnknownTaskType:
    def test_returns_not_found(self, tmp_path):
        with _patch_data_dir(tmp_path):
            result = get_playbook("nonexistent_task")

        assert result["found"] is False
        assert result["task_type"] == "nonexistent_task"
        assert "available_task_types" in result
        assert sorted(PLAYBOOKS.keys()) == result["available_task_types"]

    def test_empty_string_is_unknown(self, tmp_path):
        with _patch_data_dir(tmp_path):
            result = get_playbook("")

        assert result["found"] is False


# ---------------------------------------------------------------
# Missing rule file on disk
# ---------------------------------------------------------------

class TestMissingRuleFile:
    def test_missing_file_returns_placeholder_error_text(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Only create one of the two expected files for add_tool
        (rules_dir / "coding-standards.md").write_text("# Coding Standards")
        # testing-standards.md is intentionally missing

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_tool")

        assert result["found"] is True
        missing = [r for r in result["rules"] if "File not found" in r["content"]]
        assert len(missing) == 1
        assert missing[0]["file"] == "testing-standards.md"

    def test_all_files_missing_returns_all_placeholders(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Create NO rule files

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_tool")

        assert result["found"] is True
        assert all("File not found" in r["content"] for r in result["rules"])


# ---------------------------------------------------------------
# Case insensitivity + whitespace
# ---------------------------------------------------------------

class TestCaseInsensitivityAndWhitespace:
    def test_uppercase_with_whitespace(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("  ADD_TOOL  ")

        assert result["found"] is True
        assert result["task_type"] == "add_tool"

    def test_mixed_case(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("Add_Service")

        assert result["found"] is True
        assert result["task_type"] == "add_service"

    def test_trailing_whitespace_only(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("commit   ")

        assert result["found"] is True
        assert result["task_type"] == "commit"


# ---------------------------------------------------------------
# Correct number of rules per task type
# ---------------------------------------------------------------

class TestRuleCountsPerTaskType:
    def test_all_six_task_types_return_correct_rule_counts(self, tmp_path):
        """All 6 task types should map to the expected number of rule files."""
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            for task_type, expected_files in PLAYBOOKS.items():
                result = get_playbook(task_type)
                assert result["found"] is True, f"Failed for {task_type}"
                assert len(result["rules"]) == len(expected_files), (
                    f"Wrong count for {task_type}: "
                    f"expected {len(expected_files)}, got {len(result['rules'])}"
                )

    def test_exactly_six_task_types_defined(self):
        """PLAYBOOKS should contain exactly 6 task types."""
        expected = {"add_tool", "add_service", "add_schema",
                    "debug_pipeline", "commit", "write_test"}
        assert set(PLAYBOOKS.keys()) == expected


# ---------------------------------------------------------------
# Note and hint fields
# ---------------------------------------------------------------

class TestNoteAndHintFields:
    def test_note_field_present_on_valid_task(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("add_service")

        assert "note" in result
        assert "add_service" in result["note"]
        assert "rule files" in result["note"]

    def test_hint_field_present_on_unknown_task(self, tmp_path):
        with _patch_data_dir(tmp_path):
            result = get_playbook("unknown_type")

        assert "hint" in result
        assert "get_node" in result["hint"]

    def test_note_contains_rule_count(self, tmp_path):
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("commit")

        assert "2 rule files" in result["note"]

    def test_valid_task_has_no_hint(self, tmp_path):
        """Valid tasks should not have a 'hint' field (that's only for unknown types)."""
        rules_dir = tmp_path / "rules"
        _create_all_rule_files(rules_dir)

        with _patch_data_dir(tmp_path):
            result = get_playbook("write_test")

        assert "hint" not in result

    def test_unknown_task_has_no_note(self, tmp_path):
        """Unknown tasks should not have a 'note' field (that's only for valid types)."""
        with _patch_data_dir(tmp_path):
            result = get_playbook("bogus")

        assert "note" not in result
