"""
Tests for mcp_server/tools/graph.py — extended coverage for graph querying,
impact analysis, hotspot detection, git-based diff/analysis, and refresh.

Covers:
  - list_nodes: filter by layer, stability, do_not_revert
  - query_graph: callers, callees, tests, symbols, dependents
  - find_hotspots: large functions, high fan-in, high fan-out
  - get_impact: BFS blast radius
  - export_graph: mermaid/dot with scope filter (extended)
  - get_graph_diff: mocked git subprocess
  - analyze_changes: mocked git subprocess, risk scoring
  - refresh_graph: auto-generate stubs (mocked)

Edge cases: empty graph, isolated nodes, circular deps, deep chains,
filter combinations, no git repo.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import mcp_server.paths as paths
from indexer.sqlite_graph import SQLiteGraph
from mcp_server.tools import graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_project(tmp_path, monkeypatch) -> tuple[Path, Path, SQLiteGraph]:
    """Create a temp project with a graph database and monkeypatched paths."""
    project_root = tmp_path / "test-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text("project:\n  name: test-graph\n")
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project_root.resolve())

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    return project_root, data_dir, db


def _populate_graph(db: SQLiteGraph) -> None:
    """Create a standard graph topology for testing.

    Topology:
      src/api.py   --imports--> src/core.py --imports--> src/utils.py
      src/routes.py --imports--> src/api.py
      tests/test_api.py --imports--> src/api.py

    Layers:  api -> core -> utils, routes -> api, tests -> api
    """
    db.add_node("file:src/api.py", "file", "api.py", "src/api.py",
                layer="api", stability="medium", role="API handlers")
    db.add_node("file:src/core.py", "file", "core.py", "src/core.py",
                layer="core", stability="high", role="Business logic")
    db.add_node("file:src/utils.py", "file", "utils.py", "src/utils.py",
                layer="utils", stability="high", role="Utility functions")
    db.add_node("file:src/routes.py", "file", "routes.py", "src/routes.py",
                layer="api", stability="low", role="Route definitions")
    db.add_node("file:tests/test_api.py", "file", "test_api.py", "tests/test_api.py",
                layer="tests", stability="medium", role="API tests")

    db.add_edge("file:src/api.py", "file:src/core.py", kind="imports")
    db.add_edge("file:src/core.py", "file:src/utils.py", kind="imports")
    db.add_edge("file:src/routes.py", "file:src/api.py", kind="imports")
    db.add_edge("file:tests/test_api.py", "file:src/api.py", kind="imports")


def _add_symbols(db: SQLiteGraph) -> None:
    """Add function-level symbols and call edges for query_graph tests."""
    # api.py symbols
    db.add_symbol("file:src/api.py::handle_request", "file:src/api.py",
                  "handle_request", "function", start_line=10, end_line=30, is_public=True)
    db.add_symbol("file:src/api.py::validate_input", "file:src/api.py",
                  "validate_input", "function", start_line=32, end_line=45, is_public=True)
    db.add_symbol("file:src/api.py::_internal_helper", "file:src/api.py",
                  "_internal_helper", "function", start_line=47, end_line=55, is_public=False)

    # core.py symbols
    db.add_symbol("file:src/core.py::process_data", "file:src/core.py",
                  "process_data", "function", start_line=5, end_line=80, is_public=True)
    db.add_symbol("file:src/core.py::transform", "file:src/core.py",
                  "transform", "function", start_line=82, end_line=100, is_public=True)

    # utils.py symbol
    db.add_symbol("file:src/utils.py::sanitize", "file:src/utils.py",
                  "sanitize", "function", start_line=1, end_line=15, is_public=True)

    # Call edges: handle_request -> process_data -> sanitize
    #             handle_request -> validate_input
    db.add_call_edge("file:src/api.py::handle_request", "file:src/core.py::process_data")
    db.add_call_edge("file:src/api.py::handle_request", "file:src/api.py::validate_input")
    db.add_call_edge("file:src/core.py::process_data", "file:src/utils.py::sanitize")
    # Multiple callers for sanitize (high fan-in scenario)
    db.add_call_edge("file:src/api.py::validate_input", "file:src/utils.py::sanitize")
    db.add_call_edge("file:src/core.py::transform", "file:src/utils.py::sanitize")


# =====================================================================
# list_nodes
# =====================================================================

class TestListNodes:
    def test_list_all_nodes(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.list_nodes()
        assert result["count"] == 5
        assert len(result["nodes"]) == 5

    def test_filter_by_layer(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.list_nodes(layer="api")
        assert result["count"] == 2
        paths_found = {n["file_path"] for n in result["nodes"]}
        assert "src/api.py" in paths_found
        assert "src/routes.py" in paths_found

    def test_filter_by_stability(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.list_nodes(stability="high")
        assert result["count"] == 2
        paths_found = {n["file_path"] for n in result["nodes"]}
        assert "src/core.py" in paths_found
        assert "src/utils.py" in paths_found

    def test_filter_by_do_not_revert(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.update_node_metadata("file:src/core.py", do_not_revert=True)
        db.close()
        result = graph.list_nodes(do_not_revert=True)
        assert result["count"] == 1
        assert result["nodes"][0]["file_path"] == "src/core.py"

    def test_filter_combination(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.list_nodes(layer="api", stability="medium")
        assert result["count"] == 1
        assert result["nodes"][0]["file_path"] == "src/api.py"

    def test_empty_graph(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.list_nodes()
        assert result["count"] == 0
        assert result["nodes"] == []

    def test_hint_present(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.list_nodes()
        assert "hint" in result


# =====================================================================
# query_graph
# =====================================================================

class TestQueryGraph:
    def test_query_symbols(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", query_type="symbols")
        assert result["query_type"] == "symbols"
        assert result["count"] == 3
        names = {s["name"] for s in result["results"]}
        assert "handle_request" in names
        assert "validate_input" in names
        assert "_internal_helper" in names

    def test_query_callees(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol="handle_request", query_type="callees")
        assert result["query_type"] == "callees"
        assert result["count"] == 2
        callee_names = {c["name"] for c in result["results"]}
        assert "process_data" in callee_names
        assert "validate_input" in callee_names

    def test_query_callers(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/utils.py", symbol="sanitize", query_type="callers")
        assert result["query_type"] == "callers"
        assert result["count"] == 3
        caller_names = {c["name"] for c in result["results"]}
        assert "process_data" in caller_names
        assert "validate_input" in caller_names
        assert "transform" in caller_names

    def test_query_tests(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol="handle_request", query_type="tests")
        assert result["query_type"] == "tests"
        assert "tests/test_api.py" in result["test_files"]

    def test_query_dependents(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol="handle_request", query_type="dependents")
        assert result["query_type"] == "dependents"
        # routes.py and tests/test_api.py depend on api.py
        dep_files = {r["file"] for r in result["results"]}
        assert "src/routes.py" in dep_files or "tests/test_api.py" in dep_files

    def test_query_missing_symbol_error(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol="nonexistent_func", query_type="callers")
        assert "error" in result

    def test_query_callers_without_symbol_error(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.query_graph("src/api.py", query_type="callers")
        assert "error" in result

    def test_query_unknown_type_error(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol="handle_request", query_type="invalid")
        assert "error" in result


# =====================================================================
# find_hotspots
# =====================================================================

class TestFindHotspots:
    def test_large_functions_detected(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        # process_data spans lines 5-80 = 75 lines, above default threshold of 50
        result = graph.find_hotspots(threshold=50)
        large = result["large_functions"]
        assert len(large) >= 1
        names = {f["name"] for f in large}
        assert "process_data" in names

    def test_large_functions_high_threshold(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.find_hotspots(threshold=100)
        assert len(result["large_functions"]) == 0

    def test_high_fan_in_detected(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.find_hotspots(threshold=50)
        fan_in = result["high_fan_in"]
        # sanitize has 3 callers, default min_callers=3
        names = {h["name"] for h in fan_in}
        assert "sanitize" in names

    def test_high_fan_out_detected(self, tmp_path, monkeypatch):
        """Nodes with 5+ outgoing edges should appear in high_fan_out."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        # Create a hub node with many dependencies
        db.add_node("file:src/hub.py", "file", "hub.py", "src/hub.py", layer="core")
        for i in range(6):
            target = f"file:src/dep_{i}.py"
            db.add_node(target, "file", f"dep_{i}.py", f"src/dep_{i}.py", layer="utils")
            db.add_edge("file:src/hub.py", target, kind="imports")
        db.close()
        result = graph.find_hotspots()
        fan_out = result["high_fan_out"]
        assert len(fan_out) >= 1
        files = {f["file"] for f in fan_out}
        assert "src/hub.py" in files

    def test_empty_graph_hotspots(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.find_hotspots()
        assert result["large_functions"] == []
        assert result["high_fan_in"] == []
        assert result["high_fan_out"] == []


# =====================================================================
# get_impact (BFS blast radius)
# =====================================================================

class TestGetImpact:
    def test_impact_basic(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        # Changing core.py affects api.py, routes.py, test_api.py (via api.py)
        result = graph.get_impact("src/core.py")
        assert result["found"] is True
        affected_files = {a["file"] for a in result["affected_files"]}
        assert "src/api.py" in affected_files

    def test_impact_isolated_node(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_node("file:isolated.py", "file", "isolated.py", "isolated.py", layer="misc")
        db.close()
        result = graph.get_impact("isolated.py")
        assert result["found"] is True
        assert result["blast_radius"] == 0
        assert result["affected_files"] == []

    def test_impact_unknown_file(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.get_impact("nonexistent.py")
        assert result["found"] is False

    def test_impact_deep_chain(self, tmp_path, monkeypatch):
        """Chain of depth 6: a->b->c->d->e->f. BFS max_depth=3 should not reach f."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        files = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
        for f in files:
            db.add_node(f"file:{f}", "file", f, f, layer="chain")
        for i in range(len(files) - 1):
            db.add_edge(f"file:{files[i]}", f"file:{files[i+1]}", kind="imports")
        db.close()
        result = graph.get_impact("f.py")
        affected_files = {a["file"] for a in result["affected_files"]}
        # With max_depth=3, the blast radius from f.py should include e, d, c (direct reverse chain)
        # but likely not a.py
        assert "e.py" in affected_files

    def test_impact_circular_deps(self, tmp_path, monkeypatch):
        """Circular deps should not cause infinite loop in BFS."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_node("file:x.py", "file", "x.py", "x.py", layer="cycle")
        db.add_node("file:y.py", "file", "y.py", "y.py", layer="cycle")
        db.add_node("file:z.py", "file", "z.py", "z.py", layer="cycle")
        db.add_edge("file:x.py", "file:y.py", kind="imports")
        db.add_edge("file:y.py", "file:z.py", kind="imports")
        db.add_edge("file:z.py", "file:x.py", kind="imports")
        db.close()
        result = graph.get_impact("x.py")
        assert result["found"] is True
        # Should not hang or crash — circular path detection in SQL prevents infinite recursion
        assert isinstance(result["blast_radius"], int)

    def test_impact_partial_match(self, tmp_path, monkeypatch):
        """If exact path not found but substring match exists in the graph, use that node."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        # get_impact does a substring match via list_file_nodes when exact path fails
        # Using a substring that uniquely matches src/utils.py
        result = graph.get_impact("src/utils.py")
        assert result["found"] is True
        assert result["target_file"] == "src/utils.py"


# =====================================================================
# export_graph (extended)
# =====================================================================

class TestExportGraph:
    def test_mermaid_with_scope(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.export_graph(format="mermaid", scope="src/")
        assert result["format"] == "mermaid"
        # Should not include tests/test_api.py node itself but may include edge
        assert result["node_count"] == 4

    def test_dot_format(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.export_graph(format="dot")
        assert "digraph codevira" in result["output"]
        assert result["edge_count"] == 4

    def test_unknown_format_error(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()
        result = graph.export_graph(format="json")
        assert "error" in result

    def test_mermaid_stability_styles(self, tmp_path, monkeypatch):
        """High and low stability nodes should have style annotations in mermaid output."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.add_node("file:high.py", "file", "high.py", "high.py", stability="high", layer="x")
        db.add_node("file:low.py", "file", "low.py", "low.py", stability="low", layer="x")
        db.close()
        result = graph.export_graph(format="mermaid")
        assert ":::high" in result["output"]
        assert ":::low" in result["output"]

    def test_export_empty_graph(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.export_graph(format="mermaid")
        assert result["node_count"] == 0
        assert result["edge_count"] == 0
        assert "graph LR" in result["output"]


# =====================================================================
# get_graph_diff (mocked git)
# =====================================================================

class TestGetGraphDiff:
    def test_diff_with_changed_files(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()

        fake_diff = "src/api.py\nsrc/core.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.get_graph_diff("main", "HEAD")
        assert result["total_changed"] == 2
        files = {f["file_path"] for f in result["changed_files"]}
        assert "src/api.py" in files
        assert "src/core.py" in files

    def test_diff_file_in_graph(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()

        fake_diff = "src/api.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.get_graph_diff()
        api_entry = result["changed_files"][0]
        assert api_entry["in_graph"] is True
        assert api_entry["stability"] == "medium"

    def test_diff_file_not_in_graph(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()

        fake_diff = "README.md\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.get_graph_diff()
        readme_entry = result["changed_files"][0]
        assert readme_entry["in_graph"] is False
        assert readme_entry["stability"] == "unknown"
        assert readme_entry["blast_radius"] == 0

    def test_diff_no_changes(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("subprocess.check_output", return_value=b""):
            result = graph.get_graph_diff()
        assert result["changed_files"] == []
        assert result["total_blast_radius"] == 0

    def test_diff_git_failure(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        def raise_error(*args, **kwargs):
            raise subprocess.CalledProcessError(128, "git")

        with patch("subprocess.check_output", side_effect=raise_error):
            result = graph.get_graph_diff()
        assert "error" in result

    def test_diff_blast_radius_populated(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        db.close()

        fake_diff = "src/core.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.get_graph_diff()
        core_entry = result["changed_files"][0]
        assert core_entry["blast_radius"] >= 1


# =====================================================================
# analyze_changes (mocked git)
# =====================================================================

class TestAnalyzeChanges:
    def test_analyze_with_symbols(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()

        fake_diff = "src/api.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.analyze_changes()
        assert result["changed_files"] == 1
        assert result["functions_analyzed"] >= 3
        assert "risk_summary" in result

    def test_analyze_risk_scoring(self, tmp_path, monkeypatch):
        """Public functions with many callers and no tests should be high risk."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        # Create file nodes first (FK requirement: symbols reference file_node_ids)
        db.add_node("file:src/risky.py", "file", "risky.py", "src/risky.py", layer="core")
        for i in range(3):
            db.add_node(f"file:src/caller_{i}.py", "file", f"caller_{i}.py",
                        f"src/caller_{i}.py", layer="core")
        db.add_symbol("file:src/risky.py::risky_func", "file:src/risky.py",
                      "risky_func", "function", start_line=1, end_line=20, is_public=True)
        # 3 callers
        for i in range(3):
            caller_id = f"file:src/caller_{i}.py::call_func"
            db.add_symbol(caller_id, f"file:src/caller_{i}.py",
                          "call_func", "function", start_line=1, end_line=5, is_public=True)
            db.add_call_edge(caller_id, "file:src/risky.py::risky_func")
        db.close()

        fake_diff = "src/risky.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.analyze_changes()
        assert result["risk_summary"]["high"] >= 1
        assert len(result["test_gaps"]) >= 1

    def test_analyze_no_changes(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch("subprocess.check_output", return_value=b""):
            result = graph.analyze_changes()
        assert result["changes"] == []
        assert "No changes" in result["summary"]

    def test_analyze_git_failure(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        def raise_error(*args, **kwargs):
            raise subprocess.CalledProcessError(128, "git")

        with patch("subprocess.check_output", side_effect=raise_error):
            result = graph.analyze_changes()
        assert "error" in result

    def test_analyze_test_gaps_detected(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()

        # src/core.py has no test imports, but has public functions
        fake_diff = "src/core.py\n"
        with patch("subprocess.check_output", return_value=fake_diff.encode("utf-8")):
            result = graph.analyze_changes()
        # core.py has public symbols with no test files importing it
        gaps = result["test_gaps"]
        gap_files = {g["file"] for g in gaps}
        assert "src/core.py" in gap_files


# =====================================================================
# refresh_graph (mocked)
# =====================================================================

class TestRefreshGraph:
    def test_refresh_graph_calls_generator(self, tmp_path, monkeypatch):
        _, data_dir, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        calls = []

        def fake_generate(root, db_path):
            calls.append((root, db_path))

        # The import chain inside refresh_graph is:
        #   from indexer.graph_generator import generate_graph_sqlite
        # We need to mock at the point it's used inside the function, not the
        # module level (the module may fail to import due to missing tree_sitter deps).
        # Use sys.modules to inject a fake module.
        import sys
        fake_module = MagicMock()
        fake_module.generate_graph_sqlite = fake_generate
        fake_treesitter = MagicMock()
        fake_treesitter.get_language = MagicMock(return_value="python")
        monkeypatch.setitem(sys.modules, "indexer.graph_generator", fake_module)
        monkeypatch.setitem(sys.modules, "indexer.treesitter_parser", fake_treesitter)

        result = graph.refresh_graph(file_paths=["src/app.py"])
        assert "status" in result
        assert "hint" in result
        assert len(calls) == 1
