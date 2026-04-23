"""
Tests for SQLiteGraph (indexer/sqlite_graph.py).

Ported from test_v14_living_memory.py + new coverage & chaos tests.

Covers:
  - Edge management (add, remove, blast radius, get_edges_to, get_all_edges)
  - Outcome tracking (record, retrieve, multiple)
  - Confidence scoring (empty, all kept, mixed, only reverts)
  - Developer preferences (record, frequency, min_frequency filter)
  - Learned rules (add, filter, update)
  - Project maturity metrics
  - Graph visualization (Mermaid, DOT export)
  - Session helpers (recent sessions, recent decisions)
  - Edge cases (idempotent add, nonexistent remove, zero-division, etc.)
  - get_node / update_node / list_file_nodes
  - get_file_hash / update_file_hash
  - search_decisions
  - transaction() context manager
  - Chaos: concurrent R/W, large node count, unicode, disconnected graph
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from indexer.sqlite_graph import SQLiteGraph


# ===========================================================================
# Edge Management Tests
# ===========================================================================

class TestEdgeManagement:
    def test_add_edge(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        edges = db.get_edges_from("file:a.py")
        assert len(edges) == 1
        assert edges[0]["target_id"] == "file:b.py"
        assert edges[0]["kind"] == "imports"

    def test_remove_edges_for_node(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:a.py", "file:c.py", kind="imports")
        assert len(db.get_edges_from("file:a.py")) == 2

        db.remove_edges_for_node("file:a.py")
        assert len(db.get_edges_from("file:a.py")) == 0

    def test_blast_radius_with_edges(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")

        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")

        blast = db.get_blast_radius("file:c.py", max_depth=3)
        affected_ids = {r["id"] for r in blast}
        assert "file:b.py" in affected_ids
        assert "file:a.py" in affected_ids

    def test_get_edges_to(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")

        edges_to_b = db.get_edges_to("file:b.py")
        assert len(edges_to_b) == 1
        assert edges_to_b[0]["source_id"] == "file:a.py"

    def test_get_all_edges(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")

        all_edges = db.get_all_edges()
        assert len(all_edges) == 2

    def test_add_edge_with_line(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports", line=42)

        edges = db.get_edges_from("file:a.py")
        assert edges[0]["line"] == 42


# ===========================================================================
# Outcome Tracking Tests
# ===========================================================================

class TestOutcomeTracking:
    def test_record_and_retrieve_outcome(self, project_env):
        _, _, db = project_env
        db.log_session("sess-001", "Test session", "1", [
            {"file_path": "src/api.py", "decision": "Use REST endpoints", "context": "API design"}
        ])
        db.record_outcome("sess-001", "src/api.py", "kept", decision_id=1)
        outcomes = db.get_outcomes_for_file("src/api.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "kept"

    def test_multiple_outcomes_for_file(self, project_env):
        _, _, db = project_env
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


# ===========================================================================
# Confidence Scoring Tests
# ===========================================================================

class TestConfidenceScoring:
    def test_empty_confidence(self, project_env):
        _, _, db = project_env
        confidence = db.get_decision_confidence()
        assert confidence["total_decisions"] == 0
        assert confidence["confidence"] == 0.0

    def test_all_kept_confidence(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])
        db.record_outcome("s1", "a.py", "kept")
        db.record_outcome("s1", "a.py", "kept")
        db.record_outcome("s1", "a.py", "kept")

        confidence = db.get_decision_confidence(file_path="a.py")
        assert confidence["confidence"] == 1.0
        assert confidence["kept"] == 3

    def test_mixed_confidence(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])
        db.record_outcome("s1", "a.py", "kept")      # +1.0
        db.record_outcome("s1", "a.py", "modified")   # +0.5
        db.record_outcome("s1", "a.py", "reverted")   # +0.0

        confidence = db.get_decision_confidence(file_path="a.py")
        assert confidence["confidence"] == 0.5

    def test_confidence_with_pattern(self, project_env):
        """Test confidence filtered by file path pattern."""
        _, _, db = project_env
        db.log_session("s1", "S1", "1", [{"file_path": "src/api.py", "decision": "d1", "context": "c"}])
        db.record_outcome("s1", "src/api.py", "kept")
        db.record_outcome("s1", "src/api.py", "kept")

        confidence = db.get_decision_confidence(pattern="src/")
        assert confidence["confidence"] == 1.0
        assert confidence["total_decisions"] == 2


# ===========================================================================
# Developer Preferences Tests
# ===========================================================================

class TestPreferences:
    def test_record_and_retrieve_preference(self, project_env):
        _, _, db = project_env
        db.record_preference("naming", "Prefers snake_case", example="src/api.py")

        prefs = db.get_preferences(category="naming")
        assert len(prefs) == 1
        assert prefs[0]["signal"] == "Prefers snake_case"
        assert prefs[0]["frequency"] == 1

    def test_preference_frequency_increases(self, project_env):
        _, _, db = project_env
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("naming", "Prefers snake_case")

        prefs = db.get_preferences(category="naming")
        assert len(prefs) == 1
        assert prefs[0]["frequency"] == 3

    def test_preference_filter_by_min_frequency(self, project_env):
        _, _, db = project_env
        db.record_preference("naming", "Prefers snake_case")
        db.record_preference("structure", "Uses early returns")
        db.record_preference("structure", "Uses early returns")

        prefs = db.get_preferences(min_frequency=2)
        assert len(prefs) == 1
        assert prefs[0]["signal"] == "Uses early returns"

    def test_preference_source_field(self, project_env):
        _, _, db = project_env
        db.record_preference("naming", "camelCase", source="global")
        prefs = db.get_preferences(category="naming")
        assert prefs[0]["source"] == "global"


# ===========================================================================
# Learned Rules Tests
# ===========================================================================

class TestLearnedRules:
    def test_add_and_retrieve_rule(self, project_env):
        _, _, db = project_env
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

    def test_rules_filter_by_confidence(self, project_env):
        _, _, db = project_env
        db.add_learned_rule("Low confidence rule", 0.2, [], category="testing")
        db.add_learned_rule("High confidence rule", 0.9, [], category="testing")

        rules = db.get_learned_rules(min_confidence=0.5)
        assert len(rules) == 1
        assert rules[0]["rule_text"] == "High confidence rule"

    def test_update_rule_confidence(self, project_env):
        _, _, db = project_env
        db.add_learned_rule("A rule", 0.5, ["s1"], category="testing")

        rules = db.get_learned_rules()
        rule_id = rules[0]["id"]
        db.update_learned_rule(rule_id, confidence=0.9)

        updated = db.get_learned_rules()
        assert updated[0]["confidence"] == 0.9

    def test_update_rule_source_sessions(self, project_env):
        _, _, db = project_env
        db.add_learned_rule("A rule", 0.5, ["s1"], category="testing")

        rules = db.get_learned_rules()
        rule_id = rules[0]["id"]
        db.update_learned_rule(rule_id, source_sessions=["s1", "s2", "s3"])

        updated = db.get_learned_rules()
        sessions = json.loads(updated[0]["source_sessions"])
        assert sessions == ["s1", "s2", "s3"]


# ===========================================================================
# Project Maturity Tests
# ===========================================================================

class TestProjectMaturity:
    def test_empty_project_maturity(self, project_env):
        _, _, db = project_env
        maturity = db.get_project_maturity()
        assert maturity["session_count"] == 0
        assert maturity["coverage"] == 0.0
        assert maturity["overall_confidence"] == 0.0

    def test_maturity_with_sessions(self, project_env):
        _, _, db = project_env
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

    def test_maturity_includes_learned_rules_and_prefs(self, project_env):
        _, _, db = project_env
        db.add_learned_rule("rule1", 0.8, ["s1"], category="testing")
        db.add_learned_rule("rule2", 0.3, ["s1"], category="testing")  # below 0.5
        db.record_preference("naming", "snake_case")
        db.record_preference("naming", "snake_case")  # frequency=2

        maturity = db.get_project_maturity()
        assert maturity["learned_rules"] == 1  # only >= 0.5
        assert maturity["preference_signals"] == 1  # only freq >= 2


# ===========================================================================
# Graph Visualization Tests
# ===========================================================================

class TestGraphVisualization:
    def test_export_mermaid(self, project_env):
        from mcp_server.tools.graph import export_graph

        _, _, db = project_env
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

    def test_export_dot(self, project_env):
        from mcp_server.tools.graph import export_graph

        _, _, db = project_env
        db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="core")
        db.add_node("file:src/b.py", "file", "b.py", "src/b.py", layer="api")
        db.add_edge("file:src/a.py", "file:src/b.py", kind="imports")
        db.close()

        result = export_graph(format="dot")
        assert "digraph codevira" in result["output"]
        assert "->" in result["output"]


# ===========================================================================
# Session Helpers Tests
# ===========================================================================

class TestSessionHelpers:
    def test_get_recent_sessions(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "First", "1", [])
        db.log_session("s2", "Second", "2", [])

        recent = db.get_recent_sessions(limit=5)
        assert len(recent) == 2

    def test_get_recent_decisions(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "Test", "1", [
            {"file_path": "a.py", "decision": "Decision A", "context": "ctx"},
            {"file_path": "b.py", "decision": "Decision B", "context": "ctx"},
        ])

        decisions = db.get_recent_decisions(limit=5)
        assert len(decisions) == 2

    def test_log_session_replaces_on_duplicate(self, project_env):
        """Logging same session_id twice replaces the session row."""
        _, _, db = project_env
        db.log_session("s1", "First summary", "1", [])
        db.log_session("s1", "Updated summary", "2", [])

        sessions = db.get_recent_sessions()
        assert len(sessions) == 1
        assert sessions[0]["summary"] == "Updated summary"


# ===========================================================================
# get_node / update_node / list_file_nodes
# ===========================================================================

class TestNodeOperations:
    def test_get_node_existing(self, project_env):
        _, _, db = project_env
        db.add_node("file:src/main.py", "file", "main.py", "src/main.py",
                     role="entry point", layer="core", stability="high")
        node = db.get_node("file:src/main.py")
        assert node is not None
        assert node["name"] == "main.py"
        assert node["role"] == "entry point"
        assert node["layer"] == "core"
        assert node["stability"] == "high"

    def test_get_node_missing(self, project_env):
        _, _, db = project_env
        node = db.get_node("file:nonexistent.py")
        assert node is None

    def test_get_node_by_path(self, project_env):
        _, _, db = project_env
        db.add_node("file:src/util.py", "file", "util.py", "src/util.py")
        node = db.get_node_by_path("src/util.py")
        assert node is not None
        assert node["name"] == "util.py"

    def test_update_node_metadata(self, project_env):
        _, _, db = project_env
        db.add_node("file:src/api.py", "file", "api.py", "src/api.py")

        db.update_node_metadata("file:src/api.py",
                                role="REST API handler",
                                layer="api",
                                stability="high",
                                rules='["always validate input"]',
                                key_functions='["get_user", "create_order"]')

        node = db.get_node("file:src/api.py")
        assert node["role"] == "REST API handler"
        assert node["layer"] == "api"
        assert node["stability"] == "high"
        assert "always validate input" in node["rules"]
        assert "get_user" in node["key_functions"]

    def test_update_node_metadata_invalid_field_ignored(self, project_env):
        _, _, db = project_env
        db.add_node("file:x.py", "file", "x.py", "x.py")
        # Invalid field should be silently ignored
        db.update_node_metadata("file:x.py", invalid_field="ignored", role="ok")
        node = db.get_node("file:x.py")
        assert node["role"] == "ok"

    def test_list_file_nodes(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py", layer="core")
        db.add_node("file:b.py", "file", "b.py", "b.py", layer="api")
        db.add_node("file:c.py", "file", "c.py", "c.py", layer="core")
        db.add_node("func:a.py::main", "function", "main", "a.py")  # not a file node

        all_files = db.list_file_nodes()
        assert len(all_files) == 3

    def test_list_file_nodes_filter_by_layer(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py", layer="core")
        db.add_node("file:b.py", "file", "b.py", "b.py", layer="api")

        core_files = db.list_file_nodes(layer="core")
        assert len(core_files) == 1
        assert core_files[0]["id"] == "file:a.py"

    def test_list_file_nodes_filter_by_stability(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py", stability="high")
        db.add_node("file:b.py", "file", "b.py", "b.py", stability="low")

        high_files = db.list_file_nodes(stability="high")
        assert len(high_files) == 1
        assert high_files[0]["id"] == "file:a.py"

    def test_list_file_nodes_filter_do_not_revert(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py", do_not_revert=True)
        db.add_node("file:b.py", "file", "b.py", "b.py", do_not_revert=False)

        protected = db.list_file_nodes(do_not_revert=True)
        assert len(protected) == 1
        assert protected[0]["id"] == "file:a.py"

    def test_add_node_preserves_existing_metadata(self, project_env):
        """Re-adding a node should preserve existing metadata not in kwargs."""
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py", role="original", layer="core")

        # Re-add same node without role/layer; they should be preserved
        db.add_node("file:a.py", "file", "a.py", "a.py", stability="high")
        node = db.get_node("file:a.py")
        assert node["role"] == "original"
        assert node["layer"] == "core"
        assert node["stability"] == "high"


# ===========================================================================
# File hash tracking
# ===========================================================================

class TestFileHashTracking:
    def test_get_file_hash_none_for_new_file(self, project_env):
        _, _, db = project_env
        h = db.get_file_hash("src/new.py")
        assert h is None

    def test_update_and_get_file_hash(self, project_env):
        _, _, db = project_env
        db.update_file_hash("src/main.py", "abc123def456")
        h = db.get_file_hash("src/main.py")
        assert h == "abc123def456"

    def test_update_file_hash_replaces_existing(self, project_env):
        _, _, db = project_env
        db.update_file_hash("src/main.py", "hash_v1")
        db.update_file_hash("src/main.py", "hash_v2")
        h = db.get_file_hash("src/main.py")
        assert h == "hash_v2"


# ===========================================================================
# search_decisions
# ===========================================================================

class TestSearchDecisions:
    def test_search_finds_by_decision_text(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "API setup", "1", [
            {"file_path": "api.py", "decision": "Use REST endpoints", "context": "Design"},
        ])
        results = db.search_decisions("REST")
        assert len(results) >= 1
        assert any("REST" in r["decision"] for r in results)

    def test_search_finds_by_context(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "DB layer", "2", [
            {"file_path": "db.py", "decision": "Use SQLite", "context": "Performance optimization"},
        ])
        results = db.search_decisions("Performance")
        assert len(results) >= 1

    def test_search_finds_by_summary(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "Refactored authentication module", "3", [
            {"file_path": "auth.py", "decision": "Use JWT", "context": "Security"},
        ])
        results = db.search_decisions("authentication")
        assert len(results) >= 1

    def test_search_with_session_id_filter(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "Session 1", "1", [
            {"file_path": "a.py", "decision": "Decision A", "context": "ctx"},
        ])
        db.log_session("s2", "Session 2", "2", [
            {"file_path": "b.py", "decision": "Decision B about A topic", "context": "ctx"},
        ])

        results = db.search_decisions("Decision", session_id="s1")
        assert all(r.get("phase") == "1" for r in results)  # from session s1

    def test_search_with_limit(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "Lots", "1", [
            {"file_path": f"f{i}.py", "decision": f"Decision {i}", "context": "ctx"}
            for i in range(20)
        ])
        results = db.search_decisions("Decision", limit=5)
        assert len(results) == 5

    def test_search_no_results(self, project_env):
        _, _, db = project_env
        results = db.search_decisions("nonexistent_query_xyz")
        assert results == []


# ===========================================================================
# transaction() context manager
# ===========================================================================

class TestTransactionContextManager:
    def test_transaction_commits_on_success(self, project_env):
        _, _, db = project_env
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO nodes (id, kind, name, file_path) VALUES (?, ?, ?, ?)",
                ("file:tx_test.py", "file", "tx_test.py", "tx_test.py"),
            )
        node = db.get_node("file:tx_test.py")
        assert node is not None

    def test_transaction_rollback_on_error(self, project_env):
        _, _, db = project_env
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO nodes (id, kind, name, file_path) VALUES (?, ?, ?, ?)",
                    ("file:rollback.py", "file", "rollback.py", "rollback.py"),
                )
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # The row should NOT have been committed
        node = db.get_node("file:rollback.py")
        assert node is None


# ===========================================================================
# Edge Case Tests (ported)
# ===========================================================================

class TestEdgeCases:
    def test_add_edge_idempotent(self, project_env):
        """Adding the same edge twice should not crash (INSERT OR REPLACE)."""
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:a.py", "file:b.py", kind="imports")  # duplicate
        assert len(db.get_edges_from("file:a.py")) == 1

    def test_remove_edges_nonexistent_node(self, project_env):
        """Removing edges for a node that has none should not crash."""
        _, _, db = project_env
        db.remove_edges_for_node("file:nonexistent.py")

    def test_confidence_with_only_reverts(self, project_env):
        """All reverted outcomes should yield 0.0 confidence."""
        _, _, db = project_env
        db.log_session("s1", "S1", "1", [{"file_path": "a.py", "decision": "d1", "context": "c"}])
        db.record_outcome("s1", "a.py", "reverted")
        db.record_outcome("s1", "a.py", "reverted")
        confidence = db.get_decision_confidence(file_path="a.py")
        assert confidence["confidence"] == 0.0

    def test_preference_with_none_example(self, project_env):
        """Recording a preference with no example should work."""
        _, _, db = project_env
        db.record_preference("naming", "Uses camelCase", example=None)
        prefs = db.get_preferences()
        assert len(prefs) == 1
        assert prefs[0]["example"] is None

    def test_blast_radius_no_edges(self, project_env):
        """Blast radius on a node with no edges should return empty."""
        _, _, db = project_env
        db.add_node("file:isolated.py", "file", "isolated.py", "isolated.py")
        blast = db.get_blast_radius("file:isolated.py")
        assert blast == []

    def test_maturity_no_files(self, project_env):
        """Maturity with zero files should not divide by zero."""
        _, _, db = project_env
        maturity = db.get_project_maturity()
        assert maturity["coverage"] == 0.0
        assert maturity["total_files"] == 0

    def test_learned_rule_empty_file_pattern(self, project_env):
        """Rules with no file_pattern should still be retrievable."""
        _, _, db = project_env
        db.add_learned_rule("General rule", 0.7, [], category="patterns", file_pattern=None)
        rules = db.get_learned_rules(category="patterns")
        assert len(rules) == 1


# ===========================================================================
# Symbol & Call Graph Tests
# ===========================================================================

class TestSymbolsAndCallGraph:
    def test_add_and_get_symbol(self, project_env):
        _, _, db = project_env
        db.add_node("file:src/api.py", "file", "api.py", "src/api.py")
        db.add_symbol(
            "file:src/api.py::get_user", "file:src/api.py", "get_user", "function",
            signature="def get_user(user_id: int) -> dict",
            start_line=10, end_line=25, is_public=True,
        )
        symbols = db.get_symbols_for_file("file:src/api.py")
        assert len(symbols) == 1
        assert symbols[0]["name"] == "get_user"

    def test_find_symbol_by_name(self, project_env):
        _, _, db = project_env
        db.add_node("file:x.py", "file", "x.py", "x.py")
        db.add_symbol("file:x.py::foo", "file:x.py", "foo", "function")

        sym = db.find_symbol("foo")
        assert sym is not None
        assert sym["name"] == "foo"

    def test_find_symbol_missing(self, project_env):
        _, _, db = project_env
        sym = db.find_symbol("nonexistent_function")
        assert sym is None

    def test_call_edges(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_symbol("file:a.py::caller", "file:a.py", "caller", "function")
        db.add_symbol("file:a.py::callee", "file:a.py", "callee", "function")
        db.add_call_edge("file:a.py::caller", "file:a.py::callee", line=15)

        callers = db.get_callers("file:a.py::callee")
        assert len(callers) == 1
        assert callers[0]["name"] == "caller"

        callees = db.get_callees("file:a.py::caller")
        assert len(callees) == 1
        assert callees[0]["name"] == "callee"

    def test_remove_symbols_for_file(self, project_env):
        _, _, db = project_env
        db.add_node("file:x.py", "file", "x.py", "x.py")
        db.add_symbol("file:x.py::fn1", "file:x.py", "fn1", "function")
        db.add_symbol("file:x.py::fn2", "file:x.py", "fn2", "function")

        db.remove_symbols_for_file("file:x.py")
        assert db.get_symbols_for_file("file:x.py") == []

    def test_symbol_count(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_symbol("file:a.py::f1", "file:a.py", "f1", "function")
        db.add_symbol("file:a.py::f2", "file:a.py", "f2", "function")
        assert db.get_symbol_count() == 2

    def test_call_edge_count(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_symbol("file:a.py::f1", "file:a.py", "f1", "function")
        db.add_symbol("file:a.py::f2", "file:a.py", "f2", "function")
        db.add_call_edge("file:a.py::f1", "file:a.py::f2")
        assert db.get_call_edge_count() == 1

    def test_find_hotspot_functions(self, project_env):
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_symbol("file:a.py::big", "file:a.py", "big", "function",
                       start_line=1, end_line=100)
        db.add_symbol("file:a.py::small", "file:a.py", "small", "function",
                       start_line=1, end_line=10)

        hotspots = db.find_hotspot_functions(min_lines=50)
        assert len(hotspots) == 1
        assert hotspots[0]["name"] == "big"


# ===========================================================================
# Chaos Tests
# ===========================================================================

class TestChaos:

    def test_concurrent_reads_and_writes(self, project_env):
        """Sequential rapid reads and writes should not corrupt DB.

        Note: SQLite with check_same_thread=False can segfault in CPython
        when truly concurrent threads hit the same connection, so we test
        sequential rapid interleaving instead of parallel threads.
        """
        _, _, db = project_env

        for idx in range(20):
            db.add_node(f"file:concurrent_{idx}.py", "file",
                        f"concurrent_{idx}.py", f"concurrent_{idx}.py")
            db.log_session(f"s-{idx}", f"Session {idx}", "1", [
                {"file_path": f"concurrent_{idx}.py",
                 "decision": f"Decision {idx}", "context": "test"}
            ])
            # Interleave reads
            db.get_recent_sessions()
            db.list_file_nodes()

        # DB should still be functional
        nodes = db.list_file_nodes()
        assert len(nodes) == 20
        sessions = db.get_recent_sessions(limit=25)
        assert len(sessions) == 20

    def test_large_node_count(self, project_env):
        """Adding 100+ nodes should work without issues."""
        _, _, db = project_env
        for i in range(150):
            db.add_node(f"file:bulk_{i:03d}.py", "file",
                        f"bulk_{i:03d}.py", f"bulk_{i:03d}.py",
                        layer="bulk", stability="medium")

        nodes = db.list_file_nodes()
        assert len(nodes) == 150

    def test_unicode_in_node_names_and_decisions(self, project_env):
        """Unicode characters in names, decisions, and rules should work."""
        _, _, db = project_env
        db.add_node("file:caf\u00e9.py", "file", "caf\u00e9.py", "caf\u00e9.py",
                     role="\u65e5\u672c\u8a9e\u306e\u5f79\u5272",
                     rules='["\u4f7f\u7528\u898f\u5247"]')

        node = db.get_node("file:caf\u00e9.py")
        assert node is not None
        assert node["name"] == "caf\u00e9.py"
        assert "\u65e5\u672c\u8a9e" in node["role"]

        db.log_session("s-unicode", "\u00dcbersicht der Sitzung", "1", [
            {"file_path": "caf\u00e9.py",
             "decision": "Verwende Uml\u00e4ute: \u00e4\u00f6\u00fc\u00df",
             "context": "\u4e2d\u6587\u4e0a\u4e0b\u6587"}
        ])
        results = db.search_decisions("Uml\u00e4ute")
        assert len(results) >= 1

    def test_delete_edges_for_nonexistent_node_no_crash(self, project_env):
        """Removing edges for a node that does not exist should not crash."""
        _, _, db = project_env
        db.remove_edges_for_node("file:totally_fake.py")
        # No exception = pass

    def test_blast_radius_disconnected_graph_components(self, project_env):
        """Blast radius on disconnected components should only return connected nodes."""
        _, _, db = project_env
        # Component 1: a -> b -> c
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")

        # Component 2: x -> y (disconnected)
        db.add_node("file:x.py", "file", "x.py", "x.py")
        db.add_node("file:y.py", "file", "y.py", "y.py")
        db.add_edge("file:x.py", "file:y.py", kind="imports")

        blast = db.get_blast_radius("file:c.py", max_depth=5)
        affected_ids = {r["id"] for r in blast}

        # a and b depend on c
        assert "file:b.py" in affected_ids
        assert "file:a.py" in affected_ids
        # x and y are disconnected; should NOT be in blast radius
        assert "file:x.py" not in affected_ids
        assert "file:y.py" not in affected_ids

    def test_blast_radius_with_cycle(self, project_env):
        """Blast radius should handle cycles without infinite loop."""
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_node("file:c.py", "file", "c.py", "c.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:b.py", "file:c.py", kind="imports")
        db.add_edge("file:c.py", "file:a.py", kind="imports")  # cycle

        blast = db.get_blast_radius("file:c.py", max_depth=5)
        # Should not hang; returns finite result
        assert isinstance(blast, list)

    def test_very_long_decision_text(self, project_env):
        """Very long decision text should not cause issues."""
        _, _, db = project_env
        long_text = "x" * 50_000
        db.log_session("s-long", "Long session", "1", [
            {"file_path": "f.py", "decision": long_text, "context": "test"}
        ])
        results = db.search_decisions("xxx", limit=1)
        assert len(results) >= 1

    def test_empty_session_decisions(self, project_env):
        """Logging a session with zero decisions should work."""
        _, _, db = project_env
        db.log_session("s-empty", "Empty session", "1", [])
        sessions = db.get_recent_sessions()
        assert any(s["session_id"] == "s-empty" for s in sessions)

    def test_multiple_edge_kinds_same_pair(self, project_env):
        """Two different edge kinds between same nodes should coexist."""
        _, _, db = project_env
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_edge("file:a.py", "file:b.py", kind="imports")
        db.add_edge("file:a.py", "file:b.py", kind="tests")

        edges = db.get_edges_from("file:a.py")
        assert len(edges) == 2
        kinds = {e["kind"] for e in edges}
        assert kinds == {"imports", "tests"}


# ===========================================================================
# v1.8 Change 2 — smarter search_decisions ranking
# ===========================================================================

from indexer.sqlite_graph import _is_duplicate  # noqa: E402  — v1.8 dedup helper


class TestSearchDecisionsRanking:
    """Rank tiers: file_path (0) > decision (1) > context (2) > summary-only (3)."""

    def test_ranking_file_path_match_wins(self, project_env):
        """A decision whose file_path matches the query ranks above a decision
        whose only match is in the decision text."""
        _, _, db = project_env

        # Oldest = file_path match — should still win despite being oldest.
        db.log_session("s-old", "summary one", "1", [
            {"file_path": "src/auth.py", "decision": "Refactor login flow",
             "context": "misc"},
        ])
        db.log_session("s-mid", "summary two", "1", [
            {"file_path": "src/user.py",
             "decision": "Improve validation in auth module",
             "context": "misc"},
        ])
        db.log_session("s-new", "summary three", "1", [
            {"file_path": "src/db.py",
             "decision": "Refactor DB pool", "context": "auth stuff here"},
        ])

        results = db.search_decisions("auth", limit=10)
        # First hit must be the file_path match.
        assert results[0]["file_path"] == "src/auth.py"

    def test_ranking_decision_text_beats_context(self, project_env):
        """Within non-file tier, a decision-text match outranks a context-only
        match (even when the context match is newer)."""
        _, _, db = project_env

        # Older session: decision text matches "cache"
        db.log_session("s-old", "summary", "1", [
            {"file_path": "a.py", "decision": "Add cache layer here",
             "context": "unrelated"},
        ])
        # Newer session: only context matches "cache"
        db.log_session("s-new", "summary", "1", [
            {"file_path": "b.py", "decision": "Unrelated change",
             "context": "something about cache"},
        ])

        results = db.search_decisions("cache", limit=10)
        assert results[0]["decision"] == "Add cache layer here"

    def test_ranking_recency_within_same_tier(self, project_env):
        """Two decisions matching at the same tier → newest first."""
        _, _, db = project_env
        # Both match by decision text (tier 1). Force distinct created_at
        # (SQLite CURRENT_TIMESTAMP resolution is 1s — same-second inserts
        # would tie-break unpredictably).
        db.log_session("s1", "summary", "1", [
            {"file_path": "a.py", "decision": "Refactor old flow",
             "context": "ctx"},
        ])
        db.log_session("s2", "summary", "1", [
            {"file_path": "b.py", "decision": "Refactor new flow",
             "context": "ctx"},
        ])
        db.conn.execute(
            "UPDATE decisions SET created_at='2026-01-01 10:00:00' "
            "WHERE decision='Refactor old flow'"
        )
        db.conn.execute(
            "UPDATE decisions SET created_at='2026-04-22 10:00:00' "
            "WHERE decision='Refactor new flow'"
        )
        db.conn.commit()

        results = db.search_decisions("Refactor", limit=10)
        # Same tier — newest first.
        assert results[0]["decision"] == "Refactor new flow"

    def test_ranking_null_file_path_still_returned(self, project_env):
        """A decision with NULL file_path is still findable via decision text."""
        _, _, db = project_env
        db.log_session("s1", "summary", "1", [
            {"file_path": None, "decision": "Free-floating note about caching",
             "context": None},
        ])
        results = db.search_decisions("caching", limit=10)
        assert len(results) == 1
        assert results[0]["file_path"] is None

    def test_ranking_file_path_in_where_clause(self, project_env):
        """File_path matches are findable even when decision text doesn't mention the query."""
        _, _, db = project_env
        db.log_session("s1", "summary", "1", [
            {"file_path": "src/payments/stripe_gateway.py",
             "decision": "Minor cleanup",
             "context": "Trivial edit"},
        ])
        # Query only matches file_path
        results = db.search_decisions("stripe_gateway", limit=10)
        assert len(results) == 1
        assert results[0]["file_path"] == "src/payments/stripe_gateway.py"

    def test_ranking_session_id_filter_still_applies(self, project_env):
        """session_id filter must still work with the new ranking SQL."""
        _, _, db = project_env
        db.log_session("s-keep", "summary", "1", [
            {"file_path": "a.py", "decision": "keep me cache", "context": "x"},
        ])
        db.log_session("s-other", "summary", "1", [
            {"file_path": "b.py", "decision": "exclude cache", "context": "y"},
        ])
        results = db.search_decisions("cache", limit=10, session_id="s-keep")
        assert len(results) == 1
        assert results[0]["decision"] == "keep me cache"


# ===========================================================================
# v1.8 Change 3 — decision dedup in log_session + _is_duplicate helper
# ===========================================================================


class TestIsDuplicateHelper:
    """Unit tests for the pure _is_duplicate() token-overlap function."""

    def test_exact_duplicate(self):
        assert _is_duplicate("add validation layer here",
                             ["add validation layer here"]) is True

    def test_high_overlap_above_threshold(self):
        # 4 shared / 5 max = 0.8 → triggers at threshold 0.8
        assert _is_duplicate("add validation layer for inputs",
                             ["add validation layer for forms"]) is True

    def test_low_overlap_below_threshold(self):
        # Very little overlap
        assert _is_duplicate("rewrite the auth middleware pipeline",
                             ["add tests for the user module"]) is False

    def test_empty_existing_list(self):
        assert _is_duplicate("any decision text here", []) is False

    def test_short_decision_never_dedups(self):
        # < 3 tokens → always False
        assert _is_duplicate("fix bug", ["fix bug"]) is False

    def test_empty_existing_decision_skipped(self):
        # One empty string in existing list must be skipped, not crash
        assert _is_duplicate("fix the auth bug properly", ["", "irrelevant here"]) is False

    def test_threshold_boundary(self):
        # Tunable threshold: lowering should accept more as duplicate
        assert _is_duplicate("a b c", ["a b d"], threshold=0.6) is True
        assert _is_duplicate("a b c", ["a b d"], threshold=0.9) is False


class TestDedupInLogSession:
    """Change 3: log_session skips token-overlap duplicates per file."""

    def test_dedup_exact_duplicate_skipped(self, project_env):
        _, _, db = project_env
        payload = [{"file_path": "src/auth.py",
                    "decision": "Switch to PBKDF2 password hashing",
                    "context": "security"}]
        db.log_session("s1", "first", "1", payload)
        db.log_session("s2", "second", "1", payload)
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path='src/auth.py'"
        ).fetchone()
        assert rows["c"] == 1

    def test_dedup_high_overlap_skipped(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "first", "1", [{
            "file_path": "src/x.py",
            "decision": "add validation layer for inputs",
            "context": "",
        }])
        db.log_session("s2", "second", "1", [{
            "file_path": "src/x.py",
            "decision": "add validation layer for forms",
            "context": "",
        }])
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path='src/x.py'"
        ).fetchone()
        assert rows["c"] == 1

    def test_dedup_low_overlap_kept(self, project_env):
        _, _, db = project_env
        db.log_session("s1", "first", "1", [{
            "file_path": "src/x.py",
            "decision": "rewrite the authentication middleware pipeline",
            "context": "",
        }])
        db.log_session("s2", "second", "1", [{
            "file_path": "src/x.py",
            "decision": "drop unused imports and format file",
            "context": "",
        }])
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path='src/x.py'"
        ).fetchone()
        assert rows["c"] == 2

    def test_dedup_different_file_both_kept(self, project_env):
        """Same decision text, different file_path → both kept."""
        _, _, db = project_env
        text = "Switch to PBKDF2 password hashing"
        db.log_session("s1", "first", "1", [
            {"file_path": "src/auth.py", "decision": text, "context": "x"},
        ])
        db.log_session("s2", "second", "1", [
            {"file_path": "src/user.py", "decision": text, "context": "x"},
        ])
        rows = db.conn.execute("SELECT COUNT(*) AS c FROM decisions").fetchone()
        assert rows["c"] == 2

    def test_dedup_no_file_path_never_deduped(self, project_env):
        """Decisions without file_path are always inserted."""
        _, _, db = project_env
        payload = [{"file_path": None,
                    "decision": "Generic note about refactoring everything",
                    "context": "none"}]
        db.log_session("s1", "first", "1", payload)
        db.log_session("s2", "second", "1", payload)
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path IS NULL"
        ).fetchone()
        assert rows["c"] == 2

    def test_dedup_short_decision_never_deduped(self, project_env):
        """Decisions < 3 tokens always insert (even exact repeats)."""
        _, _, db = project_env
        payload = [{"file_path": "src/x.py", "decision": "fix bug",
                    "context": "short"}]
        db.log_session("s1", "first", "1", payload)
        db.log_session("s2", "second", "1", payload)
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path='src/x.py'"
        ).fetchone()
        assert rows["c"] == 2

    def test_dedup_first_decision_always_stored(self, project_env):
        """Nothing to compare against → first decision always persists."""
        _, _, db = project_env
        db.log_session("s1", "first", "1", [
            {"file_path": "src/new.py", "decision": "add initial feature flag",
             "context": "first ever"},
        ])
        rows = db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE file_path='src/new.py'"
        ).fetchone()
        assert rows["c"] == 1

    def test_dedup_session_row_always_created(self, project_env):
        """Session row should exist even when all decisions are skipped."""
        _, _, db = project_env
        payload = [{"file_path": "src/x.py",
                    "decision": "add validation layer for inputs",
                    "context": ""}]
        db.log_session("s1", "first", "1", payload)
        db.log_session("s2", "second", "1", payload)  # all decisions dedup'd

        sessions = db.get_recent_sessions(limit=5)
        ids = {s["session_id"] for s in sessions}
        assert "s1" in ids
        assert "s2" in ids
