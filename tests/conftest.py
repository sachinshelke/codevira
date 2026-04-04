"""
Shared pytest fixtures for the Codevira MCP test suite.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Install a comprehensive mock of indexer.treesitter_parser BEFORE any test
# file imports modules that depend on it. This mock provides all attributes
# that code_reader.py, chunker.py, and graph_generator.py import.
#
# This runs at conftest load time (before collection), so it covers all tests.
# ---------------------------------------------------------------------------
if "tree_sitter_language_pack" not in sys.modules:
    _ts_lang_pack = types.ModuleType("tree_sitter_language_pack")
    _ts_lang_pack.__dict__["__all__"] = []
    sys.modules["tree_sitter_language_pack"] = _ts_lang_pack

if "tree_sitter" not in sys.modules:
    _ts_mod = types.ModuleType("tree_sitter")
    _ts_mod.Node = MagicMock()
    sys.modules["tree_sitter"] = _ts_mod

if "indexer.treesitter_parser" not in sys.modules:
    _fake_ts = types.ModuleType("indexer.treesitter_parser")
    _fake_ts.parse_file = MagicMock(return_value=None)
    _fake_ts.get_language = MagicMock(return_value=None)
    _fake_ts.get_symbol_source = MagicMock(return_value={"found": False})
    _fake_ts.EXTENSION_MAP = {}
    # ParsedSymbol dataclass stub (used by graph_generator)
    from dataclasses import dataclass, field as dc_field
    from typing import Optional, List

    @dataclass
    class _ParsedSymbol:
        name: str = ""
        kind: str = ""
        signature_line: str = ""
        start_line: int = 0
        end_line: int = 0
        docstring: Optional[str] = None
        is_public: bool = True
        methods: List[str] = dc_field(default_factory=list)

    _fake_ts.ParsedSymbol = _ParsedSymbol
    sys.modules["indexer.treesitter_parser"] = _fake_ts
    # Also set on parent package so `from indexer.treesitter_parser import X` works
    import indexer as _indexer_pkg
    _indexer_pkg.treesitter_parser = _fake_ts

import mcp_server.paths as paths
from indexer.sqlite_graph import SQLiteGraph


@pytest.fixture(autouse=True)
def _isolate_global_home(tmp_path, monkeypatch):
    """Prevent ALL tests from writing to the real ~/.codevira/.

    This autouse fixture ensures no test pollutes the real centralized
    storage directory. Each test gets its own fake global home.
    """
    fake_home = tmp_path / "isolated-global-home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Isolated project with .codevira dir, config.yaml, and SQLiteGraph."""
    project = tmp_path / "test-project"
    data_dir = project / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text(
        "project:\n  name: test\n  language: python\n  watched_dirs:\n    - src\n  file_extensions:\n    - .py\n"
    )
    (data_dir / "graph").mkdir(parents=True)
    (data_dir / "graph" / "changesets").mkdir(parents=True)

    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project.resolve())
    # get_global_home is already patched by the autouse _isolate_global_home fixture

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    yield project, data_dir, db
    db.close()


@pytest.fixture
def populated_db(project_env):
    """project_env with pre-loaded graph data."""
    project, data_dir, db = project_env
    # Nodes
    db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
    db.add_node("file:src/service.py", "file", "service.py", "src/service.py", layer="service")
    db.add_node("file:src/db.py", "file", "db.py", "src/db.py", layer="data")
    db.add_node("file:tests/test_api.py", "file", "test_api.py", "tests/test_api.py", layer="test")
    # Edges
    db.add_edge("file:src/api.py", "file:src/service.py", kind="imports")
    db.add_edge("file:src/service.py", "file:src/db.py", kind="imports")
    db.add_edge("file:tests/test_api.py", "file:src/api.py", kind="tests")
    # Sessions + decisions
    db.log_session("s1", "Initial API setup", "1", [
        {"file_path": "src/api.py", "decision": "Use REST endpoints", "context": "API design"},
        {"file_path": "src/service.py", "decision": "Use repository pattern", "context": "Architecture"},
    ])
    db.log_session("s2", "Add database layer", "2", [
        {"file_path": "src/db.py", "decision": "Use SQLite for local storage", "context": "Data layer"},
    ])
    # Outcomes
    db.record_outcome("s1", "src/api.py", "kept")
    db.record_outcome("s1", "src/service.py", "modified", delta_summary="Changed naming")
    # Preferences
    db.record_preference("naming", "Prefers snake_case")
    db.record_preference("naming", "Prefers snake_case")
    db.record_preference("naming", "Prefers snake_case")
    db.record_preference("structure", "Uses early returns")
    # Learned rules
    db.add_learned_rule(
        "API files should have tests", 0.8, ["s1"],
        category="testing", file_pattern="src/api/*",
    )
    db.add_learned_rule(
        "Use type hints", 0.9, ["s1", "s2"],
        category="patterns",
    )
    return project, data_dir, db


@pytest.fixture
def sample_source_files(tmp_path):
    """Create realistic Python source files for testing."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        '"""Main application module."""\n'
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "DEFAULT_PORT = 8080\n"
        "\n"
        "class Application:\n"
        '    """The main application class."""\n'
        "    \n"
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "        self._running = False\n"
        "    \n"
        "    def start(self) -> None:\n"
        '        """Start the application."""\n'
        "        self._running = True\n"
        "    \n"
        "    def stop(self) -> None:\n"
        '        """Stop the application."""\n'
        "        self._running = False\n"
        "    \n"
        "    def _internal_method(self):\n"
        "        pass\n"
        "\n"
        "async def fetch_data(url: str, timeout: int = 30) -> dict:\n"
        '    """Fetch data from a URL."""\n'
        '    return {"url": url}\n'
        "\n"
        "def _private_helper():\n"
        "    pass\n"
    )
    (src / "util.py").write_text(
        '"""Utility functions."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text(
        '"""Tests for main module."""\n'
        "from src.main import Application\n"
        "\n"
        "def test_start():\n"
        '    app = Application("test")\n'
        "    app.start()\n"
        "    assert app._running\n"
    )
    return tmp_path


@pytest.fixture
def corrupt_yaml(tmp_path):
    """Factory for creating corrupt YAML files."""
    def _make(name="corrupt.yaml", content="{{invalid yaml: ["):
        p = tmp_path / name
        p.write_text(content)
        return p
    return _make


@pytest.fixture
def corrupt_sqlite(tmp_path):
    """Factory for creating corrupt SQLite database files."""
    def _make(name="corrupt.db"):
        p = tmp_path / name
        p.write_bytes(b"NOT A SQLITE DB" + b"\x00" * 100)
        return p
    return _make
