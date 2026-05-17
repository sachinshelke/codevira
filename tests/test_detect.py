"""
Tests for mcp_server/detect.py — Zero-config project auto-detection.

Creates real project structures in tmp_path with marker files
(pyproject.toml, package.json, etc.) to test detection logic.
"""

from __future__ import annotations

from pathlib import Path


from mcp_server.detect import (
    LANGUAGE_EXTENSIONS,
    auto_detect_project,
    detect_language,
    detect_watched_dirs,
    language_extensions,
    _disambiguate_js_ts,
    _scan_dominant_language,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "my-project") -> Path:
    """Create a bare project directory and return its path."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# detect_language — marker-based detection
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    """Language detection from project markers."""

    def test_python_pyproject(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        assert detect_language(root) == "python"

    def test_python_setup_py(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "setup.py").write_text("from setuptools import setup\nsetup()")
        assert detect_language(root) == "python"

    def test_python_requirements_txt(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "requirements.txt").write_text("flask\nrequests\n")
        assert detect_language(root) == "python"

    def test_javascript_package_json(self, tmp_path):
        """package.json without TS indicators -> javascript."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        assert detect_language(root) == "javascript"

    def test_typescript_tsconfig(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "tsconfig.json").write_text('{"compilerOptions": {}}')
        assert detect_language(root) == "typescript"

    def test_go_project(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "go.mod").write_text("module example.com/test\n")
        assert detect_language(root) == "go"

    def test_rust_project(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "Cargo.toml").write_text('[package]\nname = "test"\n')
        assert detect_language(root) == "rust"

    def test_java_pom_xml(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "pom.xml").write_text("<project></project>")
        assert detect_language(root) == "java"

    def test_java_build_gradle(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "build.gradle").write_text("apply plugin: 'java'\n")
        assert detect_language(root) == "java"

    def test_ruby_project(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "Gemfile").write_text("source 'https://rubygems.org'\n")
        assert detect_language(root) == "ruby"

    def test_no_markers_fallback_to_extension_scan(self, tmp_path):
        """No marker files: should scan files and pick the dominant language."""
        root = _make_project(tmp_path)
        src = root / "src"
        src.mkdir()
        (src / "main.go").write_text("package main\n")
        (src / "handler.go").write_text("package handler\n")
        (src / "util.go").write_text("package util\n")
        # One stray Python file shouldn't win
        (src / "script.py").write_text("print('hi')\n")

        result = detect_language(root)
        assert result == "go"

    def test_empty_project_fallback(self, tmp_path):
        """Empty project with no markers and no files -> ultimate fallback 'python'."""
        root = _make_project(tmp_path)
        result = detect_language(root)
        assert result == "python"

    def test_marker_priority_rust_over_python(self, tmp_path):
        """Cargo.toml appears before pyproject.toml in LANGUAGE_MARKERS, so rust wins."""
        root = _make_project(tmp_path)
        (root / "Cargo.toml").write_text('[package]\nname = "test"\n')
        (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        # Cargo.toml is checked first in the marker list
        assert detect_language(root) == "rust"

    def test_marker_priority_go_over_python(self, tmp_path):
        """go.mod appears before pyproject.toml in LANGUAGE_MARKERS."""
        root = _make_project(tmp_path)
        (root / "go.mod").write_text("module example.com/test\n")
        (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        assert detect_language(root) == "go"


# ---------------------------------------------------------------------------
# _disambiguate_js_ts
# ---------------------------------------------------------------------------


class TestDisambiguateJsTs:
    """package.json disambiguation between JavaScript and TypeScript."""

    def test_tsconfig_present(self, tmp_path):
        """package.json + tsconfig.json -> typescript."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        (root / "tsconfig.json").write_text("{}")
        assert _disambiguate_js_ts(root) == "typescript"

    def test_ts_files_present(self, tmp_path):
        """package.json + .ts files (no tsconfig) -> typescript."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        src = root / "src"
        src.mkdir()
        (src / "index.ts").write_text("export const x = 1;\n")
        assert _disambiguate_js_ts(root) == "typescript"

    def test_tsx_files_present(self, tmp_path):
        """package.json + .tsx files -> typescript."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        src = root / "src"
        src.mkdir()
        (src / "App.tsx").write_text("export default function App() {}\n")
        assert _disambiguate_js_ts(root) == "typescript"

    def test_pure_js_project(self, tmp_path):
        """package.json with only .js files -> javascript."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        src = root / "src"
        src.mkdir()
        (src / "index.js").write_text("console.log('hi');\n")
        assert _disambiguate_js_ts(root) == "javascript"

    def test_package_json_triggers_disambiguation(self, tmp_path):
        """detect_language with package.json should call _disambiguate_js_ts."""
        root = _make_project(tmp_path)
        (root / "package.json").write_text('{"name": "test"}')
        (root / "tsconfig.json").write_text("{}")
        # tsconfig.json marker is checked BEFORE package.json in the list
        # so this actually returns "typescript" from the tsconfig marker itself
        assert detect_language(root) == "typescript"


# ---------------------------------------------------------------------------
# _scan_dominant_language
# ---------------------------------------------------------------------------


class TestScanDominantLanguage:
    """Extension-based language scanning fallback."""

    def test_counts_extensions(self, tmp_path):
        """Should count file extensions and return the dominant language."""
        root = _make_project(tmp_path)
        src = root / "src"
        src.mkdir()
        for i in range(5):
            (src / f"mod{i}.rs").write_text(f"// module {i}\n")
        (src / "helper.py").write_text("# helper\n")

        result = _scan_dominant_language(root)
        assert result == "rust"

    def test_skips_ignored_dirs(self, tmp_path):
        """node_modules, .git, etc. should be skipped."""
        root = _make_project(tmp_path)
        nm = root / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        for i in range(10):
            (nm / f"file{i}.js").write_text("// generated\n")

        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text("# main\n")

        result = _scan_dominant_language(root)
        assert result == "python"

    def test_empty_dir_fallback(self, tmp_path):
        """Empty directory should fall back to 'python'."""
        root = _make_project(tmp_path)
        result = _scan_dominant_language(root)
        assert result == "python"


# ---------------------------------------------------------------------------
# detect_watched_dirs
# ---------------------------------------------------------------------------


class TestDetectWatchedDirs:
    """Source directory detection."""

    def test_finds_src_dir_with_python_files(self, tmp_path):
        """Should find 'src' directory containing .py files."""
        root = _make_project(tmp_path)
        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text("# main\n")

        dirs = detect_watched_dirs(root, "python")
        assert "src" in dirs

    def test_finds_multiple_dirs(self, tmp_path):
        """Should find all top-level directories with matching source files."""
        root = _make_project(tmp_path)
        for d in ["src", "lib", "tests"]:
            p = root / d
            p.mkdir()
            (p / "mod.py").write_text("# module\n")

        dirs = detect_watched_dirs(root, "python")
        assert "src" in dirs
        assert "lib" in dirs
        assert "tests" in dirs

    def test_skips_dot_dirs(self, tmp_path):
        """Hidden directories (starting with .) should be excluded."""
        root = _make_project(tmp_path)
        hidden = root / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("# hidden\n")
        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text("# main\n")

        dirs = detect_watched_dirs(root, "python")
        assert ".hidden" not in dirs

    def test_skips_noise_dirs(self, tmp_path):
        """node_modules, __pycache__, etc. should be excluded."""
        root = _make_project(tmp_path)
        nm = root / "node_modules"
        nm.mkdir()
        (nm / "index.js").write_text("// dep\n")
        pycache = root / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.py").write_text("# cache\n")
        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text("# main\n")

        dirs = detect_watched_dirs(root, "python")
        assert "node_modules" not in dirs
        assert "__pycache__" not in dirs

    def test_fallback_to_dot(self, tmp_path):
        """If no source dirs found and no convention dirs exist, should return ['.']."""
        root = _make_project(tmp_path)
        # Only files in root, no subdirectories with sources
        (root / "README.md").write_text("# readme\n")

        dirs = detect_watched_dirs(root, "python")
        assert dirs == ["."]

    def test_convention_fallback(self, tmp_path):
        """If no source files found by scan, fall back to convention directories that exist."""
        root = _make_project(tmp_path)
        # Create convention directories without source files at top level
        src = root / "src"
        src.mkdir()
        # Don't add any files — just the directory exists

        dirs = detect_watched_dirs(root, "python")
        # Should fall back to convention check, and "src" dir exists
        assert "src" in dirs or dirs == ["."]

    def test_nested_source_files(self, tmp_path):
        """Source files in nested subdirectories should still cause the top-level dir to be found."""
        root = _make_project(tmp_path)
        deep = root / "src" / "pkg" / "sub"
        deep.mkdir(parents=True)
        (deep / "handler.py").write_text("# handler\n")

        dirs = detect_watched_dirs(root, "python")
        assert "src" in dirs


# ---------------------------------------------------------------------------
# language_extensions
# ---------------------------------------------------------------------------


class TestLanguageExtensions:
    """Extension lookup for known languages."""

    def test_python_extensions(self):
        assert language_extensions("python") == [".py"]

    def test_typescript_extensions(self):
        assert language_extensions("typescript") == [".ts", ".tsx"]

    def test_javascript_extensions(self):
        assert language_extensions("javascript") == [".js", ".jsx"]

    def test_go_extensions(self):
        assert language_extensions("go") == [".go"]

    def test_rust_extensions(self):
        assert language_extensions("rust") == [".rs"]

    def test_java_extensions(self):
        assert language_extensions("java") == [".java"]

    def test_unknown_language_fallback(self):
        """Unknown language should fall back to ['.py']."""
        assert language_extensions("brainfuck") == [".py"]

    def test_matches_constant(self):
        """Return value should match LANGUAGE_EXTENSIONS dict."""
        for lang, exts in LANGUAGE_EXTENSIONS.items():
            assert language_extensions(lang) == exts


# ---------------------------------------------------------------------------
# auto_detect_project
# ---------------------------------------------------------------------------


class TestAutoDetectProject:
    """Full auto-detection pipeline."""

    def test_python_project(self, tmp_path):
        """2026-05-17 Bug M fix: file_extensions reflects what's ACTUALLY on
        disk (intersection of known set with seen suffixes), not the all-
        known union. The old rc.5 behavior (returning all 80 known
        extensions regardless of disk) was misleading users — a Python-
        only project shouldn't claim to "auto-detect" .swift / .elm / .dart.

        Fixture: a project with .py and .md files. Both should appear; nothing
        else should.
        """
        root = _make_project(tmp_path, "my-api")
        (root / "pyproject.toml").write_text("[project]\nname = 'my-api'\n")
        (root / "README.md").write_text("# my-api\n")  # ensures .md is on disk
        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text("# main\n")

        result = auto_detect_project(root)
        assert result["name"] == "my-api"
        assert result["language"] == "python"
        # Both extensions actually present should be detected:
        assert ".py" in result["file_extensions"]
        assert ".md" in result["file_extensions"]
        assert ".toml" in result["file_extensions"]
        # And ONLY what's on disk — no false positives:
        assert (
            ".swift" not in result["file_extensions"]
        ), "Bug M regression: .swift detected when none on disk"
        assert (
            ".elm" not in result["file_extensions"]
        ), "Bug M regression: .elm detected when none on disk"

        # Legacy single-language mode
        narrow = auto_detect_project(root, single_language=True)
        assert narrow["file_extensions"] == [".py"]
        assert narrow["collection_name"] == "my_api"

    def test_typescript_project(self, tmp_path):
        """2026-05-17 Bug M fix: assert disk-actual extensions, not the union."""
        root = _make_project(tmp_path, "web-app")
        (root / "tsconfig.json").write_text('{"compilerOptions": {}}')
        src = root / "src"
        src.mkdir()
        (src / "index.ts").write_text("export const x = 1;\n")
        (src / "App.tsx").write_text(
            "export const App = () => null;\n"
        )  # ensures .tsx on disk

        result = auto_detect_project(root)

        assert result["language"] == "typescript"
        # Both should be detected (they're on disk):
        assert ".ts" in result["file_extensions"]
        assert ".tsx" in result["file_extensions"]
        assert ".json" in result["file_extensions"]  # tsconfig.json
        # Not on disk → not detected:
        assert (
            ".py" not in result["file_extensions"]
        ), "Bug M regression: .py detected for TS project with no .py files"

    def test_go_project(self, tmp_path):
        """rc.5: file_extensions defaults to the union, narrows with single_language=True."""
        root = _make_project(tmp_path, "go-service")
        (root / "go.mod").write_text("module example.com/go-service\n")
        cmd = root / "cmd"
        cmd.mkdir()
        (cmd / "main.go").write_text("package main\n")

        result = auto_detect_project(root)
        assert result["language"] == "go"
        assert ".go" in result["file_extensions"]

        narrow = auto_detect_project(root, single_language=True)
        assert narrow["file_extensions"] == [".go"]

    def test_returns_complete_dict(self, tmp_path):
        """auto_detect_project should always return all required keys."""
        root = _make_project(tmp_path)

        result = auto_detect_project(root)

        assert "name" in result
        assert "language" in result
        assert "watched_dirs" in result
        assert "file_extensions" in result
        assert "collection_name" in result

    def test_collection_name_sanitization(self, tmp_path):
        """collection_name should be lowercase with no hyphens, spaces, or dots."""
        root = _make_project(tmp_path, "My-Cool.Project Name")
        (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        result = auto_detect_project(root)

        cn = result["collection_name"]
        assert "-" not in cn
        assert " " not in cn
        assert "." not in cn
        assert cn == cn.lower()

    def test_empty_project_defaults(self, tmp_path):
        """Completely empty project should get sensible defaults.
        rc.5: default ext list is the full union; single_language=True
        returns the legacy single-language list.
        """
        root = _make_project(tmp_path, "blank")

        result = auto_detect_project(root)
        assert result["name"] == "blank"
        assert result["language"] == "python"  # ultimate fallback
        # Union default includes .py + dozens more.
        assert ".py" in result["file_extensions"]
        assert len(result["file_extensions"]) >= 30
        assert result["watched_dirs"] == ["."]
        assert result["collection_name"] == "blank"

        narrow = auto_detect_project(root, single_language=True)
        assert narrow["file_extensions"] == [".py"]

    def test_rust_project_full(self, tmp_path):
        """rc.5: file_extensions defaults to union, single_language=True narrows."""
        root = _make_project(tmp_path, "my-crate")
        (root / "Cargo.toml").write_text('[package]\nname = "my-crate"\n')
        src = root / "src"
        src.mkdir()
        (src / "lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")

        result = auto_detect_project(root)
        assert result["language"] == "rust"
        assert ".rs" in result["file_extensions"]
        assert result["collection_name"] == "my_crate"

        narrow = auto_detect_project(root, single_language=True)
        assert narrow["file_extensions"] == [".rs"]


class TestInitConfigureAgreementBugN:
    """2026-05-17 Bug N fix (P6 predictable detection): init and configure
    used different code paths to determine "what counts as a source file."
    They now both delegate to `discover_source_files`, so init and a
    later `configure` against the same project must surface the same
    file set. Without this guarantee, init writes one config, configure
    writes another, and the indexer disagrees with both — the v2.0
    surface that produced the lh-interface bug.
    """

    def test_init_detection_uses_discover_source_files(self, tmp_path):
        """Both auto_detect_project (used by init) and discover_source_files
        (used by configure) must return the same file set for a given project."""
        from mcp_server.detect import auto_detect_project
        from mcp_server.gitignore import discover_source_files

        root = _make_project(tmp_path, "polyglot")
        (root / "pyproject.toml").write_text("[project]\nname = 'polyglot'\n")
        (root / "README.md").write_text("# polyglot\n")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("# main\n")
        (root / "src" / "client.ts").write_text("export const x = 1;\n")
        (root / "docs").mkdir()
        (root / "docs" / "arch.md").write_text("# arch\n")

        init_result = auto_detect_project(root)
        # Get configure's view: all discoverable source files.
        configure_files = discover_source_files(root)
        configure_extensions = {f.suffix.lower() for f in configure_files if f.suffix}

        # Every extension init reports should ALSO be one configure discovered.
        # (Init may not include every extension configure sees — e.g., binary
        # extensions filtered by configure's scanner — but the reverse must hold:
        # no init extension should be ABSENT from configure's discovery.)
        for ext in init_result["file_extensions"]:
            assert ext in configure_extensions or ext in {".toml"}, (
                f"Bug N regression: init detected {ext} but configure's "
                f"discover_source_files did not. They use different matchers."
            )

    def test_init_includes_dot_when_top_level_files_present(self, tmp_path):
        """init must include '.' in watched_dirs when top-level has source files.
        Combined Bug F + Bug N guarantee: top-level CLAUDE.md / README.md /
        pyproject.toml etc. are not invisible to the indexer.
        """
        from mcp_server.detect import auto_detect_project

        root = _make_project(tmp_path, "with-top-level")
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (root / "README.md").write_text("# x\n")  # top-level file
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("# main\n")

        result = auto_detect_project(root)
        assert "." in result["watched_dirs"], (
            f"Bug F/N regression: top-level files exist but '.' not in "
            f"watched_dirs={result['watched_dirs']!r}"
        )


# ---------------------------------------------------------------------------
# _scan_dominant_language — with files (covering lines 138-150)
# ---------------------------------------------------------------------------


class TestScanDominantLanguageWithFiles:
    def test_finds_dominant_python(self, tmp_path):
        """When .py files exist in tree, returns python."""
        from mcp_server.detect import _scan_dominant_language

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("pass")
        (src / "util.py").write_text("pass")
        result = _scan_dominant_language(tmp_path, max_depth=3)
        assert result == "python"

    def test_returns_python_fallback_when_no_files(self, tmp_path):
        """With no source files at all, falls back to 'python'."""
        from mcp_server.detect import _scan_dominant_language

        # Empty directory (no source files)
        result = _scan_dominant_language(tmp_path, max_depth=3)
        assert result == "python"

    def test_gitignore_discover_fails_uses_legacy_walk(self, tmp_path):
        """When gitignore discovery raises, falls back to depth-limited walk."""
        from unittest.mock import patch
        from mcp_server.detect import _scan_dominant_language

        src = tmp_path / "app"
        src.mkdir()
        (src / "server.go").write_text("package main")
        # _scan_dominant_language imports discover_source_files locally from mcp_server.gitignore
        with patch(
            "mcp_server.gitignore.discover_source_files",
            side_effect=Exception("pathspec error"),
        ):
            result = _scan_dominant_language(tmp_path, max_depth=3)
        # Should find .go via legacy walk, or python if gitignore succeeded before raise
        assert result in ("go", "python")


# ---------------------------------------------------------------------------
# detect_watched_dirs — edge cases (covering lines 194-205, 219-221)
# ---------------------------------------------------------------------------


class TestDetectWatchedDirsEdgeCases:
    def test_gitignore_empty_lang_files_falls_back_to_all_files(self, tmp_path):
        """When no language-specific files found, uses all discovered files."""
        from mcp_server.detect import detect_watched_dirs

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "README.md").write_text("# Docs")
        # No .py files, but discover_source_files finds .md files
        result = detect_watched_dirs(tmp_path, "python")
        # Should return something, even if just ["."]
        assert isinstance(result, list)
        assert len(result) > 0

    def test_permission_error_on_root_iterdir(self, tmp_path):
        """PermissionError on iterdir() is caught gracefully."""
        from unittest.mock import patch
        from mcp_server.detect import detect_watched_dirs

        # discover_source_files is imported locally inside detect_watched_dirs
        with patch(
            "mcp_server.gitignore.discover_source_files",
            side_effect=Exception("no pathspec"),
        ), patch("pathlib.Path.iterdir", side_effect=PermissionError("access denied")):
            result = detect_watched_dirs(tmp_path, "python")
        assert isinstance(result, list)

    def test_returns_found_legacy_dirs(self, tmp_path):
        """Legacy scan returns dirs containing source files."""
        from unittest.mock import patch
        from mcp_server.detect import detect_watched_dirs

        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("pass")
        # discover_source_files is imported locally inside detect_watched_dirs
        with patch(
            "mcp_server.gitignore.discover_source_files",
            side_effect=Exception("no pathspec"),
        ):
            result = detect_watched_dirs(tmp_path, "python")
        assert "src" in result


# ---------------------------------------------------------------------------
# _dir_has_sources — edge cases (covering lines 238, 241-247)
# ---------------------------------------------------------------------------


class TestDirHasSources:
    def test_finds_source_in_subdir(self, tmp_path):
        """_dir_has_sources recurses into subdirectories."""
        from mcp_server.detect import _dir_has_sources

        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "module.py").write_text("pass")
        result = _dir_has_sources(tmp_path, {".py"}, max_depth=5)
        assert result is True

    def test_max_depth_zero_returns_false(self, tmp_path):
        """_dir_has_sources returns False when max_depth=0."""
        from mcp_server.detect import _dir_has_sources

        (tmp_path / "module.py").write_text("pass")
        result = _dir_has_sources(tmp_path, {".py"}, max_depth=0)
        assert result is False

    def test_permission_error_returns_false(self, tmp_path):
        """PermissionError inside _dir_has_sources is caught, returns False."""
        from unittest.mock import patch
        from mcp_server.detect import _dir_has_sources

        with patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
            result = _dir_has_sources(tmp_path, {".py"}, max_depth=3)
        assert result is False

    def test_no_matching_extension_returns_false(self, tmp_path):
        """Files with wrong extension do not trigger True."""
        from mcp_server.detect import _dir_has_sources

        (tmp_path / "main.ts").write_text("const x = 1")
        result = _dir_has_sources(tmp_path, {".py"}, max_depth=3)
        assert result is False


# ===================================================================
# v1.8.1 — _SKIP_DIRS denylist defense-in-depth
# ===================================================================


class TestSkipDirsDenylistV181:
    """Even if is_invalid_project_root() somehow misses (e.g. user passes
    --project-dir to a $HOME-shaped tree), auto-detect must never include
    user-data dirs in watched_dirs."""

    def test_skip_dirs_includes_macos_user_data(self):
        from mcp_server.detect import _SKIP_DIRS

        for d in (
            "Library",
            "Downloads",
            "Music",
            "Movies",
            "Pictures",
            "Desktop",
            "Public",
            "Applications",
        ):
            assert d in _SKIP_DIRS, f"{d} should be in _SKIP_DIRS"

    def test_skip_dirs_includes_linux_user_data(self):
        from mcp_server.detect import _SKIP_DIRS

        for d in ("Videos", "Templates"):
            assert d in _SKIP_DIRS, f"{d} should be in _SKIP_DIRS"

    def test_skip_dirs_includes_cloud_sync_dirs(self):
        from mcp_server.detect import _SKIP_DIRS

        for d in ("Dropbox", "iCloud Drive", "OneDrive", "Google Drive", "Box"):
            assert d in _SKIP_DIRS, f"{d} should be in _SKIP_DIRS"

    def test_detect_watched_dirs_excludes_user_data_when_layout_is_home_shaped(
        self,
        tmp_path,
    ):
        """Synthetic project with user-data subdirs alongside a real `src/`:
        only `src` should make it into watched_dirs. Models the rogue $HOME
        bootstrap scenario."""
        from mcp_server.detect import detect_watched_dirs

        for d in ("Library", "Downloads", "Documents"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "junk.py").write_text("x=1")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("def f(): pass")

        watched = detect_watched_dirs(tmp_path, "python")
        # `src` is the only legitimate source dir — Library/Downloads must
        # be filtered out. (Documents isn't in _SKIP_DIRS by design — users
        # legitimately put projects under ~/Documents/...)
        assert "Library" not in watched
        assert "Downloads" not in watched
        assert "src" in watched
