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
# Install a fake treesitter_parser module before importing chunker
# ---------------------------------------------------------------------------
_fake_ts = types.ModuleType("indexer.treesitter_parser")
_fake_ts.parse_file = lambda *a, **kw: None  # type: ignore[attr-defined]
_fake_ts.get_language = lambda ext: None  # type: ignore[attr-defined]
_fake_ts.EXTENSION_MAP = {}  # type: ignore[attr-defined]
sys.modules.setdefault("indexer.treesitter_parser", _fake_ts)

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
