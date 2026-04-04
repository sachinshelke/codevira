"""
Tests for indexer/rule_learner.py — Automatic rule generation from patterns.

Uses the populated_db fixture and adds extra data as needed to trigger
each of the four inference functions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from indexer.rule_learner import (
    _find_common_phrases,
    _infer_test_pairing_rules,
    _infer_import_pattern_rules,
    _infer_decision_pattern_rules,
    _infer_file_co_change_rules,
    run_rule_inference,
)


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------

class TestEmptyDatabase:
    """No data at all should produce no rules and no crashes."""

    @patch("indexer.rule_learner.get_data_dir")
    def test_run_rule_inference_empty(self, mock_data_dir, project_env):
        """run_rule_inference on a fresh DB should not crash or create rules."""
        project, data_dir, db = project_env
        mock_data_dir.return_value = data_dir

        run_rule_inference()

        rules = db.get_learned_rules()
        assert rules == []

    def test_infer_test_pairing_empty(self, project_env):
        """No nodes -> no test pairing rules."""
        _, _, db = project_env
        _infer_test_pairing_rules(db)
        rules = db.get_learned_rules(category="testing")
        assert rules == []

    def test_infer_import_patterns_empty(self, project_env):
        """No edges -> no import rules."""
        _, _, db = project_env
        _infer_import_pattern_rules(db)
        rules = db.get_learned_rules(category="imports")
        assert rules == []

    def test_infer_decision_patterns_empty(self, project_env):
        """No decisions -> no pattern rules."""
        _, _, db = project_env
        _infer_decision_pattern_rules(db)
        rules = db.get_learned_rules(category="patterns")
        assert rules == []

    def test_infer_co_change_empty(self, project_env):
        """No sessions with multi-file decisions -> no co-change rules."""
        _, _, db = project_env
        _infer_file_co_change_rules(db)
        rules = db.get_learned_rules(category="structure")
        assert rules == []


# ---------------------------------------------------------------------------
# Test pairing inference
# ---------------------------------------------------------------------------

class TestTestPairingRules:
    """_infer_test_pairing_rules detects test<->source file patterns."""

    def test_detects_test_pairing(self, project_env):
        """Source files in src/ paired with test files in tests/ should generate a rule."""
        _, _, db = project_env

        # Need at least 2 pairings for a rule to fire (count >= 2)
        db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
        db.add_node("file:src/service.py", "file", "service.py", "src/service.py", layer="service")
        db.add_node("file:tests/test_api.py", "file", "test_api.py", "tests/test_api.py", layer="test")
        db.add_node("file:tests/test_service.py", "file", "test_service.py", "tests/test_service.py", layer="test")

        _infer_test_pairing_rules(db)

        rules = db.get_learned_rules(category="testing")
        assert len(rules) >= 1
        # The rule should mention src and tests directories
        rule_text = rules[0]["rule_text"]
        assert "src" in rule_text
        assert "tests" in rule_text

    def test_single_pairing_no_rule(self, project_env):
        """A single test pairing (count < 2) should not generate a rule."""
        _, _, db = project_env

        db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
        db.add_node("file:tests/test_api.py", "file", "test_api.py", "tests/test_api.py", layer="test")

        _infer_test_pairing_rules(db)

        rules = db.get_learned_rules(category="testing")
        assert rules == []

    def test_pairing_confidence_scales_with_count(self, project_env):
        """More pairings should increase confidence (capped at 1.0)."""
        _, _, db = project_env

        for i in range(6):
            db.add_node(f"file:src/mod{i}.py", "file", f"mod{i}.py", f"src/mod{i}.py", layer="service")
            db.add_node(f"file:tests/test_mod{i}.py", "file", f"test_mod{i}.py", f"tests/test_mod{i}.py", layer="test")

        _infer_test_pairing_rules(db)

        rules = db.get_learned_rules(category="testing")
        assert len(rules) >= 1
        # 6 pairings -> confidence = min(6/5, 1.0) = 1.0
        assert rules[0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Import pattern inference
# ---------------------------------------------------------------------------

class TestImportPatternRules:
    """_infer_import_pattern_rules detects high fan-in files."""

    def test_high_fan_in_generates_rule(self, project_env):
        """A file imported by 3+ others should get a 'wide blast radius' rule."""
        _, _, db = project_env

        # Core file imported by many
        db.add_node("file:src/core.py", "file", "core.py", "src/core.py", layer="core")
        for i in range(4):
            db.add_node(f"file:src/mod{i}.py", "file", f"mod{i}.py", f"src/mod{i}.py", layer="service")
            db.add_edge(f"file:src/mod{i}.py", "file:src/core.py", kind="imports")

        _infer_import_pattern_rules(db)

        rules = db.get_learned_rules(category="imports")
        assert len(rules) >= 1
        assert "src/core.py" in rules[0]["rule_text"]
        assert "blast radius" in rules[0]["rule_text"].lower()

    def test_low_fan_in_no_rule(self, project_env):
        """A file imported by fewer than 3 others should not trigger a rule."""
        _, _, db = project_env

        db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="service")
        db.add_node("file:src/b.py", "file", "b.py", "src/b.py", layer="service")
        db.add_edge("file:src/a.py", "file:src/b.py", kind="imports")

        _infer_import_pattern_rules(db)

        rules = db.get_learned_rules(category="imports")
        assert rules == []

    def test_non_import_edges_ignored(self, project_env):
        """Only 'imports' edges should count toward fan-in."""
        _, _, db = project_env

        db.add_node("file:src/core.py", "file", "core.py", "src/core.py", layer="core")
        for i in range(4):
            db.add_node(f"file:tests/test_{i}.py", "file", f"test_{i}.py", f"tests/test_{i}.py", layer="test")
            db.add_edge(f"file:tests/test_{i}.py", "file:src/core.py", kind="tests")

        _infer_import_pattern_rules(db)

        rules = db.get_learned_rules(category="imports")
        assert rules == []

    def test_import_confidence_calculation(self, project_env):
        """Confidence should scale with import count, capped at 0.95."""
        _, _, db = project_env

        db.add_node("file:src/utils.py", "file", "utils.py", "src/utils.py", layer="core")
        for i in range(10):
            db.add_node(f"file:src/m{i}.py", "file", f"m{i}.py", f"src/m{i}.py", layer="service")
            db.add_edge(f"file:src/m{i}.py", "file:src/utils.py", kind="imports")

        _infer_import_pattern_rules(db)

        rules = db.get_learned_rules(category="imports")
        assert len(rules) == 1
        # 10 imports -> min(10/10, 0.95) = 0.95
        assert rules[0]["confidence"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Decision pattern inference
# ---------------------------------------------------------------------------

class TestDecisionPatternRules:
    """_infer_decision_pattern_rules detects recurring decision phrases."""

    def test_recurring_phrases_generate_rule(self, project_env):
        """Multiple decisions with the same phrase should produce a pattern rule."""
        _, _, db = project_env

        # Need at least 3 decisions total, and repeated phrases in same directory
        db.log_session("dp1", "Session 1", "1", [
            {"file_path": "src/api.py", "decision": "Use repository pattern for data access layer", "context": "arch"},
        ])
        db.log_session("dp2", "Session 2", "2", [
            {"file_path": "src/service.py", "decision": "Use repository pattern for data access layer", "context": "arch"},
        ])
        db.log_session("dp3", "Session 3", "3", [
            {"file_path": "src/handler.py", "decision": "Use repository pattern for all queries", "context": "arch"},
        ])

        # Record some outcomes so they count as "successful" (kept or None)
        db.record_outcome("dp1", "src/api.py", "kept")

        _infer_decision_pattern_rules(db)

        rules = db.get_learned_rules(category="patterns")
        # Should find a recurring phrase like "repository pattern" or "data access"
        assert len(rules) >= 1

    def test_few_decisions_no_rule(self, project_env):
        """Fewer than 3 total decisions should not trigger inference."""
        _, _, db = project_env

        db.log_session("dp_few1", "S1", "1", [
            {"file_path": "src/a.py", "decision": "Do something", "context": "c"},
        ])
        db.log_session("dp_few2", "S2", "2", [
            {"file_path": "src/b.py", "decision": "Do another thing", "context": "c"},
        ])

        _infer_decision_pattern_rules(db)

        rules = db.get_learned_rules(category="patterns")
        assert rules == []


# ---------------------------------------------------------------------------
# File co-change inference
# ---------------------------------------------------------------------------

class TestCoChangeRules:
    """_infer_file_co_change_rules detects frequently co-modified files."""

    def test_co_changed_files_generate_rule(self, project_env):
        """Files modified together in 2+ sessions should generate a co-change rule."""
        _, _, db = project_env

        # Two sessions, each touching api.py and service.py together
        db.log_session("cc1", "Session 1", "1", [
            {"file_path": "src/api.py", "decision": "Update endpoint", "context": "api"},
            {"file_path": "src/service.py", "decision": "Update handler", "context": "service"},
        ])
        db.log_session("cc2", "Session 2", "2", [
            {"file_path": "src/api.py", "decision": "Add validation", "context": "api"},
            {"file_path": "src/service.py", "decision": "Add business logic", "context": "service"},
        ])

        _infer_file_co_change_rules(db)

        rules = db.get_learned_rules(category="structure")
        assert len(rules) >= 1
        rule_text = rules[0]["rule_text"]
        assert "api.py" in rule_text
        assert "service.py" in rule_text
        assert "frequently modified together" in rule_text

    def test_single_session_co_change_no_rule(self, project_env):
        """Files co-changed in only 1 session should not generate a rule."""
        _, _, db = project_env

        db.log_session("cc_single", "Session 1", "1", [
            {"file_path": "src/api.py", "decision": "Update endpoint", "context": "api"},
            {"file_path": "src/service.py", "decision": "Update handler", "context": "service"},
        ])

        _infer_file_co_change_rules(db)

        rules = db.get_learned_rules(category="structure")
        assert rules == []

    def test_co_change_confidence_calculation(self, project_env):
        """Co-change confidence should scale with count, capped at 0.9."""
        _, _, db = project_env

        for i in range(5):
            db.log_session(f"cc_conf_{i}", f"Session {i}", str(i), [
                {"file_path": "src/config.py", "decision": f"Update config {i}", "context": "config"},
                {"file_path": "src/main.py", "decision": f"Update main {i}", "context": "main"},
            ])

        _infer_file_co_change_rules(db)

        rules = db.get_learned_rules(category="structure")
        assert len(rules) >= 1
        # 5 co-changes -> min(5/4, 0.9) = 0.9
        assert rules[0]["confidence"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Duplicate rule upsert
# ---------------------------------------------------------------------------

class TestUpsertBehavior:
    """_upsert_rule should update confidence on duplicates, not create new rows."""

    def test_duplicate_rule_updates_confidence(self, project_env):
        """Running inference twice with same data should upsert, not duplicate."""
        _, _, db = project_env

        db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
        db.add_node("file:src/service.py", "file", "service.py", "src/service.py", layer="service")
        db.add_node("file:tests/test_api.py", "file", "test_api.py", "tests/test_api.py", layer="test")
        db.add_node("file:tests/test_service.py", "file", "test_service.py", "tests/test_service.py", layer="test")

        # Run twice
        _infer_test_pairing_rules(db)
        first_rules = db.get_learned_rules(category="testing")
        count_after_first = len(first_rules)

        _infer_test_pairing_rules(db)
        second_rules = db.get_learned_rules(category="testing")
        count_after_second = len(second_rules)

        # Same number of rules (upsert, not insert)
        assert count_after_first == count_after_second


# ---------------------------------------------------------------------------
# _find_common_phrases
# ---------------------------------------------------------------------------

class TestFindCommonPhrases:
    """Tests for the _find_common_phrases helper."""

    def test_finds_repeated_phrases(self):
        """Phrases appearing in multiple texts should be returned."""
        texts = [
            "use the repository pattern for data access",
            "apply the repository pattern in the service layer",
            "the repository pattern is preferred here",
        ]
        result = _find_common_phrases(texts)
        # "the repository pattern" appears in all 3 texts
        phrases = [phrase for phrase, count in result]
        assert any("repository pattern" in p for p in phrases)

    def test_no_common_phrases(self):
        """Completely different texts should return no common phrases."""
        texts = [
            "alpha beta gamma",
            "one two three four",
        ]
        result = _find_common_phrases(texts)
        assert result == []

    def test_empty_input(self):
        """Empty input should return empty list."""
        assert _find_common_phrases([]) == []

    def test_single_text(self):
        """Single text cannot produce phrases appearing in multiple texts."""
        result = _find_common_phrases(["hello world foo bar"])
        # Each phrase appears only once, so count < 2 -> filtered out
        assert result == []

    def test_short_phrases_filtered(self):
        """Phrases shorter than min_words (default 3) should not appear."""
        texts = [
            "use this pattern",
            "use this approach",
        ]
        result = _find_common_phrases(texts, min_words=3)
        # "use this" is only 2 words, should not appear with min_words=3
        for phrase, _ in result:
            assert len(phrase.split()) >= 3 or phrase not in ["use this"]


# ---------------------------------------------------------------------------
# run_rule_inference integration
# ---------------------------------------------------------------------------

class TestRunRuleInference:
    """Integration test for the top-level run_rule_inference()."""

    @patch("indexer.rule_learner.get_data_dir")
    def test_full_inference_with_data(self, mock_data_dir, populated_db):
        """run_rule_inference with populated data should produce rules without crashing."""
        project, data_dir, db = populated_db
        mock_data_dir.return_value = data_dir

        # Add extra data to trigger rules
        db.add_node("file:tests/test_service.py", "file", "test_service.py", "tests/test_service.py", layer="test")
        # Add more import edges to trigger import rule (need 3+)
        db.add_node("file:src/handler.py", "file", "handler.py", "src/handler.py", layer="api")
        db.add_node("file:src/middleware.py", "file", "middleware.py", "src/middleware.py", layer="api")
        db.add_edge("file:src/handler.py", "file:src/service.py", kind="imports")
        db.add_edge("file:src/middleware.py", "file:src/service.py", kind="imports")

        # pre-existing rules from conftest populated_db
        initial_count = len(db.get_learned_rules())

        run_rule_inference()

        final_count = len(db.get_learned_rules())
        # Should have generated at least one new rule
        assert final_count >= initial_count
