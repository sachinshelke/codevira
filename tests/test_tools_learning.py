"""
Tests for mcp_server/tools/learning.py — adaptive memory, confidence scoring,
preferences, learned rules, project maturity, and session context.

Covers ALL functions (0% coverage previously):
  - get_decision_confidence: scope by file_path or pattern
  - get_preferences: filtered list with hints
  - get_learned_rules: filtered rules with hints
  - get_project_maturity: composite 0-100 score + level + hint
  - get_session_context: aggregated context with roadmap, changesets, global intelligence
  - _interpret_confidence: boundary tests for interpretation strings
  - _compute_maturity_score: weighted formula verification
  - _maturity_level: threshold-based level classification
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import mcp_server.paths as paths
from indexer.sqlite_graph import SQLiteGraph
from mcp_server.tools import learning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_project(tmp_path, monkeypatch) -> tuple[Path, Path, SQLiteGraph]:
    """Create a temp project with a graph database and monkeypatched paths."""
    project_root = tmp_path / "test-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text("project:\n  name: test-learning\n")
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project_root.resolve())

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    return project_root, data_dir, db


def _seed_outcomes(db: SQLiteGraph, outcomes: list[tuple[str, str, str]]) -> None:
    """Seed outcomes. Each tuple is (session_id, file_path, outcome_type)."""
    sessions_seen = set()
    for sess_id, fp, ot in outcomes:
        if sess_id not in sessions_seen:
            db.log_session(sess_id, f"Session {sess_id}", "1", [
                {"file_path": fp, "decision": f"decision for {fp}", "context": "test"}
            ])
            sessions_seen.add(sess_id)
        db.record_outcome(sess_id, fp, ot)


def _seed_full_project(db: SQLiteGraph) -> None:
    """Create a project with sessions, outcomes, preferences, rules, and files."""
    # Files
    db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
    db.add_node("file:src/core.py", "file", "core.py", "src/core.py", layer="core")

    # Sessions with decisions
    db.log_session("s1", "First session", "1", [
        {"file_path": "src/api.py", "decision": "Use REST", "context": "api design"},
    ])
    db.log_session("s2", "Second session", "2", [
        {"file_path": "src/core.py", "decision": "Add caching", "context": "perf"},
    ])
    db.log_session("s3", "Third session", "2", [
        {"file_path": "src/api.py", "decision": "Add validation", "context": "security"},
    ])

    # Outcomes
    db.record_outcome("s1", "src/api.py", "kept")
    db.record_outcome("s2", "src/core.py", "kept")
    db.record_outcome("s3", "src/api.py", "modified")

    # Preferences
    db.record_preference("naming", "Prefers snake_case", example="src/api.py")
    db.record_preference("naming", "Prefers snake_case")  # frequency -> 2
    db.record_preference("structure", "Uses early returns")

    # Learned rules
    db.add_learned_rule("Test files in tests/", 0.8, ["s1", "s2"], category="testing")
    db.add_learned_rule("Import order: stdlib first", 0.6, ["s1"], category="imports")
    db.add_learned_rule("Low confidence rule", 0.2, ["s3"], category="naming")


# =====================================================================
# _interpret_confidence
# =====================================================================

class TestInterpretConfidence:
    def test_no_data(self):
        result = learning._interpret_confidence(0.0)
        assert "No data" in result

    def test_low_confidence(self):
        result = learning._interpret_confidence(0.3)
        assert "Low confidence" in result

    def test_moderate_confidence(self):
        result = learning._interpret_confidence(0.6)
        assert "Moderate confidence" in result

    def test_high_confidence(self):
        result = learning._interpret_confidence(0.9)
        assert "High confidence" in result

    def test_boundary_zero_point_five(self):
        result = learning._interpret_confidence(0.5)
        assert "Moderate confidence" in result

    def test_boundary_zero_point_eight(self):
        result = learning._interpret_confidence(0.8)
        assert "High confidence" in result

    def test_just_above_zero(self):
        result = learning._interpret_confidence(0.01)
        assert "Low confidence" in result


# =====================================================================
# _compute_maturity_score
# =====================================================================

class TestComputeMaturityScore:
    def test_zero_maturity(self):
        maturity = {
            "session_count": 0, "coverage": 0.0, "overall_confidence": 0.0,
            "learned_rules": 0, "preference_signals": 0,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 0.0

    def test_max_maturity(self):
        maturity = {
            "session_count": 20, "coverage": 1.0, "overall_confidence": 1.0,
            "learned_rules": 10, "preference_signals": 10,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 100.0

    def test_sessions_capped_at_20pts(self):
        maturity = {
            "session_count": 100, "coverage": 0.0, "overall_confidence": 0.0,
            "learned_rules": 0, "preference_signals": 0,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 20.0  # max(100*2, 20) = 20

    def test_coverage_contributes_30pts(self):
        maturity = {
            "session_count": 0, "coverage": 1.0, "overall_confidence": 0.0,
            "learned_rules": 0, "preference_signals": 0,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 30.0

    def test_confidence_contributes_25pts(self):
        maturity = {
            "session_count": 0, "coverage": 0.0, "overall_confidence": 1.0,
            "learned_rules": 0, "preference_signals": 0,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 25.0

    def test_rules_capped_at_15pts(self):
        maturity = {
            "session_count": 0, "coverage": 0.0, "overall_confidence": 0.0,
            "learned_rules": 100, "preference_signals": 0,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 15.0

    def test_preferences_capped_at_10pts(self):
        maturity = {
            "session_count": 0, "coverage": 0.0, "overall_confidence": 0.0,
            "learned_rules": 0, "preference_signals": 100,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 10.0

    def test_total_capped_at_100(self):
        """Even with overflowing inputs, score should not exceed 100."""
        maturity = {
            "session_count": 1000, "coverage": 5.0, "overall_confidence": 5.0,
            "learned_rules": 1000, "preference_signals": 1000,
        }
        score = learning._compute_maturity_score(maturity)
        assert score == 100.0

    def test_partial_maturity(self):
        """5 sessions=10pts, 50% coverage=15pts, 0.4 confidence=10pts, 2 rules=6pts, 1 pref=2pts."""
        maturity = {
            "session_count": 5, "coverage": 0.5, "overall_confidence": 0.4,
            "learned_rules": 2, "preference_signals": 1,
        }
        score = learning._compute_maturity_score(maturity)
        expected = 10.0 + 15.0 + 10.0 + 6.0 + 2.0
        assert score == expected


# =====================================================================
# _maturity_level
# =====================================================================

class TestMaturityLevel:
    def test_new_project(self):
        result = learning._maturity_level(10)
        assert "New" in result

    def test_growing_project(self):
        result = learning._maturity_level(35)
        assert "Growing" in result

    def test_intermediate_project(self):
        result = learning._maturity_level(65)
        assert "Intermediate" in result

    def test_expert_project(self):
        result = learning._maturity_level(90)
        assert "Expert" in result

    def test_boundary_20(self):
        result = learning._maturity_level(20)
        assert "Growing" in result

    def test_boundary_50(self):
        result = learning._maturity_level(50)
        assert "Intermediate" in result

    def test_boundary_80(self):
        result = learning._maturity_level(80)
        assert "Expert" in result

    def test_zero(self):
        result = learning._maturity_level(0)
        assert "New" in result


# =====================================================================
# get_decision_confidence (tool-level)
# =====================================================================

class TestGetDecisionConfidence:
    def test_empty_db_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_decision_confidence()
        assert result["confidence"] == 0.0
        assert "No data" in result["interpretation"]

    def test_file_specific_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(db, [
            ("s1", "src/api.py", "kept"),
            ("s2", "src/api.py", "kept"),
            ("s3", "src/api.py", "kept"),
        ])
        db.close()
        result = learning.get_decision_confidence(file_path="src/api.py")
        assert result["scope"] == "src/api.py"
        assert result["confidence"] == 1.0
        assert "High confidence" in result["interpretation"]

    def test_pattern_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(db, [
            ("s1", "src/api.py", "kept"),
            ("s2", "src/core.py", "reverted"),
        ])
        db.close()
        result = learning.get_decision_confidence(pattern="src/")
        assert result["scope"] == "src/"
        assert result["total_decisions"] == 2

    def test_project_wide_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(db, [
            ("s1", "a.py", "kept"),
            ("s2", "b.py", "modified"),
        ])
        db.close()
        result = learning.get_decision_confidence()
        assert result["scope"] == "project-wide"
        assert result["total_decisions"] == 2


# =====================================================================
# get_preferences (tool-level)
# =====================================================================

class TestGetPreferences:
    def test_empty_preferences(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_preferences()
        assert result["total"] == 0
        assert "No preferences" in result["hint"]

    def test_preferences_with_data(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.record_preference("naming", "snake_case")
        db.record_preference("structure", "early returns")
        db.close()
        result = learning.get_preferences()
        assert result["total"] == 2
        assert "Apply these preferences" in result["hint"]

    def test_preferences_filtered_by_category(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.record_preference("naming", "snake_case")
        db.record_preference("structure", "early returns")
        db.close()
        result = learning.get_preferences(category="naming")
        assert result["total"] == 1
        assert result["preferences"][0]["signal"] == "snake_case"


# =====================================================================
# get_learned_rules (tool-level)
# =====================================================================

class TestGetLearnedRules:
    def test_empty_rules(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_learned_rules()
        assert result["total"] == 0
        assert "No rules" in result["hint"]

    def test_rules_with_data(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_learned_rule("Rule A", 0.8, ["s1"], category="testing")
        db.add_learned_rule("Rule B", 0.5, ["s1"], category="imports")
        db.close()
        result = learning.get_learned_rules()
        assert result["total"] == 2
        assert "learned from past sessions" in result["hint"]

    def test_rules_filtered_by_category(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_learned_rule("Rule A", 0.8, ["s1"], category="testing")
        db.add_learned_rule("Rule B", 0.5, ["s1"], category="imports")
        db.close()
        result = learning.get_learned_rules(category="testing")
        assert result["total"] == 1
        assert result["rules"][0]["category"] == "testing"

    def test_rules_filtered_by_file(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_learned_rule("API rule", 0.8, ["s1"], category="testing", file_pattern="src/api%")
        db.add_learned_rule("General rule", 0.7, ["s1"], category="testing")
        db.close()
        result = learning.get_learned_rules(file_path="src/api.py")
        # Should return both (general applies to all, API rule matches the pattern)
        assert result["total"] >= 1

    def test_rules_below_min_confidence_excluded(self, tmp_path, monkeypatch):
        """The tool passes min_confidence=0.3 by default, so rules below that are excluded."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_learned_rule("Very weak rule", 0.1, ["s1"], category="testing")
        db.add_learned_rule("Decent rule", 0.5, ["s1"], category="testing")
        db.close()
        result = learning.get_learned_rules()
        # Only the 0.5 rule should appear (0.1 < 0.3 min_confidence)
        assert result["total"] == 1
        assert result["rules"][0]["rule"] == "Decent rule"


# =====================================================================
# get_project_maturity (tool-level)
# =====================================================================

class TestGetProjectMaturity:
    def test_fresh_project_maturity(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_project_maturity()
        assert result["maturity_score"] == 0.0
        assert "New" in result["maturity_level"]
        assert "hint" in result

    def test_mature_project(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()
        result = learning.get_project_maturity()
        assert result["maturity_score"] > 0
        assert result["session_count"] == 3
        assert "hint" in result

    def test_maturity_includes_all_fields(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_project_maturity()
        assert "maturity_score" in result
        assert "maturity_level" in result
        assert "session_count" in result
        assert "coverage" in result
        assert "overall_confidence" in result
        assert "learned_rules" in result
        assert "preference_signals" in result


# =====================================================================
# get_session_context (tool-level)
# =====================================================================

class TestGetSessionContext:
    def test_session_context_basic(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        # Mock the external imports that session_context pulls in
        mock_roadmap = {
            "current_phase": {"name": "Phase 5", "next_action": "Do stuff", "status": "in_progress"},
        }
        mock_changesets = {"changesets": []}

        with patch("mcp_server.tools.learning.get_roadmap", return_value=mock_roadmap, create=True):
            with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
                with patch("mcp_server.tools.changesets.list_open_changesets",
                           return_value=mock_changesets):
                    result = learning.get_session_context()

        assert "recent_sessions" in result
        assert "recent_decisions" in result
        assert "overall_confidence" in result
        assert "top_preferences" in result
        assert "top_rules" in result

    def test_session_context_with_roadmap(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        mock_roadmap = {
            "current_phase": {"name": "API Refactor", "next_action": "Fix routes", "status": "in_progress"},
        }
        with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"changesets": []}):
                result = learning.get_session_context()

        assert result["roadmap"] is not None
        assert result["roadmap"]["current_phase"] == "API Refactor"
        assert result["roadmap"]["next_action"] == "Fix routes"

    def test_session_context_roadmap_failure_graceful(self, tmp_path, monkeypatch):
        """If roadmap import fails, session_context should still work."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("broken")):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"changesets": []}):
                result = learning.get_session_context()

        assert result["roadmap"] is None

    def test_session_context_changesets_failure_graceful(self, tmp_path, monkeypatch):
        """If changesets import fails, session_context should still work."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        mock_roadmap = {
            "current_phase": {"name": "Phase 1", "next_action": "Do", "status": "pending"},
        }
        with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       side_effect=Exception("broken")):
                result = learning.get_session_context()

        assert result["open_changesets"] == []

    def test_session_context_empty_db(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("mcp_server.tools.roadmap.get_roadmap",
                   side_effect=Exception("no roadmap")):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       side_effect=Exception("no changesets")):
                result = learning.get_session_context()

        assert result["recent_sessions"] == []
        assert result["recent_decisions"] == []
        assert result["top_preferences"] == []
        assert result["top_rules"] == []
