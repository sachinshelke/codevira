"""
Tests for Codevira v1.4 "Living Memory" features:
  - Dependency edge wiring (add_edge, remove_edges, blast radius)
  - Graph visualization (Mermaid, DOT export)
  - Outcome tracking (kept, modified, reverted)
  - Confidence scoring
  - Developer preference learning
  - Learned rules
  - Project maturity metrics
  - Session context handoff
"""
import json
import os
from pathlib import Path

import mcp_server.paths as paths
from indexer.sqlite_graph import SQLiteGraph


def _setup_db(tmp_path, monkeypatch) -> SQLiteGraph:
    """Create a temp project with a SQLite graph database."""
    project_root = tmp_path / "test-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project_root.resolve())

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    return db


# =====================================================================
# Edge Management Tests
# =====================================================================

class TestEdgeManagement:
    def test_add_edge(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        edges = db.get_edges_from("file:a.py")
        assert len(edges) == 1
        assert edges[0]["target_id"] == "file:b.py"
        assert edges[0]["kind"] == "imports"
        db.close()

    def test_remove_edges_for_node(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:a.py", "file:c.py", kind="imports")
        assert len(db.get_edges_from("file:a.py")) == 2

        db.remove_edges_for_node("file:a.py")
        assert len(db.get_edges_from("file:a.py")) == 0
        db.close()

    def test_blast_radius_with_edges(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        # Create a chain: a -> b -> c
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")

        # Blast radius of c should include a and b (they depend on c)
        blast = db.get_blast_radius("file:c.py", max_depth=3)
        affected_ids = {r["id"] for r in blast}
        assert "file:b.py" in affected_ids
        assert "file:a.py" in affected_ids
        db.close()

    def test_get_edges_to(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")

        edges_to_b = db.get_edges_to("file:b.py")
        assert len(edges_to_b) == 1
        assert edges_to_b[0]["source_id"] == "file:a.py"
        db.close()

    def test_get_all_edges(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")

        all_edges = db.get_all_edges()
        assert len(all_edges) == 2
        db.close()


# =====================================================================
# Outcome Tracking Tests
# =====================================================================

class TestOutcomeTracking:
    def test_record_and_retrieve_outcome(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("sess-001", "Test session", "1", [
            {"file_path": "src/api.py", "decision": "Use REST endpoints", "context": "API design"}
        ])

        db.record_outcome("sess-001", "src/api.py", "kept", decision_id=1)
        outcomes = db.get_outcomes_for_file("src/api.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "kept"
        db.close()

    def test_multiple_outcomes_for_file(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("sess-001", "Session 1", "1", [
            {"file_path": "src/api.py", "decision": "Decision 1", "context": "ctx"}
        ])
        db.log_session("sess-002", "Session 2", "1", [
            {"file_path": "src/api.py", "decision": "Decision 2", "context": "ctx"}
        ])

        db.record_outcome("sess-001", "src/api.py", "kept")
        db.record_outcome("sess-002", "src/api.py", "modified", delta_summary="Changed naming")

        outcomes = db.get_outcomes_for_file("src/api.py")
        assert len(outcomes) == 2
        db.close()


# =====================================================================
# Confidence Scoring Tests
# =====================================================================

class TestConfidenceScoring:
    def test_empty_confidence(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        confidence = db.get_decision_confidence()
        assert confidence["total_decisions"] == 0
        assert confidence["confidence"] == 0.0
        db.close()

    def test_all_kept_confidence(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])

        db.record_outcome("s1", "a.py", "kept")
        db.record_outcome("s1", "a.py", "kept")
        db.record_outcome("s1", "a.py", "kept")

        confidence = db.get_decision_confidence(file_path="a.py")
        assert confidence["confidence"] == 1.0
        assert confidence["kept"] == 3
        db.close()

    def test_mixed_confidence(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])

        db.record_outcome("s1", "a.py", "kept")      # +1.0
        db.record_outcome("s1", "a.py", "modified")   # +0.5
        db.record_outcome("s1", "a.py", "reverted")   # +0.0

        confidence = db.get_decision_confidence(file_path="a.py")
        # (1 + 0.5 + 0) / 3 = 0.5
        assert confidence["confidence"] == 0.5
        db.close()


# =====================================================================
# Developer Preferences Tests
# =====================================================================

class TestPreferences:
    def test_record_and_retrieve_preference(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.record_preference("naming", "Prefers snake_case", example="src/api.py")

        prefs = db.get_preferences(category="naming")
        assert len(prefs) == 1
        assert prefs[0]["signal"] == "Prefers snake_case"
        assert prefs[0]["frequency"] == 1
        db.close()

    def test_preference_frequency_increases(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("naming", "Prefers snake_case")

        prefs = db.get_preferences(category="naming")
        assert len(prefs) == 1
        assert prefs[0]["frequency"] == 3
        db.close()

    def test_preference_filter_by_min_frequency(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("structure", "Uses early returns")
        db.record_preference("structure", "Uses early returns")

        prefs = db.get_preferences(min_frequency=2)
        assert len(prefs) == 1
        assert prefs[0]["signal"] == "Uses early returns"
        db.close()


# =====================================================================
# Learned Rules Tests
# =====================================================================

class TestLearnedRules:
    def test_add_and_retrieve_rule(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_learned_rule(
            "Files in src/api/ should have tests",
            confidence=0.8,
            source_sessions=["s1", "s2"],
            category="testing",
            file_pattern="src/api/*",
        )

        rules = db.get_learned_rules(category="testing")
        assert len(rules) == 1
        assert rules[0]["confidence"] == 0.8
        assert "src/api" in rules[0]["rule_text"]
        db.close()

    def test_rules_filter_by_confidence(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_learned_rule("Low confidence rule", 0.2, [], category="testing")
        db.add_learned_rule("High confidence rule", 0.9, [], category="testing")

        rules = db.get_learned_rules(min_confidence=0.5)
        assert len(rules) == 1
        assert rules[0]["rule_text"] == "High confidence rule"
        db.close()

    def test_update_rule_confidence(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_learned_rule("A rule", 0.5, ["s1"], category="testing")

        rules = db.get_learned_rules()
        rule_id = rules[0]["id"]
        db.update_learned_rule(rule_id, confidence=0.9)

        updated = db.get_learned_rules()
        assert updated[0]["confidence"] == 0.9
        db.close()


# =====================================================================
# Project Maturity Tests
# =====================================================================

class TestProjectMaturity:
    def test_empty_project_maturity(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        maturity = db.get_project_maturity()
        assert maturity["session_count"] == 0
        assert maturity["coverage"] == 0.0
        assert maturity["overall_confidence"] == 0.0
        db.close()

    def test_maturity_with_sessions(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.log_session("s1", "Session 1", "1", [
            {"file_path": "a.py", "decision": "d1", "context": "c"}
        ])

        maturity = db.get_project_maturity()
        assert maturity["session_count"] == 1
        assert maturity["total_files"] == 2
        assert maturity["covered_files"] == 1
        assert maturity["coverage"] == 0.5
        db.close()


# =====================================================================
# Graph Visualization Tests
# =====================================================================

class TestGraphVisualization:
    def test_export_mermaid(self, tmp_path, monkeypatch):
        from mcp_server.tools.graph import export_graph

        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="core")
        db.add_node("file:src/b.py", "file", "b.py", "src/b.py", layer="api")
        db.add_edge("file:src/a.py", "file:src/b.py", kind="imports")
        db.close()

        result = export_graph(format="mermaid")
        assert result["format"] == "mermaid"
        assert result["node_count"] == 2
        assert result["edge_count"] == 1
        assert "graph LR" in result["output"]
        assert "-->" in result["output"]

    def test_export_dot(self, tmp_path, monkeypatch):
        from mcp_server.tools.graph import export_graph

        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="core")
        db.add_node("file:src/b.py", "file", "b.py", "src/b.py", layer="api")
        db.add_edge("file:src/a.py", "file:src/b.py", kind="imports")
        db.close()

        result = export_graph(format="dot")
        assert "digraph codevira" in result["output"]
        assert "->" in result["output"]


# =====================================================================
# Session Helpers Tests
# =====================================================================

# =====================================================================
# Edge Case Tests
# =====================================================================

class TestEdgeCases:
    def test_add_edge_idempotent(self, tmp_path, monkeypatch):
        """Adding the same edge twice should not crash (INSERT OR REPLACE)."""
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:a.py", "file:b.py", kind="imports")  # duplicate
        assert len(db.get_edges_from("file:a.py")) == 1
        db.close()

    def test_remove_edges_nonexistent_node(self, tmp_path, monkeypatch):
        """Removing edges for a node that has none should not crash."""
        db = _setup_db(tmp_path, monkeypatch)
        db.remove_edges_for_node("file:nonexistent.py")
        db.close()

    def test_confidence_with_only_reverts(self, tmp_path, monkeypatch):
        """All reverted outcomes should yield 0.0 confidence."""
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])
        db.record_outcome("s1", "a.py", "reverted")
        db.record_outcome("s1", "a.py", "reverted")
        confidence = db.get_decision_confidence(file_path="a.py")
        assert confidence["confidence"] == 0.0
        db.close()

    def test_preference_with_none_example(self, tmp_path, monkeypatch):
        """Recording a preference with no example should work."""
        db = _setup_db(tmp_path, monkeypatch)
        db.record_preference("naming", "Uses camelCase", example=None)
        prefs = db.get_preferences()
        assert len(prefs) == 1
        assert prefs[0]["example"] is None
        db.close()

    def test_blast_radius_no_edges(self, tmp_path, monkeypatch):
        """Blast radius on a node with no edges should return empty."""
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:isolated.py", "file", "isolated.py", "isolated.py")
        blast = db.get_blast_radius("file:isolated.py")
        assert blast == []
        db.close()

    def test_maturity_no_files(self, tmp_path, monkeypatch):
        """Maturity with zero files should not divide by zero."""
        db = _setup_db(tmp_path, monkeypatch)
        maturity = db.get_project_maturity()
        assert maturity["coverage"] == 0.0
        assert maturity["total_files"] == 0
        db.close()

    def test_learned_rule_empty_file_pattern(self, tmp_path, monkeypatch):
        """Rules with no file_pattern should still be retrievable."""
        db = _setup_db(tmp_path, monkeypatch)
        db.add_learned_rule("General rule", 0.7, [], category="patterns", file_pattern=None)
        rules = db.get_learned_rules(category="patterns")
        assert len(rules) == 1
        db.close()


class TestSessionHelpers:
    def test_get_recent_sessions(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("s1", "First", "1", [])
        db.log_session("s2", "Second", "2", [])

        recent = db.get_recent_sessions(limit=5)
        assert len(recent) == 2
        db.close()

    def test_get_recent_decisions(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.log_session("s1", "Test", "1", [
            {"file_path": "a.py", "decision": "Decision A", "context": "ctx"},
            {"file_path": "b.py", "decision": "Decision B", "context": "ctx"},
        ])

        decisions = db.get_recent_decisions(limit=5)
        assert len(decisions) == 2
        db.close()
