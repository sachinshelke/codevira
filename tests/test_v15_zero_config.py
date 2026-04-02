"""
Tests for v1.5: Zero-Config Global Memory + Deep Graph Intelligence.

Covers: detect.py, ide_inject.py, global_db.py, global_sync.py,
        symbols/call_edges in sqlite_graph.py, query_graph, analyze_changes,
        find_hotspots, prompts.
"""
import json
import os
import sqlite3
import pytest
from pathlib import Path


# =====================================================================
# Helpers
# =====================================================================

def _setup_db(tmp_path, monkeypatch):
    """Create a fresh SQLiteGraph in a temp directory."""
    monkeypatch.setenv("CODEVIRA_DATA_DIR", str(tmp_path))
    from indexer.sqlite_graph import SQLiteGraph
    db_path = tmp_path / "graph.db"
    return SQLiteGraph(db_path)


# =====================================================================
# Part A: Zero-Config Auto-Detection
# =====================================================================

class TestDetect:
    def test_detect_python_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "src").mkdir()
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "python"
        assert ".py" in result["file_extensions"]
        assert "src" in result["watched_dirs"]

    def test_detect_typescript_from_tsconfig(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "src").mkdir()
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "typescript"
        assert ".ts" in result["file_extensions"]

    def test_detect_go_from_gomod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/test")
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "go"
        assert ".go" in result["file_extensions"]

    def test_detect_rust_from_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='test'\n")
        (tmp_path / "src").mkdir()
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "rust"
        assert "src" in result["watched_dirs"]

    def test_detect_java_from_pom(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "java"

    def test_detect_js_vs_ts_disambiguation(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}')
        # No .ts files — should detect as javascript
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "javascript"

        # Add tsconfig — should detect as typescript
        (tmp_path / "tsconfig.json").write_text("{}")
        result2 = auto_detect_project(tmp_path)
        assert result2["language"] == "typescript"

    def test_detect_fallback_to_python(self, tmp_path):
        # Empty directory — should fallback to python
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert result["language"] == "python"

    def test_detect_watched_dirs_existing(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()
        from mcp_server.detect import detect_watched_dirs
        dirs = detect_watched_dirs(tmp_path, "python")
        assert "src" in dirs
        assert "lib" in dirs

    def test_detect_watched_dirs_fallback(self, tmp_path):
        from mcp_server.detect import detect_watched_dirs
        dirs = detect_watched_dirs(tmp_path, "python")
        assert dirs == ["."]

    def test_collection_name_sanitized(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        from mcp_server.detect import auto_detect_project
        result = auto_detect_project(tmp_path)
        assert " " not in result["collection_name"]
        assert "-" not in result["collection_name"]


# =====================================================================
# Part B: IDE Auto-Inject
# =====================================================================

class TestIDEInject:
    def test_merge_mcp_config_non_destructive(self):
        from mcp_server.ide_inject import _merge_mcp_config
        existing = {
            "mcpServers": {"other-tool": {"command": "other"}},
            "permissions": {"allow": True},
        }
        merged = _merge_mcp_config(existing, "codevira", {"command": "codevira-mcp"})
        assert "other-tool" in merged["mcpServers"]
        assert "codevira" in merged["mcpServers"]
        assert merged["permissions"]["allow"] is True

    def test_merge_creates_mcp_servers_key(self):
        from mcp_server.ide_inject import _merge_mcp_config
        merged = _merge_mcp_config({}, "codevira", {"command": "test"})
        assert merged["mcpServers"]["codevira"]["command"] == "test"

    def test_write_json_safe_atomic(self, tmp_path):
        from mcp_server.ide_inject import _write_json_safe, _read_json_safe
        path = tmp_path / "test.json"
        _write_json_safe(path, {"key": "value"})
        result = _read_json_safe(path)
        assert result["key"] == "value"

    def test_read_json_safe_missing_file(self, tmp_path):
        from mcp_server.ide_inject import _read_json_safe
        result = _read_json_safe(tmp_path / "nonexistent.json")
        assert result == {}

    def test_inject_claude_creates_config(self, tmp_path):
        from mcp_server.ide_inject import _inject_claude
        (tmp_path / ".claude").mkdir()
        path = _inject_claude(tmp_path, "/usr/bin/codevira-mcp")
        assert path is not None
        config = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert config["mcpServers"]["codevira"]["command"] == "/usr/bin/codevira-mcp"

    def test_inject_preserves_existing_servers(self, tmp_path):
        from mcp_server.ide_inject import _inject_claude, _read_json_safe
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Pre-existing config
        (claude_dir / "settings.json").write_text(json.dumps({
            "mcpServers": {"other": {"command": "other-cmd"}},
        }))
        _inject_claude(tmp_path, "/usr/bin/codevira-mcp")
        config = _read_json_safe(claude_dir / "settings.json")
        assert "other" in config["mcpServers"]
        assert "codevira" in config["mcpServers"]


# =====================================================================
# Part C: Global Database
# =====================================================================

class TestGlobalDB:
    def test_create_and_register_project(self, tmp_path):
        from indexer.global_db import GlobalDB
        db = GlobalDB(tmp_path / "global.db")
        db.register_project("/path/to/project", "my-project", "python")
        assert db.get_project_count() == 1
        db.close()

    def test_upsert_preference(self, tmp_path):
        from indexer.global_db import GlobalDB
        db = GlobalDB(tmp_path / "global.db")
        db.upsert_preference("naming", "snake_case", "my_var", "/proj-a")
        db.upsert_preference("naming", "snake_case", "other_var", "/proj-b")
        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1
        assert prefs[0]["frequency"] == 2
        sources = json.loads(prefs[0]["source_projects"])
        assert "/proj-a" in sources
        assert "/proj-b" in sources
        db.close()

    def test_upsert_rule_weighted_average(self, tmp_path):
        from indexer.global_db import GlobalDB
        db = GlobalDB(tmp_path / "global.db")
        db.upsert_rule("Always use early returns", 0.8, "/proj-a", "patterns", "python")
        db.upsert_rule("Always use early returns", 1.0, "/proj-b", "patterns", "python")
        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        # Weighted: 0.8 * 0.6 + 1.0 * 0.4 = 0.88
        assert abs(rules[0]["confidence"] - 0.88) < 0.01
        db.close()

    def test_get_rules_language_filter(self, tmp_path):
        from indexer.global_db import GlobalDB
        db = GlobalDB(tmp_path / "global.db")
        db.upsert_rule("Go exported names capitalized", 0.9, "/proj-a", "naming", "go")
        db.upsert_rule("Universal early returns", 0.9, "/proj-b", "patterns", None)
        # Python query should get the universal rule but not Go-specific
        rules = db.get_rules(min_confidence=0.5, language="python")
        names = [r["rule_text"] for r in rules]
        assert "Universal early returns" in names
        assert "Go exported names capitalized" not in names
        db.close()

    def test_get_stats(self, tmp_path):
        from indexer.global_db import GlobalDB
        db = GlobalDB(tmp_path / "global.db")
        db.register_project("/a", "a", "python")
        db.register_project("/b", "b", "go")
        db.upsert_preference("naming", "snake_case", None, "/a")
        db.upsert_rule("Rule 1", 0.8, "/a")
        stats = db.get_stats()
        assert stats["project_count"] == 2
        assert stats["total_preferences"] == 1
        assert stats["total_rules"] == 1
        db.close()


# =====================================================================
# Part D: Function-Level Symbols & Call Graph
# =====================================================================

class TestSymbolsCallGraph:
    def test_add_and_get_symbols(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:main.py", "file", "main.py", "main.py")
        db.add_symbol("file:main.py::foo", "file:main.py", "foo", "function",
                       signature="def foo(x: int) -> str:", start_line=1, end_line=10)
        db.add_symbol("file:main.py::bar", "file:main.py", "bar", "function",
                       signature="def bar():", start_line=12, end_line=20)
        symbols = db.get_symbols_for_file("file:main.py")
        assert len(symbols) == 2
        assert symbols[0]["name"] == "foo"
        db.close()

    def test_call_edges(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_symbol("file:a.py::caller", "file:a.py", "caller", "function")
        db.add_symbol("file:b.py::callee", "file:b.py", "callee", "function")
        db.add_call_edge("file:a.py::caller", "file:b.py::callee", line=5)

        callers = db.get_callers("file:b.py::callee")
        assert len(callers) == 1
        assert callers[0]["name"] == "caller"

        callees = db.get_callees("file:a.py::caller")
        assert len(callees) == 1
        assert callees[0]["name"] == "callee"
        db.close()

    def test_remove_symbols_cascades(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:a.py", "file", "a.py", "a.py")
        db.add_symbol("file:a.py::foo", "file:a.py", "foo", "function")
        db.add_node("file:b.py", "file", "b.py", "b.py")
        db.add_symbol("file:b.py::bar", "file:b.py", "bar", "function")
        db.add_call_edge("file:a.py::foo", "file:b.py::bar")

        db.remove_symbols_for_file("file:a.py")
        assert db.get_symbol_count() == 1  # Only bar remains
        assert db.get_call_edge_count() == 0  # Edge cascaded
        db.close()

    def test_find_symbol(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:x.py", "file", "x.py", "x.py")
        db.add_symbol("file:x.py::process", "file:x.py", "process", "function")
        sym = db.find_symbol("process")
        assert sym is not None
        assert sym["name"] == "process"
        db.close()

    def test_find_hotspot_functions(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:big.py", "file", "big.py", "big.py")
        db.add_symbol("file:big.py::huge_func", "file:big.py", "huge_func", "function",
                       start_line=1, end_line=100)
        db.add_symbol("file:big.py::small_func", "file:big.py", "small_func", "function",
                       start_line=101, end_line=110)
        hotspots = db.find_hotspot_functions(min_lines=50)
        assert len(hotspots) == 1
        assert hotspots[0]["name"] == "huge_func"
        db.close()

    def test_find_high_fan_in(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        db.add_node("file:core.py", "file", "core.py", "core.py")
        db.add_symbol("file:core.py::important", "file:core.py", "important", "function")
        for i in range(5):
            db.add_node(f"file:f{i}.py", "file", f"f{i}.py", f"f{i}.py")
            db.add_symbol(f"file:f{i}.py::caller{i}", f"file:f{i}.py", f"caller{i}", "function")
            db.add_call_edge(f"file:f{i}.py::caller{i}", "file:core.py::important")
        high_fi = db.find_high_fan_in(min_callers=3)
        assert len(high_fi) == 1
        assert high_fi[0]["name"] == "important"
        assert high_fi[0]["caller_count"] == 5
        db.close()


# =====================================================================
# Part F: MCP Prompts
# =====================================================================

class TestPrompts:
    def test_list_prompts(self):
        from mcp_server.prompts import list_prompts
        prompts = list_prompts()
        assert len(prompts) == 5
        names = {p["name"] for p in prompts}
        assert "review_changes" in names
        assert "debug_issue" in names
        assert "onboard_session" in names
        assert "pre_commit_check" in names
        assert "architecture_overview" in names

    def test_get_prompt_with_arguments(self):
        from mcp_server.prompts import get_prompt
        result = get_prompt("debug_issue", {"description": "Login fails on mobile"})
        assert result is not None
        assert "Login fails on mobile" in result["messages"][0]["content"]["text"]

    def test_get_prompt_unknown(self):
        from mcp_server.prompts import get_prompt
        result = get_prompt("nonexistent_prompt")
        assert result is None


# =====================================================================
# Part C+: Global sync source column migration
# =====================================================================

class TestGlobalSyncMigration:
    def test_source_column_exists(self, tmp_path, monkeypatch):
        db = _setup_db(tmp_path, monkeypatch)
        # Should be able to insert with source column
        db.record_preference("test", "signal", "example", source="global")
        prefs = db.get_preferences()
        assert len(prefs) == 1
        db.close()
