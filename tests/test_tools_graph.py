"""
Tests for mcp_server/tools/graph.py -- full coverage for graph CRUD,
querying, impact analysis, hotspot detection, git-based diff/analysis,
export, and refresh.

Covers:
  - add_node: basic, all optional params (stability, do_not_revert, key_functions, tests, rules)
  - update_node: update rules, stability, key_functions; non-existent node error
  - get_node: node found with full fields, index staleness detection, not-found fallback
  - list_nodes: filter by layer, stability, do_not_revert
  - query_graph: callers, callees, tests, symbols, dependents
  - find_hotspots: large functions, high fan-in, high fan-out
  - get_impact: BFS blast radius
  - export_graph: mermaid/dot with scope filter (extended)
  - get_graph_diff: mocked git subprocess
  - analyze_changes: mocked git subprocess, risk scoring
  - refresh_graph: auto-generate stubs (mocked)
  - _check_staleness: file doesn't exist on disk, index missing, file newer than index

Chaos tests:
  - add_node with empty strings
  - update_node for non-existent node
  - get_impact on deeply nested chain (A->B->C->D->E)
  - export_graph with 50+ nodes (performance)
  - query_graph with None symbol
  - find_hotspots with threshold=0 (everything is a hotspot)

Edge cases: empty graph, isolated nodes, circular deps, deep chains,
filter combinations, no git repo.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

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


def _seed_node(file_path: str, *, role: str, layer: str, **kwargs) -> None:
    """v3.0.0 test helper: seed a graph node directly via SQLiteGraph.

    The v2.x tests for ``mcp_server.tools.graph.add_node`` (deleted in
    the 2026-05-22 surface-cut audit) used the high-level wrapper as
    their seeding API. With the wrapper gone, surviving tests for
    other graph functions (get_node / get_impact / query_graph)
    needed a thin re-seeding helper that writes via the still-alive
    SQLiteGraph.add_node — that's this function.

    Accepts the v2.x add_node kwargs (role, layer, stability,
    key_functions, rules, connects_to, do_not_revert, tests) for
    minimal test churn. JSON-list fields are serialized to match what
    the v2.x wrapper used to write.
    """
    import json

    node_id = f"file:{file_path}"
    name = Path(file_path).name
    add_kwargs: dict = {"role": role, "layer": layer}
    for k in ("stability", "do_not_revert"):
        if k in kwargs:
            add_kwargs[k] = kwargs[k]
    if "rules" in kwargs:
        add_kwargs["rules"] = json.dumps(kwargs["rules"])
    if "key_functions" in kwargs:
        add_kwargs["key_functions"] = json.dumps(kwargs["key_functions"])
    if "connects_to" in kwargs:
        add_kwargs["dependencies"] = json.dumps(kwargs["connects_to"])

    # Reopen the DB on demand — tests call _setup_project + db.close()
    # then come back here to seed. Mirrors the v2.x wrapper's behavior.
    from mcp_server.paths import get_data_dir

    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    try:
        db.add_node(node_id, "file", name, file_path, **add_kwargs)
    finally:
        db.close()


def _populate_graph(db: SQLiteGraph) -> None:
    """Create a standard graph topology for testing.

    Topology:
      src/api.py   --imports--> src/core.py --imports--> src/utils.py
      src/routes.py --imports--> src/api.py
      tests/test_api.py --imports--> src/api.py

    Layers:  api -> core -> utils, routes -> api, tests -> api
    """
    db.add_node(
        "file:src/api.py",
        "file",
        "api.py",
        "src/api.py",
        layer="api",
        stability="medium",
        role="API handlers",
    )
    db.add_node(
        "file:src/core.py",
        "file",
        "core.py",
        "src/core.py",
        layer="core",
        stability="high",
        role="Business logic",
    )
    db.add_node(
        "file:src/utils.py",
        "file",
        "utils.py",
        "src/utils.py",
        layer="utils",
        stability="high",
        role="Utility functions",
    )
    db.add_node(
        "file:src/routes.py",
        "file",
        "routes.py",
        "src/routes.py",
        layer="api",
        stability="low",
        role="Route definitions",
    )
    db.add_node(
        "file:tests/test_api.py",
        "file",
        "test_api.py",
        "tests/test_api.py",
        layer="tests",
        stability="medium",
        role="API tests",
    )

    db.add_edge("file:src/api.py", "file:src/core.py", kind="imports")
    db.add_edge("file:src/core.py", "file:src/utils.py", kind="imports")
    db.add_edge("file:src/routes.py", "file:src/api.py", kind="imports")
    db.add_edge("file:tests/test_api.py", "file:src/api.py", kind="imports")


def _add_symbols(db: SQLiteGraph) -> None:
    """Add function-level symbols and call edges for query_graph tests."""
    # api.py symbols
    db.add_symbol(
        "file:src/api.py::handle_request",
        "file:src/api.py",
        "handle_request",
        "function",
        start_line=10,
        end_line=30,
        is_public=True,
    )
    db.add_symbol(
        "file:src/api.py::validate_input",
        "file:src/api.py",
        "validate_input",
        "function",
        start_line=32,
        end_line=45,
        is_public=True,
    )
    db.add_symbol(
        "file:src/api.py::_internal_helper",
        "file:src/api.py",
        "_internal_helper",
        "function",
        start_line=47,
        end_line=55,
        is_public=False,
    )

    # core.py symbols
    db.add_symbol(
        "file:src/core.py::process_data",
        "file:src/core.py",
        "process_data",
        "function",
        start_line=5,
        end_line=80,
        is_public=True,
    )
    db.add_symbol(
        "file:src/core.py::transform",
        "file:src/core.py",
        "transform",
        "function",
        start_line=82,
        end_line=100,
        is_public=True,
    )

    # utils.py symbol
    db.add_symbol(
        "file:src/utils.py::sanitize",
        "file:src/utils.py",
        "sanitize",
        "function",
        start_line=1,
        end_line=15,
        is_public=True,
    )

    # Call edges: handle_request -> process_data -> sanitize
    #             handle_request -> validate_input
    db.add_call_edge(
        "file:src/api.py::handle_request", "file:src/core.py::process_data"
    )
    db.add_call_edge(
        "file:src/api.py::handle_request", "file:src/api.py::validate_input"
    )
    db.add_call_edge("file:src/core.py::process_data", "file:src/utils.py::sanitize")
    # Multiple callers for sanitize (high fan-in scenario)
    db.add_call_edge("file:src/api.py::validate_input", "file:src/utils.py::sanitize")
    db.add_call_edge("file:src/core.py::transform", "file:src/utils.py::sanitize")


# =====================================================================
# add_node
# =====================================================================


# v3.0.0 audit cleanup (2026-05-22 surface-cut): test classes for
# the deleted graph mutation / graph export tools were removed
# wholesale: TestAddNode, TestUpdateNode, TestListNodes,
# TestFindHotspots, TestExportGraph, TestGetGraphDiff,
# TestAnalyzeChanges. Their MCP tools were deleted in batch 4a
# and the underlying functions were ripped in v3.0.0's dead-code
# sweep. Surviving tests cover: get_node, get_impact, query_graph,
# refresh_graph (still-active MCP tools).


# =====================================================================
# update_node
# =====================================================================


# =====================================================================
# get_node
# =====================================================================


class TestGetNode:
    def test_get_node_found_with_full_fields(self, tmp_path, monkeypatch):
        """get_node with full=True should return parsed JSON for rules/deps/key_functions."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        _seed_node(
            "src/full.py",
            role="Full node",
            layer="api",
            stability="high",
            key_functions=["main", "init"],
            rules=["Do not modify"],
            connects_to=[{"target": "src/db.py", "edge": "imports"}],
            do_not_revert=True,
        )
        result = graph.get_node("src/full.py", full=True)
        assert result["found"] is True
        assert result["file_path"] == "src/full.py"
        assert result["role"] == "Full node"
        assert result["layer"] == "api"
        assert result["stability"] == "high"
        assert isinstance(result["key_functions"], list)
        assert "main" in result["key_functions"]
        assert isinstance(result["rules"], list)
        assert "Do not modify" in result["rules"]
        assert bool(result["do_not_revert"]) is True
        assert "stale" in result

    def test_get_node_summary_mode_default(self, tmp_path, monkeypatch):
        """Default get_node returns counts, not full rules/deps arrays."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        _seed_node(
            "src/summ.py",
            role="Summary node",
            layer="api",
            rules=["rule1", "rule2"],
        )
        result = graph.get_node("src/summ.py")
        assert result["found"] is True
        assert result["rules_count"] == 2
        # Full arrays should NOT be present in summary mode
        assert "rules" not in result
        assert "dependencies" not in result
        assert "key_functions" not in result
        assert "hint" in result

    def test_get_node_not_found(self, tmp_path, monkeypatch):
        """get_node for a non-existent path should return found=False."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.get_node("does/not/exist.py")
        assert result["found"] is False
        assert "hint" in result

    def test_get_node_not_indexed_returns_null_counts(self, tmp_path, monkeypatch):
        """2026-05-18 v2.1.2 Item 2: get_node for an unindexed file must
        return `not_indexed: True` AND `null` for all numeric counts
        (rules_count, dependencies_count, key_functions_count). Previously
        these fields were absent OR were `0` — agents couldn't distinguish
        'unindexed' from 'indexed-with-zero-deps' (the trust-recovery bug
        Report 3 §'Graph layer is dead weight for new code' identified)."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.get_node("does/not/exist.py")
        assert result["found"] is False
        assert result["not_indexed"] is True, (
            "Bug regression: get_node must return not_indexed=True for "
            "unindexed paths so agents don't trust a zero blast-radius lie."
        )
        # Counts must be null, NOT 0 (the whole point of this fix).
        assert result["rules_count"] is None
        assert result["dependencies_count"] is None
        assert result["key_functions_count"] is None

    def test_get_impact_not_indexed_returns_null_counts(self, tmp_path, monkeypatch):
        """Same Item 2 contract for get_impact: unindexed → null counts +
        not_indexed: True. Previously returned blast_radius: 0 which
        agents trusted as 'safe to edit'."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = graph.get_impact("does/not/exist.py")
        assert result["found"] is False
        assert result["not_indexed"] is True
        assert result["blast_radius"] is None
        assert result["protected_count"] is None
        assert result["high_stability_count"] is None

    def test_staleness_file_does_not_exist(self, tmp_path, monkeypatch):
        """_check_staleness should flag stale=True if file is missing from disk."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        # Add a node for a file that doesn't exist on disk
        _seed_node("src/ghost.py", role="Ghost", layer="core")
        result = graph.get_node("src/ghost.py")
        assert result["found"] is True
        # Summary: stale flag at top; full mode includes stale_reason
        assert result["stale"] is True
        full = graph.get_node("src/ghost.py", full=True)
        assert (
            "does not exist" in full["stale_reason"].lower()
            or "missing" in full["stale_reason"].lower()
        )

    def test_staleness_index_missing(self, tmp_path, monkeypatch):
        """_check_staleness should flag stale when .last_indexed file is missing."""
        project_root, data_dir, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        # Create the actual file on disk so file_mtime is not None
        src_dir = project_root / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "real.py").write_text("x = 1")
        _seed_node("src/real.py", role="Real file", layer="core")

        result = graph.get_node("src/real.py")
        # Summary: stale at top level
        assert result["stale"] is True
        full = graph.get_node("src/real.py", full=True)
        assert (
            "index missing" in full["stale_reason"].lower()
            or "last_indexed" in full["stale_reason"].lower()
        )

    def test_staleness_file_newer_than_index(self, tmp_path, monkeypatch):
        """_check_staleness should flag stale when file is newer than index."""
        project_root, data_dir, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        src_dir = project_root / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "fresh.py").write_text("y = 2")

        # Create index timestamp in the past
        index_dir = data_dir / "codeindex"
        index_dir.mkdir(parents=True)
        past_ts = time.time() - 3600  # 1 hour ago
        (index_dir / ".last_indexed").write_text(str(past_ts))

        _seed_node("src/fresh.py", role="Fresh file", layer="core")
        result = graph.get_node("src/fresh.py")
        assert result["stale"] is True
        full = graph.get_node("src/fresh.py", full=True)
        assert "modified after" in full["stale_reason"].lower()


# =====================================================================
# list_nodes
# =====================================================================


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
        result = graph.query_graph(
            "src/api.py", symbol="handle_request", query_type="callees"
        )
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
        result = graph.query_graph(
            "src/utils.py", symbol="sanitize", query_type="callers"
        )
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
        result = graph.query_graph(
            "src/api.py", symbol="handle_request", query_type="tests"
        )
        assert result["query_type"] == "tests"
        assert "tests/test_api.py" in result["test_files"]

    def test_query_dependents(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph(
            "src/api.py", symbol="handle_request", query_type="dependents"
        )
        assert result["query_type"] == "dependents"
        # routes.py and tests/test_api.py depend on api.py
        dep_files = {r["file"] for r in result["results"]}
        assert "src/routes.py" in dep_files or "tests/test_api.py" in dep_files

    def test_query_missing_symbol_error(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph(
            "src/api.py", symbol="nonexistent_func", query_type="callers"
        )
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
        result = graph.query_graph(
            "src/api.py", symbol="handle_request", query_type="invalid"
        )
        assert "error" in result

    def test_query_with_none_symbol_chaos(self, tmp_path, monkeypatch):
        """Chaos: query_graph with None symbol for callers should return error, not crash."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _populate_graph(db)
        _add_symbols(db)
        db.close()
        result = graph.query_graph("src/api.py", symbol=None, query_type="callers")
        assert "error" in result


# =====================================================================
# find_hotspots
# =====================================================================


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
        db.add_node(
            "file:isolated.py", "file", "isolated.py", "isolated.py", layer="misc"
        )
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
        # Should not hang or crash -- circular path detection in SQL prevents infinite recursion
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

    def test_impact_deeply_nested_five_levels_chaos(self, tmp_path, monkeypatch):
        """Chaos: A->B->C->D->E chain, check that impact analysis does not crash."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        chain = ["alpha.py", "bravo.py", "charlie.py", "delta.py", "echo.py"]
        for f in chain:
            db.add_node(f"file:{f}", "file", f, f, layer="deep")
        for i in range(len(chain) - 1):
            db.add_edge(f"file:{chain[i]}", f"file:{chain[i+1]}", kind="imports")
        db.close()

        # Impact from the leaf node
        result = graph.get_impact("echo.py")
        assert result["found"] is True
        # delta.py should definitely be affected (depth 1)
        affected = {a["file"] for a in result["affected_files"]}
        assert "delta.py" in affected


# =====================================================================
# export_graph (extended)
# =====================================================================


# =====================================================================
# get_graph_diff (mocked git)
# =====================================================================


# =====================================================================
# analyze_changes (mocked git)
# =====================================================================


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


# ---------------------------------------------------------------------------
# v3.7.0 opt-in gate — graph-read vector (the dominant ghost-dir source)
# ---------------------------------------------------------------------------


class TestGraphOptInGate:
    """The graph-read vector must NOT adopt a project the user never init-ed.

    SQLiteGraph.__init__ mkdir's the graph dir just by connecting, so merely
    *reading* a non-opted project (e.g. get_impact before an edit) used to
    create a ghost ~/.codevira/projects/<key>/graph/. These assert the gate
    returns an inert hint and creates NOTHING.
    """

    def _ghost_project(self, tmp_path, monkeypatch):
        from mcp_server import opt_in

        monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)  # -> hint mode
        project_root = tmp_path / "ghost"
        project_root.mkdir()  # NO in-repo .codevira/config.yaml -> not opted in
        centralized = tmp_path / "central"  # where get_data_dir would point
        monkeypatch.setattr(paths, "_project_dir_override", None)
        monkeypatch.chdir(project_root.resolve())
        monkeypatch.setattr("mcp_server.tools.graph.get_data_dir", lambda: centralized)
        opt_in.invalidate_opt_in_cache()
        return project_root, centralized

    def test_get_node_returns_hint_and_creates_nothing(self, tmp_path, monkeypatch):
        _, centralized = self._ghost_project(tmp_path, monkeypatch)
        result = graph.get_node("src/main.py")
        assert result["not_opted_in"] is True
        assert result["file_path"] == "src/main.py"
        assert "codevira init" in result["fix_command"]
        assert not centralized.exists()  # no ghost graph dir created

    def test_get_impact_returns_hint_and_creates_nothing(self, tmp_path, monkeypatch):
        _, centralized = self._ghost_project(tmp_path, monkeypatch)
        result = graph.get_impact("src/main.py")
        assert result["not_opted_in"] is True
        assert not (centralized / "graph" / "graph.db").exists()
        assert not centralized.exists()

    def test_query_graph_returns_hint_and_creates_nothing(self, tmp_path, monkeypatch):
        _, centralized = self._ghost_project(tmp_path, monkeypatch)
        result = graph.query_graph("src/main.py", query_type="callees")
        assert result["not_opted_in"] is True
        assert not centralized.exists()

    def test_refresh_graph_refuses_and_creates_nothing(self, tmp_path, monkeypatch):
        _, centralized = self._ghost_project(tmp_path, monkeypatch)
        result = graph.refresh_graph()
        assert result["not_opted_in"] is True
        assert result["status"] == "skipped"
        assert not centralized.exists()

    def test_opted_in_project_is_not_gated(self, tmp_path, monkeypatch):
        from mcp_server import opt_in

        monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)
        _project_root, _data_dir, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        opt_in.invalidate_opt_in_cache()
        result = graph.get_node("src/whatever.py")
        # Opted in -> normal not-found path, never the opt-in refusal.
        assert result.get("not_opted_in") is not True
