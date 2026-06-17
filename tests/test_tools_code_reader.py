"""
Tests for mcp_server/tools/code_reader.py

Covers:
  - get_signature(): Python with functions + classes + async, empty file, syntax error,
                     private exclusion, nested class methods, file not found, unsupported ext
  - get_code(): function by name, class by name, symbol=None (module constants),
                symbol not found (hint), file not found
  - Edge: __init__ excluded from public methods, only first docstring line shown

Note: tree-sitter-language-pack is an optional dependency. We mock the
treesitter_parser module so these tests can run without it installed.
Only the Python (ast-based) code paths are exercised here.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock tree-sitter dependencies before importing code_reader
# ---------------------------------------------------------------------------
_ts_mock = types.ModuleType("tree_sitter_language_pack")
_ts_mock.__dict__.update({"__all__": []})
_ts_node_mock = types.ModuleType("tree_sitter")
_ts_node_mock.Node = MagicMock()

# Only patch if not already importable
if "tree_sitter_language_pack" not in sys.modules:
    sys.modules["tree_sitter_language_pack"] = _ts_mock
if "tree_sitter" not in sys.modules:
    sys.modules["tree_sitter"] = _ts_node_mock

# Now we can safely import treesitter_parser (it will use the mocks)
# and then import code_reader which depends on it. The late import is
# intentional — the mocks above MUST land in sys.modules first.
from mcp_server.tools.code_reader import (  # noqa: E402
    _is_private,
    _resolve,
    _within_root,
    get_code,
    get_signature,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_project_root(tmp_path, monkeypatch):
    """Patch get_project_root() to return tmp_path."""
    monkeypatch.setattr(
        "mcp_server.tools.code_reader.get_project_root",
        lambda: tmp_path,
    )
    return tmp_path


@pytest.fixture
def py_file(mock_project_root):
    """Create a realistic Python source file at src/main.py."""
    src = mock_project_root / "src"
    src.mkdir()
    main = src / "main.py"
    main.write_text(
        '"""Main application module.\n\nThis module provides the core app.\n"""\n'
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "DEFAULT_PORT = 8080\n"
        "\n"
        "class Application:\n"
        '    """The main application class.\n\n    Handles lifecycle.\n    """\n'
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
        '    """Fetch data from a URL.\n\n    Supports retries.\n    """\n'
        '    return {"url": url}\n'
        "\n"
        "def _private_helper():\n"
        "    pass\n"
    )
    return main


@pytest.fixture
def empty_py(mock_project_root):
    """Create an empty Python file."""
    f = mock_project_root / "empty.py"
    f.write_text("")
    return f


@pytest.fixture
def syntax_error_py(mock_project_root):
    """Create a Python file with a syntax error."""
    f = mock_project_root / "broken.py"
    f.write_text("def foo(\n    # missing closing paren and colon\n")
    return f


@pytest.fixture
def nested_classes_py(mock_project_root):
    """Create a Python file with nested classes."""
    f = mock_project_root / "nested.py"
    f.write_text(
        "class Outer:\n"
        '    """Outer class."""\n'
        "    \n"
        "    def outer_method(self):\n"
        "        pass\n"
        "    \n"
        "    def __init__(self):\n"
        "        pass\n"
        "    \n"
        "    def _private_outer(self):\n"
        "        pass\n"
        "    \n"
        "    class Inner:\n"
        '        """Inner class."""\n'
        "        def inner_method(self):\n"
        "            pass\n"
    )
    return f


@pytest.fixture
def util_py(mock_project_root):
    """Create a simple utility Python file."""
    f = mock_project_root / "util.py"
    f.write_text(
        '"""Utility functions."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Add two numbers."""\n'
        "    return a + b\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        '    """Multiply two numbers."""\n'
        "    return a * b\n"
    )
    return f


@pytest.fixture
def constants_only_py(mock_project_root):
    """Create a Python file with only constants."""
    f = mock_project_root / "constants.py"
    f.write_text(
        '"""App constants."""\n'
        "\n"
        "VERSION = '1.0.0'\n"
        "DEBUG = True\n"
        "MAX_ITEMS: int = 100\n"
    )
    return f


# ===========================================================================
# _is_private()
# ===========================================================================


class TestIsPrivate:
    def test_private_single_underscore(self):
        assert _is_private("_helper") is True

    def test_private_dunder(self):
        assert _is_private("__init__") is True

    def test_public(self):
        assert _is_private("fetch_data") is False

    def test_empty_string(self):
        assert _is_private("") is False


# ===========================================================================
# _resolve()
# ===========================================================================


class TestResolve:
    def test_relative_path(self, mock_project_root):
        result = _resolve("src/main.py")
        assert result == mock_project_root / "src" / "main.py"

    def test_absolute_path(self):
        abs_path = "/tmp/absolute/file.py"
        result = _resolve(abs_path)
        assert result == Path(abs_path)


class TestPathContainment:
    """The reader tools refuse paths outside the project root — defense in
    depth against a prompt-injected read of an arbitrary file."""

    def test_within_root_true_for_inside(self, mock_project_root):
        assert _within_root(mock_project_root / "pkg" / "mod.py") is True

    def test_within_root_false_for_outside(self, mock_project_root):
        assert _within_root(Path("/etc/passwd")) is False

    def test_get_signature_refuses_outside_root(self, mock_project_root):
        for bad in ("/etc/passwd", "../../../../etc/passwd"):
            res = get_signature(bad)
            assert res["found"] is False
            assert "outside the project root" in res["error"]

    def test_get_code_refuses_outside_root(self, mock_project_root):
        res = get_code("/etc/hosts", "anything")
        assert res["found"] is False
        assert "outside the project root" in res["error"]


# ===========================================================================
# get_signature() — Python files
# ===========================================================================


class TestGetSignaturePython:
    def test_finds_public_class(self, py_file):
        result = get_signature("src/main.py")
        assert result["found"] is True
        names = [s["name"] for s in result["symbols"]]
        assert "Application" in names

    def test_finds_async_function(self, py_file):
        result = get_signature("src/main.py")
        names = [s["name"] for s in result["symbols"]]
        assert "fetch_data" in names

    def test_excludes_private_functions(self, py_file):
        result = get_signature("src/main.py")
        names = [s["name"] for s in result["symbols"]]
        assert "_private_helper" not in names

    def test_module_docstring(self, py_file):
        result = get_signature("src/main.py")
        assert result["module_docstring"] is not None
        assert "Main application module" in result["module_docstring"]

    def test_symbol_count(self, py_file):
        result = get_signature("src/main.py")
        # Application + fetch_data = 2 public symbols
        assert result["symbol_count"] == 2

    def test_symbol_has_line_range(self, py_file):
        result = get_signature("src/main.py")
        for sym in result["symbols"]:
            assert "start_line" in sym
            assert "end_line" in sym
            assert sym["start_line"] <= sym["end_line"]

    def test_symbol_has_signature_line(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert "class Application" in app_sym["signature_line"]

    def test_docstring_first_line_only(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert app_sym["docstring"] == "The main application class."
        # Multi-line docstring should be truncated to first line
        assert "Handles lifecycle" not in app_sym.get("docstring", "")

    def test_async_docstring_first_line(self, py_file):
        result = get_signature("src/main.py")
        fetch_sym = [s for s in result["symbols"] if s["name"] == "fetch_data"][0]
        assert fetch_sym["docstring"] == "Fetch data from a URL."

    def test_class_public_methods(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert "public_methods" in app_sym
        assert "start" in app_sym["public_methods"]
        assert "stop" in app_sym["public_methods"]

    def test_init_excluded_from_public_methods(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert "__init__" not in app_sym.get("public_methods", [])

    def test_private_methods_excluded_from_public_methods(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert "_internal_method" not in app_sym.get("public_methods", [])

    def test_empty_file(self, empty_py):
        result = get_signature("empty.py")
        assert result["found"] is True
        assert result["symbol_count"] == 0
        assert result["symbols"] == []

    def test_syntax_error(self, syntax_error_py):
        result = get_signature("broken.py")
        assert result["found"] is False
        assert "Syntax error" in result["error"]

    def test_file_not_found(self, mock_project_root):
        result = get_signature("nonexistent.py")
        assert result["found"] is False
        assert "File not found" in result["error"]

    def test_unsupported_extension(self, mock_project_root):
        f = mock_project_root / "data.json"
        f.write_text("{}")
        result = get_signature("data.json")
        assert result["found"] is False
        assert "Unsupported file type" in result["error"]

    def test_hint_provided(self, py_file):
        result = get_signature("src/main.py")
        assert "hint" in result
        assert "get_code" in result["hint"]

    def test_function_kind_is_function(self, py_file):
        result = get_signature("src/main.py")
        fetch_sym = [s for s in result["symbols"] if s["name"] == "fetch_data"][0]
        assert fetch_sym["kind"] == "function"

    def test_class_kind_is_class(self, py_file):
        result = get_signature("src/main.py")
        app_sym = [s for s in result["symbols"] if s["name"] == "Application"][0]
        assert app_sym["kind"] == "class"


class TestGetSignatureNestedClasses:
    def test_nested_class_public_methods(self, nested_classes_py):
        result = get_signature("nested.py")
        outer = [s for s in result["symbols"] if s["name"] == "Outer"][0]
        # outer_method is public, __init__ excluded, _private_outer excluded
        assert "outer_method" in outer.get("public_methods", [])
        assert "__init__" not in outer.get("public_methods", [])
        assert "_private_outer" not in outer.get("public_methods", [])

    def test_inner_class_method_visible_via_walk(self, nested_classes_py):
        """inner_method is found via ast.walk of the Outer class."""
        result = get_signature("nested.py")
        outer = [s for s in result["symbols"] if s["name"] == "Outer"][0]
        # inner_method is discovered through ast.walk as a public method
        assert "inner_method" in outer.get("public_methods", [])


class TestGetSignatureUtilFile:
    def test_multiple_functions(self, util_py):
        result = get_signature("util.py")
        assert result["found"] is True
        names = [s["name"] for s in result["symbols"]]
        assert "add" in names
        assert "multiply" in names
        assert result["symbol_count"] == 2


# ===========================================================================
# get_code() — Python files
# ===========================================================================


class TestGetCodeFunction:
    def test_get_function_by_name(self, py_file):
        result = get_code("src/main.py", "fetch_data")
        assert result["found"] is True
        assert result["symbol"] == "fetch_data"
        assert result["kind"] == "function"
        assert "async def fetch_data" in result["source"]

    def test_get_function_has_line_range(self, py_file):
        result = get_code("src/main.py", "fetch_data")
        assert "start_line" in result
        assert "end_line" in result
        assert result["start_line"] <= result["end_line"]

    def test_get_function_has_docstring(self, py_file):
        result = get_code("src/main.py", "fetch_data")
        assert result["docstring"] is not None
        assert "Fetch data" in result["docstring"]

    def test_get_simple_function(self, util_py):
        result = get_code("util.py", "add")
        assert result["found"] is True
        assert "return a + b" in result["source"]


class TestGetCodeClass:
    def test_get_class_by_name(self, py_file):
        result = get_code("src/main.py", "Application")
        assert result["found"] is True
        assert result["symbol"] == "Application"
        assert result["kind"] == "class"
        assert "class Application" in result["source"]

    def test_class_source_includes_methods(self, py_file):
        result = get_code("src/main.py", "Application")
        assert "def start" in result["source"]
        assert "def stop" in result["source"]
        assert "def __init__" in result["source"]

    def test_class_docstring(self, py_file):
        result = get_code("src/main.py", "Application")
        assert result["docstring"] is not None
        assert "main application class" in result["docstring"].lower()


class TestGetCodeModuleConstants:
    def test_symbol_none_returns_constants(self, py_file):
        result = get_code("src/main.py", None)
        assert result["found"] is True
        assert result["kind"] == "module_constants"
        assert result["symbol"] is None

    def test_module_constants_has_assignments(self, py_file):
        result = get_code("src/main.py", None)
        assert "assignments" in result
        sources = [a["source"] for a in result["assignments"]]
        all_source = "\n".join(sources)
        assert "MAX_RETRIES" in all_source
        assert "DEFAULT_PORT" in all_source

    def test_module_constants_has_docstring(self, py_file):
        result = get_code("src/main.py", None)
        assert result["module_docstring"] is not None

    def test_constants_only_file(self, constants_only_py):
        result = get_code("constants.py", None)
        assert result["found"] is True
        assert len(result["assignments"]) >= 2

    def test_assignment_has_line_range(self, constants_only_py):
        result = get_code("constants.py", None)
        for assignment in result["assignments"]:
            assert "start_line" in assignment
            assert "end_line" in assignment


class TestGetCodeSymbolNotFound:
    def test_symbol_not_found(self, py_file):
        result = get_code("src/main.py", "nonexistent_func")
        assert result["found"] is False
        assert "not found" in result["error"]
        assert result["symbol"] == "nonexistent_func"

    def test_provides_available_symbols(self, py_file):
        result = get_code("src/main.py", "nonexistent_func")
        assert "available_symbols" in result
        assert len(result["available_symbols"]) > 0

    def test_provides_hint(self, py_file):
        result = get_code("src/main.py", "nonexistent_func")
        assert "hint" in result
        assert "get_signature" in result["hint"]

    def test_available_symbols_sorted(self, py_file):
        result = get_code("src/main.py", "nonexistent_func")
        syms = result["available_symbols"]
        assert syms == sorted(syms)


class TestGetCodeFileNotFound:
    def test_file_not_found(self, mock_project_root):
        result = get_code("nonexistent.py", "foo")
        assert result["found"] is False
        assert "File not found" in result["error"]

    def test_file_not_found_with_none_symbol(self, mock_project_root):
        result = get_code("nonexistent.py", None)
        assert result["found"] is False
        assert "File not found" in result["error"]


class TestGetCodeSyntaxError:
    def test_syntax_error_with_symbol(self, syntax_error_py):
        result = get_code("broken.py", "foo")
        assert result["found"] is False
        assert "Syntax error" in result["error"]

    def test_syntax_error_with_none_symbol(self, syntax_error_py):
        result = get_code("broken.py", None)
        assert result["found"] is False
        assert "Syntax error" in result["error"]


class TestGetCodeUnsupported:
    def test_unsupported_extension(self, mock_project_root):
        f = mock_project_root / "data.txt"
        f.write_text("hello")
        result = get_code("data.txt", "foo")
        assert result["found"] is False
        assert "Unsupported file type" in result["error"]


class TestGetCodePrivateSymbolAccess:
    def test_can_get_private_function_by_name(self, py_file):
        """get_code should find private symbols when asked directly (ast.walk matches any name)."""
        result = get_code("src/main.py", "_private_helper")
        assert result["found"] is True
        assert result["symbol"] == "_private_helper"

    def test_can_get_init_method(self, py_file):
        """get_code should find __init__ when asked directly."""
        result = get_code("src/main.py", "__init__")
        assert result["found"] is True
        assert result["symbol"] == "__init__"


# ===========================================================================
# get_signature() — tree-sitter (non-Python) dispatch (line 76, 91-121)
# ===========================================================================

# The conftest stubs EXTENSION_MAP as {}, so we must patch
# mcp_server.tools.code_reader.TS_EXTENSION_MAP to include the target
# extension for each test, simulating the real treesitter_parser.


class TestGetSignatureTreesitter:
    def test_non_python_dispatches_to_treesitter(self, tmp_path, monkeypatch):
        """get_signature for .ts file dispatches to _get_signature_treesitter."""
        ts_file = tmp_path / "app.ts"
        ts_file.write_text(
            "export function greet(name: string): string { return name; }"
        )
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        mock_parsed = MagicMock()
        mock_sym = MagicMock()
        mock_sym.is_public = True
        mock_sym.name = "greet"
        mock_sym.kind = "function"
        mock_sym.signature_line = "export function greet(name: string): string"
        mock_sym.start_line = 1
        mock_sym.end_line = 1
        mock_sym.docstring = None
        mock_sym.methods = []
        mock_parsed.symbols = [mock_sym]
        mock_parsed.language = "typescript"
        mock_parsed.module_docstring = None

        with (
            patch(
                "mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".ts": "typescript"}
            ),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file", return_value=mock_parsed
            ),
        ):
            result = get_signature("app.ts")
        assert result["found"] is True
        assert result["language"] == "typescript"
        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["name"] == "greet"

    def test_treesitter_parse_error_returns_not_found(self, tmp_path, monkeypatch):
        """When ts_parse_file raises ValueError, returns found=False."""
        ts_file = tmp_path / "broken.ts"
        ts_file.write_text("invalid")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        with (
            patch(
                "mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".ts": "typescript"}
            ),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file",
                side_effect=ValueError("parse fail"),
            ),
        ):
            result = get_signature("broken.ts")
        assert result["found"] is False
        assert "parse fail" in result["error"]

    def test_treesitter_symbol_with_docstring_and_methods(self, tmp_path, monkeypatch):
        """Symbols with docstrings and methods include them in result."""
        go_file = tmp_path / "service.go"
        go_file.write_text("type Service struct {}")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        mock_parsed = MagicMock()
        mock_sym = MagicMock()
        mock_sym.is_public = True
        mock_sym.name = "Service"
        mock_sym.kind = "class"
        mock_sym.signature_line = "type Service struct"
        mock_sym.start_line = 1
        mock_sym.end_line = 5
        mock_sym.docstring = "Service is the main service.\nSecond line."
        mock_sym.methods = ["Start", "Stop"]
        mock_parsed.symbols = [mock_sym]
        mock_parsed.language = "go"
        mock_parsed.module_docstring = "Package main"

        with (
            patch("mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".go": "go"}),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file", return_value=mock_parsed
            ),
        ):
            result = get_signature("service.go")
        sym = result["symbols"][0]
        assert sym["docstring"] == "Service is the main service."  # only first line
        assert sym["public_methods"] == ["Start", "Stop"]

    def test_treesitter_private_symbols_excluded(self, tmp_path, monkeypatch):
        """Private symbols (is_public=False) are excluded from results."""
        go_file = tmp_path / "pkg.go"
        go_file.write_text("package main")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        mock_parsed = MagicMock()
        private_sym = MagicMock()
        private_sym.is_public = False
        public_sym = MagicMock()
        public_sym.is_public = True
        public_sym.name = "PublicFunc"
        public_sym.kind = "function"
        public_sym.signature_line = "func PublicFunc()"
        public_sym.start_line = 1
        public_sym.end_line = 3
        public_sym.docstring = None
        public_sym.methods = []
        mock_parsed.symbols = [private_sym, public_sym]
        mock_parsed.language = "go"
        mock_parsed.module_docstring = None

        with (
            patch("mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".go": "go"}),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file", return_value=mock_parsed
            ),
        ):
            result = get_signature("pkg.go")
        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["name"] == "PublicFunc"


# ===========================================================================
# get_code() — tree-sitter (non-Python) dispatch (line 221, 238-261)
# ===========================================================================


class TestGetCodeTreesitter:
    def test_non_python_symbol_none_returns_module_info(self, tmp_path, monkeypatch):
        """get_code for .ts with symbol=None returns module-level info via treesitter."""
        ts_file = tmp_path / "module.ts"
        ts_file.write_text("import express from 'express'")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        mock_parsed = MagicMock()
        mock_parsed.language = "typescript"
        mock_parsed.module_docstring = None
        mock_import = MagicMock()
        mock_import.module = "express"
        mock_parsed.imports = [mock_import]

        with (
            patch(
                "mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".ts": "typescript"}
            ),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file", return_value=mock_parsed
            ),
        ):
            result = get_code("module.ts", symbol=None)
        assert result["found"] is True
        assert result["kind"] == "module_info"
        assert "express" in result["imports"]

    def test_non_python_symbol_none_parse_error(self, tmp_path, monkeypatch):
        """When ts_parse_file raises in get_code symbol=None path, returns found=False."""
        ts_file = tmp_path / "broken.go"
        ts_file.write_text("invalid")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        with (
            patch("mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".go": "go"}),
            patch(
                "mcp_server.tools.code_reader.ts_parse_file",
                side_effect=FileNotFoundError("missing"),
            ),
        ):
            result = get_code("broken.go", symbol=None)
        assert result["found"] is False
        assert "missing" in result["error"]

    def test_non_python_named_symbol_lookup(self, tmp_path, monkeypatch):
        """get_code for .go with named symbol uses ts_get_symbol_source."""
        go_file = tmp_path / "handler.go"
        go_file.write_text("func HandleRequest() {}")
        monkeypatch.setattr(
            "mcp_server.tools.code_reader.get_project_root", lambda: tmp_path
        )

        mock_result = {
            "found": True,
            "source": "func HandleRequest() {}",
            "start_line": 1,
            "end_line": 1,
        }
        with (
            patch("mcp_server.tools.code_reader.TS_EXTENSION_MAP", {".go": "go"}),
            patch(
                "mcp_server.tools.code_reader.ts_get_symbol_source",
                return_value=mock_result.copy(),
            ),
        ):
            result = get_code("handler.go", symbol="HandleRequest")
        assert result["found"] is True
        assert result["file_path"] == "handler.go"
        assert "HandleRequest" in result["source"]


# ─────────────────────────────────────────────────────────────────────
# Phase 16 — TS / TSX / JS / JSX skeletons (tree-sitter)
#
# get_signature already dispatches these to tree-sitter; pin it so the
# multi-language support can't silently regress (and the roadmap can't
# mistake it for "Python-only" again).
# ─────────────────────────────────────────────────────────────────────


class TestGetSignatureWebLanguages:
    @pytest.fixture(autouse=True)
    def _require_real_grammars(self):
        # This module mocks tree_sitter for the Python-only paths; the
        # web-language tests need the REAL grammars. Skip (don't fail) when
        # the optional deps aren't installed.
        pytest.importorskip("tree_sitter_typescript")
        pytest.importorskip("tree_sitter_javascript")
        if not hasattr(sys.modules.get("tree_sitter"), "Parser"):
            pytest.skip("tree_sitter is mocked in this module; real parser unavailable")

    def _write(self, root, name, body):
        (root / name).write_text(body, encoding="utf-8")

    def test_typescript_functions_and_classes(self, mock_project_root):
        self._write(
            mock_project_root,
            "svc.ts",
            "export function greet(name: string): string {\n"
            "  return `hi ${name}`;\n"
            "}\n"
            "export class Service {\n"
            "  run(x: number): void {}\n"
            "}\n",
        )
        result = get_signature("svc.ts")
        assert result["found"] is True
        assert result["language"] == "typescript"
        names = {s["name"] for s in result["symbols"]}
        assert "greet" in names and "Service" in names

    def test_tsx_component(self, mock_project_root):
        self._write(
            mock_project_root,
            "App.tsx",
            "export function App(): JSX.Element {\n" "  return <div>hi</div>;\n" "}\n",
        )
        result = get_signature("App.tsx")
        assert result["found"] is True
        assert "App" in {s["name"] for s in result["symbols"]}

    def test_javascript_functions(self, mock_project_root):
        self._write(
            mock_project_root,
            "util.js",
            "export function add(a, b) {\n  return a + b;\n}\n",
        )
        result = get_signature("util.js")
        assert result["found"] is True
        assert result["language"] == "javascript"
        assert "add" in {s["name"] for s in result["symbols"]}

    def test_jsx_function(self, mock_project_root):
        self._write(
            mock_project_root,
            "Btn.jsx",
            "export function Btn(props) {\n  return <button>{props.label}</button>;\n}\n",
        )
        result = get_signature("Btn.jsx")
        assert result["found"] is True
        assert "Btn" in {s["name"] for s in result["symbols"]}

    def test_error_message_lists_js(self, mock_project_root):
        (mock_project_root / "data.xyz").write_text("nope", encoding="utf-8")
        result = get_signature("data.xyz")
        assert result["found"] is False
        assert ".js" in result["error"]  # accuracy: JS/JSX are advertised
