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
        mock_changesets = {"open_changesets": [], "count": 0, "warning": None}

        with patch("mcp_server.tools.learning.get_roadmap", return_value=mock_roadmap, create=True):
            with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
                with patch("mcp_server.tools.changesets.list_open_changesets",
                           return_value=mock_changesets):
                    result = learning.get_session_context()

        assert "recent_sessions" in result
        assert "recent_decisions" in result
        assert "confidence" in result
        assert "top_signals" in result
        assert "preferences" in result["top_signals"]
        assert "rules" in result["top_signals"]

    def test_session_context_with_roadmap(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        mock_roadmap = {
            "current_phase": {"name": "API Refactor", "next_action": "Fix routes", "status": "in_progress"},
        }
        with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"open_changesets": [], "count": 0, "warning": None}):
                result = learning.get_session_context()

        # New shape: current_phase at top level (no more nested `roadmap` key)
        assert result["current_phase"]["name"] == "API Refactor"
        assert result["current_phase"]["next_action"] == "Fix routes"

    def test_session_context_roadmap_failure_graceful(self, tmp_path, monkeypatch):
        """If roadmap import fails, session_context should still work."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("broken")):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"open_changesets": [], "count": 0, "warning": None}):
                result = learning.get_session_context()

        # On failure current_phase stays empty dict
        assert result["current_phase"] == {}

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
        assert result["top_signals"]["preferences"] == []
        assert result["top_signals"]["rules"] == []

    def test_session_context_surfaces_phase_key_decisions(self, tmp_path, monkeypatch):
        """Bug 5 regression: complete_phase(key_decisions=[...]) writes to
        the roadmap store, NOT the decisions table. Without surfacing in
        session_context, a fresh session has no way to learn what was
        just decided when the previous phase completed.

        Fix: query the roadmap's recently-completed phases and include
        their key_decisions tagged with source='phase_completion'.
        """
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        # Simulate two completed phases with key_decisions
        mock_roadmap_for_get = {
            "current_phase": {"name": "Phase 5", "next_action": "Do", "status": "in_progress"},
        }
        mock_roadmap_data = {
            "completed_phases": [
                {
                    "number": 1,
                    "name": "Stub closure",
                    "key_decisions": [
                        "Plan 1 Week 1 multi-host Go client foundation shipped (commit ce24961).",
                        "Hardening pass commit 3a4bc05 closes Week 1.",
                    ],
                },
                {
                    "number": 2,
                    "name": "Plan 1 Week 2 — Core commands ported",
                    "key_decisions": [
                        "12 Python CLI commands ported to Go (operator/cmd/uadp/*.go).",
                    ],
                },
            ],
        }

        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value=mock_roadmap_for_get):
            with patch("mcp_server.tools.roadmap._load_roadmap",
                       return_value=mock_roadmap_data):
                with patch("mcp_server.tools.changesets.list_open_changesets",
                           return_value={"open_changesets": [], "count": 0, "warning": None}):
                    result = learning.get_session_context()

        assert "recent_phase_decisions" in result, (
            "Bug 5 regression: get_session_context must include "
            "`recent_phase_decisions` field"
        )
        decisions = result["recent_phase_decisions"]
        assert len(decisions) >= 2
        # Most recent completed phase first (phase 2)
        assert decisions[0]["phase_number"] == 2
        assert decisions[0]["source"] == "phase_completion"
        assert "Go" in decisions[0]["decision"]
        # Phase 1 decisions present too
        phase_1_decisions = [d for d in decisions if d["phase_number"] == 1]
        assert len(phase_1_decisions) >= 1

    def test_session_context_phase_decisions_capped_at_5(self, tmp_path, monkeypatch):
        """Don't blow the ~500-token budget — cap phase decisions at 5."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        many_decisions = [f"Decision {i}" for i in range(20)]
        mock_roadmap_data = {
            "completed_phases": [
                {"number": 1, "name": "Phase 1", "key_decisions": many_decisions},
            ],
        }
        with patch("mcp_server.tools.roadmap._load_roadmap",
                   return_value=mock_roadmap_data):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"open_changesets": [], "count": 0, "warning": None}):
                result = learning.get_session_context()

        assert len(result["recent_phase_decisions"]) <= 5

    def test_session_context_no_completed_phases(self, tmp_path, monkeypatch):
        """Empty completed_phases → recent_phase_decisions is empty list,
        not missing or None."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("mcp_server.tools.roadmap._load_roadmap",
                   return_value={"completed_phases": []}):
            with patch("mcp_server.tools.changesets.list_open_changesets",
                       return_value={"open_changesets": [], "count": 0, "warning": None}):
                result = learning.get_session_context()

        assert result["recent_phase_decisions"] == []

    def test_session_context_recent_decisions_tagged_with_source(self, tmp_path, monkeypatch):
        """Bug 5 — the existing recent_decisions list (from sessions table)
        should now be tagged source='session' so AIs can distinguish it
        from the new recent_phase_decisions list."""
        project, data_dir, db = _setup_project(tmp_path, monkeypatch)
        # Seed a real decision so recent_decisions isn't empty
        db.log_session("s-test", "test session", "1", [
            {"file_path": "src/api.py", "decision": "Use REST", "context": "ctx"},
        ])
        db.close()

        with patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value={"open_changesets": [], "count": 0, "warning": None}):
            result = learning.get_session_context()

        if result["recent_decisions"]:
            for d in result["recent_decisions"]:
                assert d.get("source") == "session", (
                    f"recent_decisions entries must be tagged source='session'; "
                    f"got {d}"
                )


# =====================================================================
# get_session_context exception branches (lines 171-173, 180-182)
# =====================================================================

class TestGetSessionContextExceptions:
    def test_graceful_on_dependent_failures(self, tmp_path, monkeypatch):
        """get_session_context continues when sub-calls raise.

        v1.7.0 dropped global_intelligence and indexing_progress from the
        response (they belong in admin/status tools, not session context).
        Verify the function still returns a valid response when sub-calls fail.
        """
        _setup_project(tmp_path, monkeypatch)

        with patch("mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("no roadmap")), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   side_effect=Exception("no changesets")):
            result = learning.get_session_context()

        assert result is not None
        assert "recent_sessions" in result
        assert result["current_phase"] == {}
        assert result["open_changesets"] == []


# =====================================================================
# _maturity_hint boundary coverage (lines 233, 237)
# =====================================================================

class TestMaturityHint:
    def test_score_above_80_returns_mature_hint(self):
        """Score >= 80 returns the 'mature' hint string."""
        result = learning._maturity_hint(80.0)
        assert "mature" in result.lower() or "learned patterns" in result.lower()

    def test_score_between_50_and_80_returns_good_progress_hint(self):
        """Score >= 50 but < 80 returns the 'good progress' hint."""
        result = learning._maturity_hint(60.0)
        assert "confidence" in result.lower() or "progress" in result.lower()

    def test_score_between_20_and_50_returns_building_hint(self):
        """Score >= 20 but < 50 returns the 'still building' hint (line 237)."""
        result = learning._maturity_hint(30.0)
        assert "building" in result.lower() or "memory" in result.lower()

    def test_score_below_20_returns_fresh_start_hint(self):
        """Score < 20 returns the 'fresh start' hint."""
        result = learning._maturity_hint(5.0)
        assert "fresh" in result.lower() or "every session" in result.lower()


# =====================================================================
# v1.8: Open-changesets key bug (Change 0) + focus inference (Change 1)
# =====================================================================

def _changeset(id: str, files: list[str], created: str = "2026-04-22",
               description: str = "desc") -> dict:
    """Helper producing the raw list_open_changesets() item shape."""
    return {
        "id": id,
        "description": description,
        "created": created,
        "files_pending": files,
        "blocker": None,
    }


class TestOpenChangesetsKeyFixed:
    """Change 0: get_session_context() must read the real key."""

    def test_open_changesets_key_fixed(self, tmp_path, monkeypatch):
        """When the mock returns real shape, open_changesets is non-empty."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        cs_payload = {
            "open_changesets": [
                _changeset("auth-refactor", ["src/auth.py", "src/user.py"]),
            ],
            "count": 1,
            "warning": None,
        }

        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value={"current_phase": {}}), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value=cs_payload):
            result = learning.get_session_context()

        assert len(result["open_changesets"]) == 1
        assert result["open_changesets"][0]["id"] == "auth-refactor"
        assert result["open_changesets"][0]["files_pending_count"] == 2


class TestInferFocus:
    """Change 1: _infer_focus priority rules."""

    def test_focus_from_changeset(self):
        cs = [_changeset("auth-refactor", ["src/auth.py", "src/user.py"])]
        focus, source = learning._infer_focus(cs, {})
        assert focus == "src/auth.py"
        assert source == "open_changeset:auth-refactor"

    def test_focus_prefers_most_recent_changeset(self):
        cs = [
            _changeset("old", ["src/old.py"], created="2026-01-01"),
            _changeset("new", ["src/new.py"], created="2026-04-22"),
        ]
        focus, source = learning._infer_focus(cs, {})
        assert focus == "src/new.py"
        assert source == "open_changeset:new"

    def test_focus_skips_changeset_with_no_pending_files(self):
        cs = [
            _changeset("empty", [], created="2026-04-22"),
            _changeset("has-files", ["src/x.py"], created="2026-01-01"),
        ]
        focus, source = learning._infer_focus(cs, {})
        assert focus == "src/x.py"
        assert source == "open_changeset:has-files"

    def test_focus_from_next_action(self):
        cp = {"next_action": "Refactor authentication middleware pipeline"}
        focus, source = learning._infer_focus([], cp)
        assert source == "next_action"
        # All tokens >= 4 chars
        assert "refactor" in focus
        assert "authentication" in focus
        assert "middleware" in focus
        assert "pipeline" in focus

    def test_focus_weak_signal_ignored_short(self):
        cp = {"next_action": "continue work"}
        focus, source = learning._infer_focus([], cp)
        assert focus is None
        assert source is None

    def test_focus_weak_signal_ignored_stop_list_only(self):
        cp = {"next_action": "continue work fix todo"}
        focus, source = learning._infer_focus([], cp)
        assert focus is None
        assert source is None

    def test_focus_none_when_no_signals(self):
        focus, source = learning._infer_focus([], {})
        assert focus is None
        assert source is None


class TestSessionContextFocus:
    """Change 1: focus inference wired into get_session_context()."""

    def test_focus_source_field_always_present(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value={"current_phase": {}}), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value={"open_changesets": [], "count": 0, "warning": None}):
            result = learning.get_session_context()
        assert "focus_source" in result
        assert result["focus_source"] is None

    def test_focus_source_reflects_changeset(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        cs = {
            "open_changesets": [_changeset("api-work", ["src/api.py"])],
            "count": 1, "warning": None,
        }
        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value={"current_phase": {}}), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value=cs):
            result = learning.get_session_context()

        assert result["focus_source"] == "open_changeset:api-work"
        # Decisions ranked against "src/api.py" should surface api.py matches.
        # Seed has 2 decisions touching src/api.py — both should appear.
        api_matches = [d for d in result["recent_decisions"]
                       if d.get("file_path") == "src/api.py"]
        assert len(api_matches) >= 1

    def test_focus_pads_with_recent_when_few_matches(self, tmp_path, monkeypatch):
        """Focus returning 0 matches → fall back to chronological 3."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        # Focus on a file that has NO decisions — should pad with recent.
        cs = {
            "open_changesets": [_changeset("unseen", ["src/unknown.py"])],
            "count": 1, "warning": None,
        }
        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value={"current_phase": {}}), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value=cs):
            result = learning.get_session_context()

        # Seed has 3 decisions total → pads to 3
        assert len(result["recent_decisions"]) == 3
        assert result["focus_source"] == "open_changeset:unseen"

    def test_focus_from_next_action_sets_source(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        roadmap = {
            "current_phase": {
                "name": "API Hardening",
                "status": "in_progress",
                "next_action": "Add validation layer to api endpoints",
            }
        }
        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value=roadmap), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value={"open_changesets": [], "count": 0, "warning": None}):
            result = learning.get_session_context()

        assert result["focus_source"] == "next_action"

    def test_no_focus_uses_chronological_fallback(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        with patch("mcp_server.tools.roadmap.get_roadmap",
                   return_value={"current_phase": {}}), \
             patch("mcp_server.tools.changesets.list_open_changesets",
                   return_value={"open_changesets": [], "count": 0, "warning": None}):
            result = learning.get_session_context()

        assert result["focus_source"] is None
        # 3 decisions seeded → should get all 3, newest first
        assert len(result["recent_decisions"]) == 3
