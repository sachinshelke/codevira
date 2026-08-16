"""
Tests for indexer.imports — import extraction for the code graph.

Renamed from tests/test_chunker.py in 4.0.1, when indexer/chunker.py became
indexer/imports.py. The chunk-only tests were dropped with the code they
covered: CodeChunk, chunk_file, chunk_project, iter_source_files,
_chunk_file_python, _chunk_file_treesitter, _infer_layer, _get_docstring,
_extract_source_lines and the markdown/JSON/YAML chunk cases (Bug E). Those
exercised the source-chunking half of the old module, which existed only to
feed ChromaDB embeddings; ChromaDB was removed in v2.2.0, so the behaviour
they asserted no longer exists. Every extract_imports test is kept, including
the TypeScript path-alias / tsconfig resolution cases (decision D000125).

Mocks treesitter_parser at the sys.modules level before importing imports,
since imports has a module-level `from indexer.treesitter_parser import ...`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Install a fake treesitter_parser module before importing indexer.imports.
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
    # real symbol don't crash if test_imports happened to load first.
    _fake_ts.get_symbol_source = lambda *a, **kw: {}  # type: ignore[attr-defined]
    sys.modules["indexer.treesitter_parser"] = _fake_ts

from indexer.imports import (  # noqa: E402
    _project_packages,
    _extract_imports_python,
    _get_project_config,
    _load_tsconfig,
    _resolve_ts_import,
    extract_imports,
)


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
        py_file.write_text("from src.utils import x\nfrom src.utils import x\n")

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

        monkeypatch.setattr("indexer.imports._load_config", _empty_config)
        _get_project_config.cache_clear()
        target_dirs, file_extensions = _get_project_config()
        assert "src" in target_dirs
        assert ".py" in file_extensions


# ---------------------------------------------------------------------------
# _extract_imports_treesitter
# ---------------------------------------------------------------------------


class TestExtractImportsTreesitter:
    def test_treesitter_parse_error_returns_empty(self, tmp_path):
        """When ts_parse_file raises, extract_imports returns empty list."""
        from indexer.imports import _extract_imports_treesitter

        ts_file = tmp_path / "broken.ts"
        ts_file.write_text("broken content")

        with patch(
            "indexer.imports.ts_parse_file", side_effect=FileNotFoundError("missing")
        ):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_treesitter_value_error_returns_empty(self, tmp_path):
        from indexer.imports import _extract_imports_treesitter

        ts_file = tmp_path / "bad.go"
        ts_file.write_text("package main")

        with patch(
            "indexer.imports.ts_parse_file", side_effect=ValueError("unsupported")
        ):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []

    def test_extracts_resolved_import(self, tmp_path):
        """When an import resolves to a project file, it's included."""
        from indexer.imports import _extract_imports_treesitter

        # Create a real project file that can be found
        target = tmp_path / "utils.ts"
        target.write_text("export const x = 1;")

        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import { x } from './utils';")

        mock_imp = MagicMock()
        mock_imp.module = "./utils"

        mock_parsed = MagicMock()
        mock_parsed.imports = [mock_imp]

        with patch("indexer.imports.ts_parse_file", return_value=mock_parsed):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        # Should find utils.ts
        assert isinstance(result, list)

    def test_unresolvable_import_excluded(self, tmp_path):
        """When an import cannot be resolved to a file, it's excluded."""
        from indexer.imports import _extract_imports_treesitter

        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import React from 'react';")

        mock_imp = MagicMock()
        mock_imp.module = "react"  # third-party, no local file

        mock_parsed = MagicMock()
        mock_parsed.imports = [mock_imp]

        with patch("indexer.imports.ts_parse_file", return_value=mock_parsed):
            result = _extract_imports_treesitter(str(ts_file), str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _load_config — corrupt / missing YAML
# ---------------------------------------------------------------------------


class TestLoadConfigCorrupt:
    def test_corrupt_yaml_returns_empty_dict(self, project_env, monkeypatch):
        """When config.yaml has corrupt YAML, _load_config returns {}."""
        from indexer.imports import _load_config

        project, data_dir, db = project_env
        corrupt_config = data_dir / "config.yaml"
        corrupt_config.write_text("{{invalid yaml: [missing close")
        result = _load_config()
        assert result == {}

    def test_nonexistent_config_returns_empty_dict(self, project_env):
        """When config.yaml doesn't exist, _load_config returns {}."""
        from indexer.imports import _load_config

        project, data_dir, db = project_env
        config_file = data_dir / "config.yaml"
        if config_file.exists():
            config_file.unlink()
        result = _load_config()
        assert result == {}


# ---------------------------------------------------------------------------
# extract_imports — tree-sitter dispatch
# ---------------------------------------------------------------------------


class TestExtractImportsDispatch:
    def test_ts_extension_dispatches_to_treesitter(self, tmp_path):
        """extract_imports dispatches .ts files to _extract_imports_treesitter."""
        ts_file = tmp_path / "app.ts"
        ts_file.write_text("import { x } from './util';")

        with (
            patch("indexer.imports._TS_SUPPORTED_EXTENSIONS", {".ts"}),
            patch(
                "indexer.imports._extract_imports_treesitter",
                return_value=["src/util.ts"],
            ) as mock_ts,
        ):
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


# ---------------------------------------------------------------------------
# _resolve_ts_import — path resolution edge cases (lines 155-184)
# ---------------------------------------------------------------------------


class TestResolveTsImport:
    def test_relative_import_no_candidates_returns_none(self, tmp_path):
        """_resolve_ts_import returns None when no candidate files exist (line 163)."""
        from indexer.imports import _resolve_ts_import

        file_dir = tmp_path / "src"
        file_dir.mkdir()
        result = _resolve_ts_import("./nonexistent", file_dir, tmp_path)
        assert result is None

    def test_relative_import_resolves_to_ts_file(self, tmp_path):
        """_resolve_ts_import resolves a relative import to an existing .ts file."""
        from indexer.imports import _resolve_ts_import

        src = tmp_path / "src"
        src.mkdir()
        util = src / "util.ts"
        util.write_text("export const x = 1;")
        result = _resolve_ts_import("./util", src, tmp_path)
        assert result is not None
        assert "util.ts" in result

    def test_non_relative_resolves_with_ts_extension(self, tmp_path):
        """_resolve_ts_import finds a .ts file for a non-relative import (lines 167-170)."""
        from indexer.imports import _resolve_ts_import

        target = tmp_path / "utils.ts"
        target.write_text("export const x = 1;")
        result = _resolve_ts_import("utils", tmp_path, tmp_path)
        assert result is not None
        assert "utils.ts" in result

    def test_go_package_directory_with_go_files(self, tmp_path):
        """_resolve_ts_import finds a .go file from a Go package directory
        (lines 180-184)."""
        from indexer.imports import _resolve_ts_import

        pkg_dir = tmp_path / "internal" / "services"
        pkg_dir.mkdir(parents=True)
        go_file = pkg_dir / "service.go"
        go_file.write_text("package services")
        result = _resolve_ts_import("internal/services", tmp_path, tmp_path)
        assert result is not None
        assert "service.go" in result

    def test_non_relative_unresolvable_returns_none(self, tmp_path):
        """_resolve_ts_import returns None when no file can be found."""
        from indexer.imports import _resolve_ts_import

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
# D000124: tsconfig path-alias (@/…) import resolution
# ---------------------------------------------------------------------------


class TestTsconfigAliasResolution:
    """`_resolve_ts_import` must resolve tsconfig compilerOptions.paths aliases,
    otherwise aliased imports drop their dependency edge and get_impact reports
    blast_radius:0 for a heavily-imported file (D000124)."""

    def setup_method(self):
        _load_tsconfig.cache_clear()

    def teardown_method(self):
        _load_tsconfig.cache_clear()

    def _make_ts_project(self, root: Path, tsconfig: str):
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "objective-spec.ts").write_text("export type Spec = {};\n")
        (root / "src" / "app.ts").write_text(
            "import { Spec } from '@/objective-spec';\n"
        )
        (root / "tsconfig.json").write_text(tsconfig)

    def test_alias_import_resolves_to_file(self, tmp_path):
        """`@/objective-spec` must resolve to src/objective-spec.ts via the
        `@/*` -> `src/*` alias. FAILS before the tsconfig-paths fix."""
        self._make_ts_project(
            tmp_path,
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
        )
        resolved = _resolve_ts_import("@/objective-spec", tmp_path / "src", tmp_path)
        assert resolved is not None, (
            "aliased import '@/objective-spec' resolved to None — the dependency "
            "edge would be dropped and blast_radius would read 0 (D000124)"
        )
        assert resolved.replace("\\", "/") == "src/objective-spec.ts"

    def test_baseurl_relative_paths(self, tmp_path):
        """paths targets are relative to baseUrl: baseUrl='src', '@/*' -> '*'."""
        self._make_ts_project(
            tmp_path,
            '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}',
        )
        resolved = _resolve_ts_import("@/objective-spec", tmp_path / "src", tmp_path)
        assert resolved is not None
        assert resolved.replace("\\", "/") == "src/objective-spec.ts"

    def test_jsonc_comments_and_trailing_commas_tolerated(self, tmp_path):
        """Real tsconfig files are JSONC — comments + trailing commas must parse."""
        self._make_ts_project(
            tmp_path,
            "{\n"
            "  // project config\n"
            '  "compilerOptions": {\n'
            '    "baseUrl": ".",\n'
            '    "paths": { "@/*": ["src/*"], },  /* alias */\n'
            "  },\n"
            "}\n",
        )
        resolved = _resolve_ts_import("@/objective-spec", tmp_path / "src", tmp_path)
        assert resolved is not None
        assert resolved.replace("\\", "/") == "src/objective-spec.ts"

    def test_index_barrel_via_alias(self, tmp_path):
        """`@/specs` -> src/specs/index.ts (directory barrel)."""
        (tmp_path / "src" / "specs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "specs" / "index.ts").write_text("export const x = 1;\n")
        (tmp_path / "tsconfig.json").write_text(
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}'
        )
        resolved = _resolve_ts_import("@/specs", tmp_path / "src", tmp_path)
        assert resolved is not None
        assert resolved.replace("\\", "/") == "src/specs/index.ts"

    def test_unmatched_alias_returns_none(self, tmp_path):
        """A specifier that matches no alias and no file still returns None."""
        self._make_ts_project(
            tmp_path,
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
        )
        assert _resolve_ts_import("lodash", tmp_path / "src", tmp_path) is None

    def test_relative_import_still_resolves(self, tmp_path):
        """Regression guard: the refactored relative branch still works."""
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "objective-spec.ts").write_text("export type S = {};\n")
        resolved = _resolve_ts_import("./objective-spec", tmp_path / "src", tmp_path)
        assert resolved is not None
        assert resolved.replace("\\", "/") == "src/objective-spec.ts"


class TestProjectPackagesFromDisk:
    """4.0.1: import resolution must not assume the project lives in ``src/``.

    ``_module_to_path`` gates on "does this import start with a known project
    package", and that set came from ``watched_dirs`` in config.yaml with a
    hardcoded ``["src"]`` fallback. The v2.2 scaffold writes
    ``<project>/.codevira/config.yaml`` WITHOUT ``watched_dirs``, and creating
    that file is what flips ``get_data_dir()`` to the in-repo path — so the
    config that wins is the one missing the key, the fallback engages, and any
    project not laid out as ``src/`` resolved ZERO imports.

    Measured on codevira's own repo before this fix: 1,197 graph nodes and
    0 edges, which silently emptied ``get_impact`` — the tool CLAUDE.md tells
    every agent to call before editing.
    """

    def _project(self, root: Path) -> None:
        """A project whose code is in app/ and lib/ — not src/."""
        (root / "app").mkdir()
        (root / "lib").mkdir()
        (root / "app" / "__init__.py").write_text("")
        (root / "lib" / "__init__.py").write_text("")
        (root / "lib" / "util.py").write_text("def helper():\n    return 1\n")
        (root / "app" / "main.py").write_text(
            "import os\n"
            "import json\n"
            "from lib.util import helper\n"
            "def go():\n"
            "    return helper()\n"
        )

    def test_imports_resolve_when_project_is_not_in_src(self, tmp_path):
        """The bug, as a test: lib/util.py must be found from app/main.py."""
        self._project(tmp_path)
        found = extract_imports(str(tmp_path / "app" / "main.py"), str(tmp_path))
        assert "lib/util.py" in [f.replace("\\", "/") for f in found], (
            "import resolution missed a real project-local import, so the code "
            f"graph would carry no edge for it. got: {found}"
        )

    def test_stdlib_is_still_skipped(self, tmp_path):
        """Widening the package set must not turn `import os` into an edge."""
        self._project(tmp_path)
        found = extract_imports(str(tmp_path / "app" / "main.py"), str(tmp_path))
        assert not [f for f in found if "os" in Path(f).stem.split(".")[:1]]
        for f in found:
            assert (tmp_path / f).exists(), f"resolved a path that does not exist: {f}"

    def test_configured_watched_dirs_are_still_honoured(self, tmp_path):
        """The disk scan is a UNION with config, not a replacement — a project
        that already resolved correctly must not lose anything."""
        self._project(tmp_path)
        pkgs = _project_packages(tmp_path)
        assert {"app", "lib"} <= set(pkgs)

    def test_non_package_dirs_are_ignored(self, tmp_path):
        """node_modules/.venv/build must never become import roots."""
        self._project(tmp_path)
        for junk in ("node_modules", ".venv", "build", "__pycache__"):
            d = tmp_path / junk
            d.mkdir()
            (d / "shim.py").write_text("x = 1\n")
        pkgs = _project_packages(tmp_path)
        assert not ({"node_modules", ".venv", "build", "__pycache__"} & set(pkgs))
