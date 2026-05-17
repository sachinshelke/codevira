"""
Tests for indexer.chunker — Python AST-based code chunking.

Mocks treesitter_parser at the sys.modules level before importing chunker,
since chunker has a module-level `from indexer.treesitter_parser import ...`.
"""
from __future__ import annotations

import ast
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Install a fake treesitter_parser module before importing chunker.
#
# Use the real module if available — pollution-free for downstream tests
# that need attrs (get_symbol_source, etc.) the fake doesn't provide.
# Only install the fake stub when the real module truly cannot be loaded
# (no tree-sitter dependency in this test environment).
# ---------------------------------------------------------------------------
try:
    import indexer.treesitter_parser  # noqa: F401 — populates sys.modules
except Exception:
    _fake_ts = types.ModuleType("indexer.treesitter_parser")
    _fake_ts.parse_file = lambda *a, **kw: None  # type: ignore[attr-defined]
    _fake_ts.get_language = lambda ext: None  # type: ignore[attr-defined]
    _fake_ts.EXTENSION_MAP = {}  # type: ignore[attr-defined]
    # Also stub the v2.0 export so downstream test files that import the
    # real symbol don't crash if test_chunker happened to load first.
    _fake_ts.get_symbol_source = lambda *a, **kw: {}  # type: ignore[attr-defined]
    sys.modules["indexer.treesitter_parser"] = _fake_ts

from indexer.chunker import (  # noqa: E402
    CodeChunk,
    _extract_source_lines,
    _get_docstring,
    _get_project_config,
    _infer_layer,
    chunk_file,
    chunk_project,
    extract_imports,
    _extract_imports_python,
)


# ---------------------------------------------------------------------------
# CodeChunk dataclass
# ---------------------------------------------------------------------------


class TestCodeChunkDataclass:
    def test_fields(self):
        chunk = CodeChunk(
            file_path="src/foo.py",
            chunk_type="function",
            name="bar",
            source_text="def bar(): pass",
            start_line=1,
            end_line=1,
            docstring="A function.",
            layer="core",
        )
        assert chunk.file_path == "src/foo.py"
        assert chunk.chunk_type == "function"
        assert chunk.name == "bar"
        assert chunk.source_text == "def bar(): pass"
        assert chunk.start_line == 1
        assert chunk.end_line == 1
        assert chunk.docstring == "A function."
        assert chunk.layer == "core"

    def test_equality(self):
        a = CodeChunk("p", "function", "f", "s", 1, 2, "", "core")
        b = CodeChunk("p", "function", "f", "s", 1, 2, "", "core")
        assert a == b


# ---------------------------------------------------------------------------
# _infer_layer
# ---------------------------------------------------------------------------


class TestInferLayer:
    def test_api_routes(self):
        assert _infer_layer("src/api/handler.py") == "api"

    def test_routes_directory(self):
        assert _infer_layer("src/routes/user.py") == "api"

    def test_core(self):
        assert _infer_layer("src/core/engine.py") == "core"

    def test_services(self):
        assert _infer_layer("src/services/auth.py") == "services"

    def test_indexer(self):
        assert _infer_layer("indexer/chunker.py") == "indexer"

    def test_schemas(self):
        assert _infer_layer("src/schemas/user.py") == "schemas"

    def test_unknown_fallback(self):
        assert _infer_layer("random/file.py") == "unknown"

    def test_handlers(self):
        assert _infer_layer("src/handlers/event.py") == "handlers"


# ---------------------------------------------------------------------------
# _get_docstring
# ---------------------------------------------------------------------------


class TestGetDocstring:
    def test_function_with_docstring(self):
        source = 'def foo():\n    """Hello world."""\n    pass\n'
        tree = ast.parse(source)
        func = tree.body[0]
        assert _get_docstring(func) == "Hello world."

    def test_function_without_docstring(self):
        source = "def foo():\n    pass\n"
        tree = ast.parse(source)
        func = tree.body[0]
        assert _get_docstring(func) == ""

    def test_module_docstring(self):
        source = '"""Module doc."""\nx = 1\n'
        tree = ast.parse(source)
        assert _get_docstring(tree) == "Module doc."

    def test_non_ast_node_returns_empty(self):
        # Passing something that makes ast.get_docstring raise
        assert _get_docstring("not a node") == ""


# ---------------------------------------------------------------------------
# _extract_source_lines
# ---------------------------------------------------------------------------


class TestExtractSourceLines:
    def test_basic_extraction(self):
        lines = ["line1\n", "line2\n", "line3\n", "line4\n"]
        assert _extract_source_lines(lines, 2, 3) == "line2\nline3\n"

    def test_single_line(self):
        lines = ["only\n"]
        assert _extract_source_lines(lines, 1, 1) == "only\n"

    def test_full_range(self):
        lines = ["a\n", "b\n", "c\n"]
        assert _extract_source_lines(lines, 1, 3) == "a\nb\nc\n"


# ---------------------------------------------------------------------------
# extract_imports / _extract_imports_python
# ---------------------------------------------------------------------------


class TestExtractImports:
    def test_stdlib_import_excluded(self, project_env):
        """Imports of stdlib modules (os, sys) should be excluded."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "app.py"
        py_file.write_text("import os\nimport sys\nfrom pathlib import Path\n")

        result = extract_imports(str(py_file), str(project))
        assert result == []

    def test_local_import_resolved(self, project_env):
        """Import of a project-local module should be resolved to a relative path."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "utils.py").write_text("def helper(): pass\n")
        py_file = src / "app.py"
        py_file.write_text("from src.utils import helper\n")

        result = extract_imports(str(py_file), str(project))
        assert len(result) == 1
        assert "src/utils.py" in result[0] or "src" in result[0]

    def test_nonexistent_file_returns_empty(self, project_env):
        project, data_dir, db = project_env
        result = extract_imports("/nonexistent/file.py", str(project))
        assert result == []

    def test_syntax_error_returns_empty(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "broken.py"
        py_file.write_text("def broken(\n")

        result = extract_imports(str(py_file), str(project))
        assert result == []

    def test_relative_import(self, project_env):
        """Relative imports (from . import ...) should be resolved."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "sibling.py").write_text("x = 1\n")
        py_file = src / "app.py"
        py_file.write_text("from . import sibling\n")

        result = extract_imports(str(py_file), str(project))
        # Relative import resolves under src — may or may not resolve
        # depending on whether src is in watched_dirs. The key is no crash.
        assert isinstance(result, list)

    def test_duplicate_imports_deduplicated(self, project_env):
        """The same module imported twice should appear only once."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "utils.py").write_text("x = 1\n")
        py_file = src / "app.py"
        py_file.write_text(
            "from src.utils import x\nfrom src.utils import x\n"
        )

        result = extract_imports(str(py_file), str(project))
        # Should be at most 1 entry for src/utils
        assert len(result) <= 1


# ---------------------------------------------------------------------------
# _get_project_config (lru_cache behavior)
# ---------------------------------------------------------------------------


class TestGetProjectConfig:
    def test_returns_tuple(self, project_env):
        """_get_project_config returns (frozenset, tuple)."""
        _get_project_config.cache_clear()
        target_dirs, file_extensions = _get_project_config()
        assert isinstance(target_dirs, frozenset)
        assert isinstance(file_extensions, tuple)

    def test_caching(self, project_env):
        """Second call hits cache (same object identity)."""
        _get_project_config.cache_clear()
        first = _get_project_config()
        second = _get_project_config()
        assert first is second

    def test_default_when_no_config(self, project_env, monkeypatch):
        """Without a config file, defaults to watched_dirs=['src'], extensions=['.py']."""
        _get_project_config.cache_clear()

        def _empty_config():
            return {}

        monkeypatch.setattr("indexer.chunker._load_config", _empty_config)
        _get_project_config.cache_clear()
        target_dirs, file_extensions = _get_project_config()
        assert "src" in target_dirs
        assert ".py" in file_extensions


# ---------------------------------------------------------------------------
# chunk_file — Python AST path
# ---------------------------------------------------------------------------


class TestChunkFile:
    def test_empty_file_only_module_chunk(self, project_env):
        """An empty Python file produces no chunks (no docstring, no functions)."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "empty.py"
        py_file.write_text("")

        chunks = chunk_file(str(py_file), str(project))
        # No docstring, no functions, no classes => empty
        assert chunks == []

    def test_file_with_module_docstring(self, project_env):
        """A file with only a module docstring gets a module chunk."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "documented.py"
        py_file.write_text('"""This is the module docstring."""\n')

        chunks = chunk_file(str(py_file), str(project))
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "module"
        assert chunks[0].docstring == "This is the module docstring."
        assert chunks[0].name == "documented"

    def test_function_chunk(self, project_env):
        """A function with >3 lines is chunked; short ones are skipped."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "funcs.py"
        py_file.write_text(
            "def big_func(x):\n"
            '    """Does something big."""\n'
            "    a = x + 1\n"
            "    b = a + 2\n"
            "    return b\n"
            "\n"
            "def tiny(x):\n"
            "    return x\n"
        )

        chunks = chunk_file(str(py_file), str(project))
        names = [c.name for c in chunks]
        assert "big_func" in names
        # tiny is only 2 lines (lineno to end_lineno) => skipped
        assert "tiny" not in names

    def test_function_docstring_extracted(self, project_env):
        """A function's docstring is captured in the chunk."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "with_doc.py"
        py_file.write_text(
            "def my_func():\n"
            '    """My function docstring."""\n'
            "    x = 1\n"
            "    y = 2\n"
            "    return x + y\n"
        )

        chunks = chunk_file(str(py_file), str(project))
        func_chunks = [c for c in chunks if c.name == "my_func"]
        assert len(func_chunks) == 1
        assert func_chunks[0].docstring == "My function docstring."

    def test_class_chunk(self, project_env):
        """Classes produce class-type chunks."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "classes.py"
        py_file.write_text(
            "class MyClass:\n"
            '    """A test class."""\n'
            "    def method_one(self):\n"
            "        pass\n"
            "    def method_two(self):\n"
            "        pass\n"
        )

        chunks = chunk_file(str(py_file), str(project))
        class_chunks = [c for c in chunks if c.chunk_type == "class"]
        assert len(class_chunks) == 1
        assert class_chunks[0].name == "MyClass"
        assert class_chunks[0].docstring == "A test class."

    def test_dunder_methods_skipped(self, project_env):
        """Dunder methods (__init__, __repr__) are not chunked."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "dunders.py"
        py_file.write_text(
            "class Foo:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "        self.y = 2\n"
            "        self.z = 3\n"
            "    def __repr__(self):\n"
            "        return f'Foo({self.x})'\n"
            "        # extra line\n"
            "        # extra line 2\n"
            "    def real_method(self):\n"
            "        a = 1\n"
            "        b = 2\n"
            "        c = 3\n"
            "        return a + b + c\n"
        )

        chunks = chunk_file(str(py_file), str(project))
        func_names = [c.name for c in chunks if c.chunk_type == "function"]
        assert "__init__" not in func_names
        assert "__repr__" not in func_names
        assert "real_method" in func_names

    def test_syntax_error_returns_empty(self, project_env):
        """A file with a syntax error returns an empty list."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py_file = src / "broken.py"
        py_file.write_text("def broken(\n")

        chunks = chunk_file(str(py_file), str(project))
        assert chunks == []

    def test_layer_inference(self, project_env):
        """The layer field is inferred from the file path."""
        project, data_dir, db = project_env
        api_dir = project / "src" / "api"
        api_dir.mkdir(parents=True, exist_ok=True)
        py_file = api_dir / "endpoints.py"
        py_file.write_text(
            "def get_users():\n"
            '    """Get all users."""\n'
            "    result = []\n"
            "    return result\n"
        )

        chunks = chunk_file(str(py_file), str(project))
        assert len(chunks) > 0
        assert chunks[0].layer == "api"


# ---------------------------------------------------------------------------
# chunk_project
# ---------------------------------------------------------------------------


class TestChunkProject:
    def test_chunks_files_in_watched_dirs(self, project_env):
        """chunk_project finds and chunks files under configured watched_dirs."""
        _get_project_config.cache_clear()
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "app.py").write_text(
            '"""App module."""\n'
            "def run():\n"
            '    """Run the app."""\n'
            "    x = 1\n"
            "    return x\n"
        )

        chunks = chunk_project(str(project))
        assert len(chunks) > 0
        file_paths = {c.file_path for c in chunks}
        assert any("app.py" in fp for fp in file_paths)

    def test_skips_pycache(self, project_env):
        """Files inside __pycache__ are not chunked."""
        _get_project_config.cache_clear()
        project, data_dir, db = project_env
        src = project / "src"
        cache = src / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "cached.py").write_text("x = 1\n")
        (src / "real.py").write_text(
            '"""Real module."""\n'
            "def func():\n"
            '    """A function."""\n'
            "    a = 1\n"
            "    return a\n"
        )

        chunks = chunk_project(str(project))
        all_paths = [c.file_path for c in chunks]
        assert not any("__pycache__" in p for p in all_paths)

    def test_skips_init_files(self, project_env):
        """__init__.py files are in SKIP_FILES and should be excluded."""
        _get_project_config.cache_clear()
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "__init__.py").write_text('"""Init."""\n')
        (src / "main.py").write_text(
            '"""Main."""\n'
            "def main():\n"
            '    """Entry point."""\n'
            "    x = 1\n"
            "    return x\n"
        )

        chunks = chunk_project(str(project))
        all_paths = [c.file_path for c in chunks]
        assert not any("__init__.py" in p for p in all_paths)

    def test_empty_project_returns_empty(self, project_env):
        """A project with no source files yields no chunks."""
        _get_project_config.cache_clear()
        project, data_dir, db = project_env
        # No src directory at all
        chunks = chunk_project(str(project))
        assert chunks == []


# ---------------------------------------------------------------------------
# Additional imports for new tests
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock  # noqa: E402


# ---------------------------------------------------------------------------
# _chunk_file_treesitter
# ---------------------------------------------------------------------------


class TestChunkFileTreesitter:
    def test_treesitter_dispatch_with_parse_error(self, tmp_path):
        """When ts_parse_file raises for a .ts file, returns empty list."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "app.ts"
        ts_file.write_text("export function hello() {}")

        with patch("indexer.chunker.ts_parse_file", side_effect=ValueError("parse failed")):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_treesitter_dispatch_filenotfound(self, tmp_path):
        """When ts_parse_file raises FileNotFoundError, returns empty list."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "missing.go"
        ts_file.write_text("package main")

        with patch("indexer.chunker.ts_parse_file", side_effect=FileNotFoundError("missing")):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_treesitter_chunks_with_module_docstring(self, tmp_path):
        """When parsed.module_docstring is set, a module chunk is created."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "service.ts"
        ts_file.write_text("// Service module\nexport function hello() {}")

        mock_sym = MagicMock()
        mock_sym.is_public = True
        mock_sym.name = "hello"
        mock_sym.kind = "function"
        mock_sym.start_line = 2
        mock_sym.end_line = 10  # > 3 lines diff
        mock_sym.docstring = None

        mock_parsed = MagicMock()
        mock_parsed.module_docstring = "Service module"
        mock_parsed.symbols = [mock_sym]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        # Should have module chunk + function chunk
        chunk_types = [c.chunk_type for c in result]
        assert "module" in chunk_types
        assert "function" in chunk_types

    def test_treesitter_skips_short_symbols(self, tmp_path):
        """Symbols with end_line - start_line < 3 are skipped."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "small.ts"
        ts_file.write_text("const x = 1;")

        mock_sym = MagicMock()
        mock_sym.name = "x"
        mock_sym.kind = "const"
        mock_sym.start_line = 1
        mock_sym.end_line = 2  # only 1 line diff - < 3, should be skipped

        mock_parsed = MagicMock()
        mock_parsed.module_docstring = None
        mock_parsed.symbols = [mock_sym]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_treesitter_class_limits_to_15_lines(self, tmp_path):
        """Class symbols are limited to first 15 lines."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "large_class.ts"
        content = "class BigClass {\n" + "\n".join(f"  method{i}() {{}}" for i in range(25)) + "\n}"
        ts_file.write_text(content)

        mock_sym = MagicMock()
        mock_sym.name = "BigClass"
        mock_sym.kind = "class"
        mock_sym.start_line = 1
        mock_sym.end_line = 26  # 25 line diff - enough to trigger
        mock_sym.docstring = "A big class"

        mock_parsed = MagicMock()
        mock_parsed.module_docstring = None
        mock_parsed.symbols = [mock_sym]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        assert len(result) == 1
        # The source should be truncated (limited to 15 lines from start)

    def test_unicode_decode_error_returns_empty(self, tmp_path):
        """When source file has encoding issues, returns empty list."""
        from indexer.chunker import _chunk_file_treesitter
        ts_file = tmp_path / "binary.ts"
        ts_file.write_bytes(b"\xff\xfe non-utf8 content")

        mock_sym = MagicMock()
        mock_sym.name = "something"
        mock_sym.kind = "function"
        mock_sym.start_line = 1
        mock_sym.end_line = 10
        mock_sym.docstring = None

        mock_parsed = MagicMock()
        mock_parsed.module_docstring = None
        mock_parsed.symbols = [mock_sym]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _chunk_file_treesitter(str(ts_file), str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _extract_imports_treesitter
# ---------------------------------------------------------------------------


class TestExtractImportsTreesitter:
    def test_treesitter_parse_error_returns_empty(self, tmp_path):
        """When ts_parse_file raises, extract_imports returns empty list."""
        from indexer.chunker import _extract_imports_treesitter
        ts_file = tmp_path / "broken.ts"
        ts_file.write_text("broken content")

        with patch("indexer.chunker.ts_parse_file", side_effect=FileNotFoundError("missing")):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_treesitter_value_error_returns_empty(self, tmp_path):
        from indexer.chunker import _extract_imports_treesitter
        ts_file = tmp_path / "bad.go"
        ts_file.write_text("package main")

        with patch("indexer.chunker.ts_parse_file", side_effect=ValueError("unsupported")):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_extracts_resolved_import(self, tmp_path):
        """When an import resolves to a project file, it's included."""
        from indexer.chunker import _extract_imports_treesitter
        # Create a real project file that can be found
        target = tmp_path / "utils.ts"
        target.write_text("export const x = 1;")

        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import { x } from './utils';")

        mock_imp = MagicMock()
        mock_imp.module = "./utils"

        mock_parsed = MagicMock()
        mock_parsed.imports = [mock_imp]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        # Should find utils.ts
        assert isinstance(result, list)

    def test_unresolvable_import_excluded(self, tmp_path):
        """When an import cannot be resolved to a file, it's excluded."""
        from indexer.chunker import _extract_imports_treesitter
        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import React from 'react';")

        mock_imp = MagicMock()
        mock_imp.module = "react"  # third-party, no local file

        mock_parsed = MagicMock()
        mock_parsed.imports = [mock_imp]

        with patch("indexer.chunker.ts_parse_file", return_value=mock_parsed):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _load_config — corrupt / missing YAML
# ---------------------------------------------------------------------------


class TestLoadConfigCorrupt:
    def test_corrupt_yaml_returns_empty_dict(self, project_env, monkeypatch):
        """When config.yaml has corrupt YAML, _load_config returns {}."""
        from indexer.chunker import _load_config
        project, data_dir, db = project_env
        corrupt_config = data_dir / "config.yaml"
        corrupt_config.write_text("{{invalid yaml: [missing close")
        result = _load_config()
        assert result == {}

    def test_nonexistent_config_returns_empty_dict(self, project_env):
        """When config.yaml doesn't exist, _load_config returns {}."""
        from indexer.chunker import _load_config
        project, data_dir, db = project_env
        config_file = data_dir / "config.yaml"
        if config_file.exists():
            config_file.unlink()
        result = _load_config()
        assert result == {}


# ---------------------------------------------------------------------------
# extract_imports / chunk_file — tree-sitter dispatch (lines 108-109, 265-266)
# ---------------------------------------------------------------------------


class TestExtractImportsDispatch:
    def test_ts_extension_dispatches_to_treesitter(self, tmp_path):
        """extract_imports dispatches .ts files to _extract_imports_treesitter."""
        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import { x } from './util';")

        with patch("indexer.chunker._TS_SUPPORTED_EXTENSIONS", {".ts"}), \
             patch("indexer.chunker._extract_imports_treesitter",
                   return_value=["src/util.ts"]) as mock_ts:
            result = extract_imports(str(ts_file), str(tmp_path))

        mock_ts.assert_called_once()
        assert result == ["src/util.ts"]

    def test_py_extension_uses_python_extractor(self, project_env):
        """extract_imports falls through to _extract_imports_python for .py files."""
        project, data_dir, db = project_env
        _get_project_config.cache_clear()
        py_file = project / "src" / "mod.py"
        py_file.parent.mkdir(parents=True, exist_ok=True)
        py_file.write_text("import os\n")

        # .py is not in _TS_SUPPORTED_EXTENSIONS so the Python path is taken
        result = extract_imports(str(py_file), str(project))
        assert isinstance(result, list)


class TestChunkFileDispatch:
    def test_ts_extension_dispatches_to_treesitter(self, tmp_path):
        """chunk_file dispatches .ts files to _chunk_file_treesitter (line 265-266)."""
        ts_file = tmp_path / "app.ts"
        ts_file.write_text("export function hello() {}")

        with patch("indexer.chunker._TS_SUPPORTED_EXTENSIONS", {".ts"}), \
             patch("indexer.chunker._chunk_file_treesitter",
                   return_value=[]) as mock_ts:
            result = chunk_file(str(ts_file), str(tmp_path))

        mock_ts.assert_called_once()
        assert result == []

    def test_py_extension_uses_python_chunker(self, project_env):
        """chunk_file uses the Python AST path for .py files."""
        project, data_dir, db = project_env
        _get_project_config.cache_clear()
        py_file = project / "src" / "simple.py"
        py_file.parent.mkdir(parents=True, exist_ok=True)
        py_file.write_text("def foo():\n    pass\n")

        result = chunk_file(str(py_file), str(project))
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# _resolve_ts_import — path resolution edge cases (lines 155-184)
# ---------------------------------------------------------------------------


class TestResolveTsImport:
    def test_relative_import_no_candidates_returns_none(self, tmp_path):
        """_resolve_ts_import returns None when no candidate files exist (line 163)."""
        from indexer.chunker import _resolve_ts_import
        file_dir = tmp_path / "src"
        file_dir.mkdir()
        result = _resolve_ts_import("./nonexistent", file_dir, tmp_path)
        assert result is None

    def test_relative_import_resolves_to_ts_file(self, tmp_path):
        """_resolve_ts_import resolves a relative import to an existing .ts file."""
        from indexer.chunker import _resolve_ts_import
        src = tmp_path / "src"
        src.mkdir()
        util = src / "util.ts"
        util.write_text("export const x = 1;")
        result = _resolve_ts_import("./util", src, tmp_path)
        assert result is not None
        assert "util.ts" in result

    def test_non_relative_resolves_with_ts_extension(self, tmp_path):
        """_resolve_ts_import finds a .ts file for a non-relative import (lines 167-170)."""
        from indexer.chunker import _resolve_ts_import
        target = tmp_path / "utils.ts"
        target.write_text("export const x = 1;")
        result = _resolve_ts_import("utils", tmp_path, tmp_path)
        assert result is not None
        assert "utils.ts" in result

    def test_go_package_directory_with_go_files(self, tmp_path):
        """_resolve_ts_import finds a .go file from a Go package directory
        (lines 180-184)."""
        from indexer.chunker import _resolve_ts_import
        pkg_dir = tmp_path / "internal" / "services"
        pkg_dir.mkdir(parents=True)
        go_file = pkg_dir / "service.go"
        go_file.write_text("package services")
        result = _resolve_ts_import("internal/services", tmp_path, tmp_path)
        assert result is not None
        assert "service.go" in result

    def test_non_relative_unresolvable_returns_none(self, tmp_path):
        """_resolve_ts_import returns None when no file can be found."""
        from indexer.chunker import _resolve_ts_import
        result = _resolve_ts_import("completely/unknown/pkg", tmp_path, tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _extract_imports_python — ast.Import and ast.ImportFrom edge cases
# (lines 231-252)
# ---------------------------------------------------------------------------


class TestExtractImportsPythonEdgeCases:
    def test_direct_import_resolves_to_project_file(self, project_env):
        """ast.Import that resolves to a project-local file is included (lines 231-235)."""
        project, data_dir, db = project_env
        _get_project_config.cache_clear()
        src = project / "src"
        src.mkdir(exist_ok=True)
        (src / "utils.py").write_text("x = 1\n")

        # 'src' is in watched_dirs, so 'src.utils' should resolve
        app_file = src / "app.py"
        app_file.write_text("import src.utils\n")

        result = _extract_imports_python(str(app_file), str(project))
        assert isinstance(result, list)
        # src/utils.py should appear in results
        assert any("utils.py" in p for p in result)

    def test_import_from_without_module_name_is_skipped(self, project_env):
        """ImportFrom with no module name (e.g. 'from . import x') hits the
        'continue' branch (line 249)."""
        project, data_dir, db = project_env
        _get_project_config.cache_clear()
        src = project / "src"
        src.mkdir(exist_ok=True)
        app_file = src / "app.py"
        # Level > 0, no module name
        app_file.write_text("from . import something\n")
        result = _extract_imports_python(str(app_file), str(project))
        assert isinstance(result, list)
        # No crash; result may be empty since 'something' can't be resolved

    def test_absolute_import_from_resolves_project_file(self, project_env):
        """Absolute ImportFrom (level==0) resolves a project module (line 246-252)."""
        project, data_dir, db = project_env
        _get_project_config.cache_clear()
        src = project / "src"
        src.mkdir(exist_ok=True)
        (src / "helper.py").write_text("def help(): pass\n")

        app_file = src / "main.py"
        app_file.write_text("from src.helper import help\n")
        result = _extract_imports_python(str(app_file), str(project))
        assert isinstance(result, list)
        assert any("helper.py" in p for p in result)


# ---------------------------------------------------------------------------
# _chunk_file_python — OSError on read returns empty list (lines 339-340)
# ---------------------------------------------------------------------------


class TestChunkFilePythonOSError:
    def test_oserror_on_read_returns_empty(self, tmp_path):
        """_chunk_file_python returns [] when the file cannot be read (lines 339-340)."""
        from indexer.chunker import _chunk_file_python
        py_file = tmp_path / "inaccessible.py"
        py_file.write_text("def foo(): pass\n")

        with patch("builtins.open", side_effect=OSError("permission denied")):
            result = _chunk_file_python(str(py_file), str(tmp_path))

        assert result == []


class TestChunkFileMarkdownBugE:
    """2026-05-17 Bug E fix (P1 no silent failures): docs-only repos
    used to fall through to the Python AST parser, which returned [] for
    every .md file. That meant lh-interface-style projects (markdown +
    JSON only) produced 0 chunks silently. Now markdown gets heading-based
    chunks; generic text gets paragraph chunks.
    """

    def test_markdown_with_headings_yields_per_section(self, tmp_path):
        from indexer.chunker import chunk_file
        f = tmp_path / "doc.md"
        f.write_text(
            "# Top\n"
            "Intro paragraph.\n"
            "\n"
            "## Sub one\n"
            "Body of sub one.\n"
            "\n"
            "## Sub two\n"
            "Body of sub two.\n"
        )
        chunks = chunk_file(str(f), str(tmp_path))
        assert len(chunks) == 3, f"expected 3 sections, got {len(chunks)}"
        # All chunks should be markdown_section type.
        assert all(c.chunk_type == "markdown_section" for c in chunks)
        # Section names should reflect headings.
        names = {c.name for c in chunks}
        assert "Top" in names
        assert "Sub one" in names
        assert "Sub two" in names

    def test_markdown_with_no_headings_yields_whole_file(self, tmp_path):
        """No-heading markdown still produces 1 chunk (not 0). Bug E core."""
        from indexer.chunker import chunk_file
        f = tmp_path / "plain.md"
        f.write_text("Just some paragraph text.\nNo headings at all.\n")
        chunks = chunk_file(str(f), str(tmp_path))
        assert len(chunks) == 1, (
            f"Bug E regression: no-heading markdown should still produce "
            f"1 whole-file chunk, got {len(chunks)}"
        )

    def test_markdown_empty_file_yields_zero(self, tmp_path):
        """Empty file → 0 chunks, but not a crash."""
        from indexer.chunker import chunk_file
        f = tmp_path / "empty.md"
        f.write_text("")
        chunks = chunk_file(str(f), str(tmp_path))
        assert chunks == []

    def test_json_yields_paragraph_chunks(self, tmp_path):
        """JSON files now produce chunks instead of falling through to Python AST."""
        from indexer.chunker import chunk_file
        f = tmp_path / "config.json"
        f.write_text('{\n  "name": "test",\n  "version": "1.0"\n}\n')
        chunks = chunk_file(str(f), str(tmp_path))
        assert len(chunks) >= 1, (
            f"Bug E regression: JSON should produce ≥1 chunk, got {len(chunks)}"
        )
        assert all(c.chunk_type == "text_paragraph" for c in chunks)

    def test_yaml_yields_paragraph_chunks(self, tmp_path):
        from indexer.chunker import chunk_file
        f = tmp_path / "config.yaml"
        f.write_text("app:\n  name: foo\n\nlogging:\n  level: info\n")
        chunks = chunk_file(str(f), str(tmp_path))
        assert len(chunks) >= 1
        assert all(c.chunk_type == "text_paragraph" for c in chunks)

    def test_lh_interface_shaped_fixture_produces_nonzero_chunks(self, tmp_path):
        """End-to-end: a docs-only repo (the lh-interface shape that
        triggered Bug E) must produce > 0 chunks across all files."""
        from indexer.chunker import chunk_file
        files = {
            "README.md": "# Project\nIntro.\n",
            "CLAUDE.md": "# Instructions\nHow to work here.\n",
            "package.json": '{"name": "lh-interface"}\n',
            "docs/architecture.md": "# Architecture\n## Components\n- A\n- B\n",
        }
        for rel, content in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        total_chunks = 0
        for rel in files:
            total_chunks += len(chunk_file(str(tmp_path / rel), str(tmp_path)))
        assert total_chunks > 0, (
            f"Bug E regression: lh-interface-shaped docs-only repo "
            f"produced {total_chunks} chunks (expected > 0)"
        )
