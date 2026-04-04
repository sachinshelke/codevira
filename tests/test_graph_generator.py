"""
Tests for indexer.graph_generator — graph node generation and SQLite graph building.

Mocks treesitter_parser at the sys.modules level before importing graph_generator,
since it has a module-level `from indexer.treesitter_parser import ...`.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Install a fake treesitter_parser module before importing graph_generator.
# graph_generator also imports from indexer.chunker which itself imports
# treesitter_parser, so the fake must be in place before either is loaded.
# ---------------------------------------------------------------------------
_fake_ts = types.ModuleType("indexer.treesitter_parser")
_fake_ts.parse_file = lambda *a, **kw: None  # type: ignore[attr-defined]
_fake_ts.get_language = lambda ext: None  # type: ignore[attr-defined]
_fake_ts.EXTENSION_MAP = {}  # type: ignore[attr-defined]

# ParsedSymbol is referenced by _get_python_symbols_detailed at runtime
from dataclasses import dataclass, field as _field


@dataclass
class _FakeParsedSymbol:
    name: str
    kind: str
    signature_line: str
    start_line: int
    end_line: int
    docstring: str | None = None
    is_public: bool = True
    methods: list[str] = _field(default_factory=list)


_fake_ts.ParsedSymbol = _FakeParsedSymbol  # type: ignore[attr-defined]
sys.modules.setdefault("indexer.treesitter_parser", _fake_ts)

from indexer.graph_generator import (  # noqa: E402
    _infer_layer,
    _get_python_docstring,
    _get_python_public_symbols,
    generate_graph_node,
    generate_graph_sqlite,
)
from indexer.sqlite_graph import SQLiteGraph  # noqa: E402


# ---------------------------------------------------------------------------
# _infer_layer
# ---------------------------------------------------------------------------


class TestInferLayer:
    def test_api_handler(self):
        assert _infer_layer("src/api/handler.py") == "api"

    def test_controllers(self):
        assert _infer_layer("src/controllers/user.py") == "api"

    def test_routes(self):
        assert _infer_layer("src/routes/auth.py") == "api"

    def test_routers(self):
        assert _infer_layer("src/routers/items.py") == "api"

    def test_models(self):
        assert _infer_layer("src/models/user.py") == "database"

    def test_db_directory(self):
        assert _infer_layer("src/db/connection.py") == "database"

    def test_schemas(self):
        assert _infer_layer("src/schemas/order.py") == "database"

    def test_services(self):
        assert _infer_layer("src/services/auth.py") == "service"

    def test_core(self):
        assert _infer_layer("src/core/engine.py") == "service"

    def test_utils(self):
        assert _infer_layer("src/utils/helpers.py") == "utility"

    def test_helpers(self):
        assert _infer_layer("src/helpers/formatting.py") == "utility"

    def test_common(self):
        assert _infer_layer("src/common/constants.py") == "utility"

    def test_frontend(self):
        assert _infer_layer("src/frontend/app.py") == "frontend"

    def test_components(self):
        assert _infer_layer("src/components/button.py") == "frontend"

    def test_test_file(self):
        assert _infer_layer("tests/test_api.py") == "test"

    def test_test_in_path(self):
        assert _infer_layer("src/test_utils/mock.py") == "test"

    def test_fallback_core(self):
        assert _infer_layer("src/main.py") == "core"

    def test_plain_file(self):
        assert _infer_layer("setup.py") == "core"


# ---------------------------------------------------------------------------
# _get_python_docstring
# ---------------------------------------------------------------------------


class TestGetPythonDocstring:
    def test_file_with_docstring(self, tmp_path):
        py = tmp_path / "documented.py"
        py.write_text('"""This is the first line.\nSecond line here."""\nx = 1\n')
        result = _get_python_docstring(str(py))
        assert result == "This is the first line."

    def test_file_without_docstring(self, tmp_path):
        py = tmp_path / "no_doc.py"
        py.write_text("x = 1\ny = 2\n")
        result = _get_python_docstring(str(py))
        assert result is None

    def test_file_with_syntax_error(self, tmp_path):
        py = tmp_path / "broken.py"
        py.write_text("def broken(\n")
        result = _get_python_docstring(str(py))
        assert result is None

    def test_nonexistent_file(self, tmp_path):
        result = _get_python_docstring(str(tmp_path / "nope.py"))
        assert result is None

    def test_single_line_docstring(self, tmp_path):
        py = tmp_path / "single.py"
        py.write_text('"""Just one line."""\n')
        result = _get_python_docstring(str(py))
        assert result == "Just one line."


# ---------------------------------------------------------------------------
# _get_python_public_symbols
# ---------------------------------------------------------------------------


class TestGetPythonPublicSymbols:
    def test_functions_and_classes(self, tmp_path):
        py = tmp_path / "mixed.py"
        py.write_text(
            "def public_func():\n"
            "    pass\n"
            "\n"
            "class PublicClass:\n"
            "    pass\n"
        )
        result = _get_python_public_symbols(str(py))
        assert "public_func" in result
        assert "PublicClass" in result

    def test_private_excluded(self, tmp_path):
        py = tmp_path / "private.py"
        py.write_text(
            "def _private_func():\n"
            "    pass\n"
            "\n"
            "class _PrivateClass:\n"
            "    pass\n"
            "\n"
            "def public_one():\n"
            "    pass\n"
        )
        result = _get_python_public_symbols(str(py))
        assert "_private_func" not in result
        assert "_PrivateClass" not in result
        assert "public_one" in result

    def test_syntax_error_empty_list(self, tmp_path):
        py = tmp_path / "broken.py"
        py.write_text("class Broken(\n")
        result = _get_python_public_symbols(str(py))
        assert result == []

    def test_empty_file(self, tmp_path):
        py = tmp_path / "empty.py"
        py.write_text("")
        result = _get_python_public_symbols(str(py))
        assert result == []

    def test_nested_functions_not_included(self, tmp_path):
        """Only top-level definitions are listed."""
        py = tmp_path / "nested.py"
        py.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        pass\n"
        )
        result = _get_python_public_symbols(str(py))
        assert "outer" in result
        # inner is nested inside outer's body — not a top-level body statement
        assert "inner" not in result


# ---------------------------------------------------------------------------
# generate_graph_node
# ---------------------------------------------------------------------------


class TestGenerateGraphNode:
    def test_python_file(self, project_env):
        project, data_dir, db = project_env
        src = project / "src" / "services"
        src.mkdir(parents=True, exist_ok=True)
        py = src / "auth.py"
        py.write_text(
            '"""Authentication service."""\n'
            "\n"
            "def login(user, password):\n"
            "    pass\n"
            "\n"
            "def logout(user):\n"
            "    pass\n"
            "\n"
            "def _hash_password(pw):\n"
            "    pass\n"
        )

        node = generate_graph_node("src/services/auth.py", str(project))
        assert node["file_path"] == "src/services/auth.py"
        assert node["layer"] == "service"
        assert node["role"] == "Authentication service."
        assert "login" in node["key_functions"]
        assert "logout" in node["key_functions"]
        assert "_hash_password" not in node["key_functions"]

    def test_nonexistent_file_returns_empty(self, project_env):
        project, data_dir, db = project_env
        node = generate_graph_node("does/not/exist.py", str(project))
        assert node == {}

    def test_layer_correct_for_api(self, project_env):
        project, data_dir, db = project_env
        api_dir = project / "src" / "api"
        api_dir.mkdir(parents=True, exist_ok=True)
        py = api_dir / "routes.py"
        py.write_text("def get_items(): pass\n")

        node = generate_graph_node("src/api/routes.py", str(project))
        assert node["layer"] == "api"
        assert node["type"] == "component"

    def test_utility_type(self, project_env):
        project, data_dir, db = project_env
        util_dir = project / "src" / "utils"
        util_dir.mkdir(parents=True, exist_ok=True)
        py = util_dir / "helpers.py"
        py.write_text("def format_string(s): pass\n")

        node = generate_graph_node("src/utils/helpers.py", str(project))
        assert node["layer"] == "utility"
        assert node["type"] == "utility"

    def test_database_layer_high_stability(self, project_env):
        project, data_dir, db = project_env
        models_dir = project / "src" / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        py = models_dir / "user.py"
        py.write_text(
            '"""User model."""\n'
            "class User:\n"
            "    pass\n"
        )

        node = generate_graph_node("src/models/user.py", str(project))
        assert node["layer"] == "database"
        assert node["stability"] == "high"

    def test_role_with_no_docstring(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py = src / "plain.py"
        py.write_text("x = 1\n")

        node = generate_graph_node("src/plain.py", str(project))
        # No docstring => default role based on layer
        assert node["role"].startswith("Handles")
        assert node["role"].endswith(".")

    def test_role_ends_with_period(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py = src / "nodot.py"
        py.write_text('"""No trailing period"""\n')

        node = generate_graph_node("src/nodot.py", str(project))
        assert node["role"].endswith(".")

    def test_auto_generated_flag(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        py = src / "any.py"
        py.write_text("x = 1\n")

        node = generate_graph_node("src/any.py", str(project))
        assert node["auto_generated"] is True
        assert node["do_not_revert"] is False


# ---------------------------------------------------------------------------
# generate_graph_sqlite
# ---------------------------------------------------------------------------


class TestGenerateGraphSqlite:
    def test_creates_nodes_for_python_files(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "alpha.py").write_text(
            '"""Alpha module."""\n'
            "def do_alpha():\n"
            "    pass\n"
        )
        (src / "beta.py").write_text(
            '"""Beta module."""\n'
            "def do_beta():\n"
            "    pass\n"
        )

        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)

        assert stats["nodes_added"] >= 2
        assert stats["files_processed"] >= 2

        # Verify nodes exist in the DB
        verify_db = SQLiteGraph(db_path)
        node_a = verify_db.get_node("file:src/alpha.py")
        node_b = verify_db.get_node("file:src/beta.py")
        assert node_a is not None
        assert node_b is not None
        assert node_a["layer"] is not None
        verify_db.close()

    def test_skips_node_modules(self, project_env):
        project, data_dir, db = project_env
        nm = project / "node_modules" / "pkg"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "index.py").write_text("x = 1\n")
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "app.py").write_text("y = 1\n")

        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)

        verify_db = SQLiteGraph(db_path)
        node_nm = verify_db.get_node("file:node_modules/pkg/index.py")
        assert node_nm is None
        verify_db.close()

    def test_skips_venv(self, project_env):
        project, data_dir, db = project_env
        venv = project / ".venv" / "lib"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "something.py").write_text("x = 1\n")
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "main.py").write_text("y = 1\n")

        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)

        verify_db = SQLiteGraph(db_path)
        node_venv = verify_db.get_node("file:.venv/lib/something.py")
        assert node_venv is None
        verify_db.close()

    def test_does_not_duplicate_existing_nodes(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "existing.py").write_text("x = 1\n")

        db_path = str(data_dir / "graph" / "graph.db")

        # Pre-add a node so it already exists
        db.add_node(
            "file:src/existing.py", "file", "existing.py", "src/existing.py",
            layer="core", role="Already here.",
        )
        db.close()

        stats = generate_graph_sqlite(str(project), db_path)
        assert stats["nodes_skipped"] >= 1

        # Verify original node was not overwritten
        verify_db = SQLiteGraph(db_path)
        node = verify_db.get_node("file:src/existing.py")
        assert node is not None
        assert node["role"] == "Already here."
        verify_db.close()

    def test_returns_stats_dict(self, project_env):
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "one.py").write_text("a = 1\n")

        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)

        assert "nodes_added" in stats
        assert "nodes_skipped" in stats
        assert "files_processed" in stats
        assert "edges_added" in stats
        assert isinstance(stats["nodes_added"], int)
        assert isinstance(stats["nodes_skipped"], int)
        assert isinstance(stats["files_processed"], int)

    def test_empty_project_returns_zeros(self, project_env):
        """A project with no Python files produces zero stats."""
        project, data_dir, db = project_env
        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)
        assert stats["nodes_added"] == 0
        assert stats["nodes_skipped"] == 0
        assert stats["files_processed"] == 0

    def test_symbols_populated(self, project_env):
        """generate_graph_sqlite populates function-level symbols."""
        project, data_dir, db = project_env
        src = project / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "with_funcs.py").write_text(
            "def public_func():\n"
            '    """Does stuff."""\n'
            "    x = 1\n"
            "    return x\n"
            "\n"
            "class PublicClass:\n"
            "    pass\n"
        )

        db_path = str(data_dir / "graph" / "graph.db")
        db.close()
        stats = generate_graph_sqlite(str(project), db_path)

        assert stats["symbols_added"] >= 1

        verify_db = SQLiteGraph(db_path)
        syms = verify_db.get_symbols_for_file("file:src/with_funcs.py")
        sym_names = [s["name"] for s in syms]
        assert "public_func" in sym_names
        verify_db.close()
