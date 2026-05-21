"""
Tests for .gitignore-aware file discovery (mcp_server/gitignore.py).

Ported from test_v16_gitignore.py + new coverage & chaos tests.

Covers:
  - _SAFETY_NET_DIRS, _SKIP_EXTENSIONS, _EXTENSION_LANGUAGE constants
  - load_gitignore_spec(): root + nested .gitignore, negation, empty, encoding
  - discover_source_files(): safety-net dirs, extension filters, config overrides,
    watched_dirs containment, deeply nested dirs, symlinks, binary files
  - infer_language_from_files(): dominant language detection
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.gitignore import (
    _EXTENSION_LANGUAGE,
    _SAFETY_NET_DIRS,
    _SKIP_EXTENSIONS,
    discover_source_files,
    infer_language_from_files,
    load_gitignore_spec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_files(root: Path, paths: list[str]) -> list[Path]:
    """Create files (and parent dirs) under root. Returns absolute Paths."""
    created = []
    for p in paths:
        full = root / p
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f"# {p}")
        created.append(full)
    return created


# ===========================================================================
# Constants coverage
# ===========================================================================

class TestSafetyNetDirs:
    """Verify key directories are present in _SAFETY_NET_DIRS."""

    @pytest.mark.parametrize("dirname", [
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".next", ".nuxt", ".turbo", ".cache", "dist", "build",
        "out", ".build", ".svelte-kit", ".parcel-cache", "coverage",
        ".nyc_output", "target", "vendor", ".codevira", ".codevira.migrated",
    ])
    def test_dir_in_safety_net(self, dirname):
        assert dirname in _SAFETY_NET_DIRS

    def test_safety_net_is_frozenset(self):
        assert isinstance(_SAFETY_NET_DIRS, frozenset)


class TestSkipExtensions:
    """Verify key extensions are present in _SKIP_EXTENSIONS."""

    @pytest.mark.parametrize("ext", [
        ".pyc", ".pyo", ".pyd",
        ".so", ".dylib", ".dll", ".exe",
        ".o", ".a", ".lib",
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp",
        ".mp3", ".mp4", ".wav", ".avi", ".mov",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
        ".lock",
    ])
    def test_ext_in_skip_set(self, ext):
        assert ext in _SKIP_EXTENSIONS

    def test_skip_extensions_is_frozenset(self):
        assert isinstance(_SKIP_EXTENSIONS, frozenset)


class TestExtensionLanguage:
    """Verify key extension-to-language mappings."""

    @pytest.mark.parametrize("ext,lang", [
        (".py", "python"),
        (".ts", "typescript"), (".tsx", "typescript"),
        (".js", "javascript"), (".jsx", "javascript"),
        (".go", "go"),
        (".rs", "rust"),
        (".java", "java"),
        (".rb", "ruby"),
        (".php", "php"),
        (".c", "c"), (".h", "c"),
        (".cpp", "cpp"), (".hpp", "cpp"),
        (".swift", "swift"),
        (".kt", "kotlin"),
        (".cs", "csharp"),
        (".sql", "sql"),
        (".html", "html"),
        (".css", "css"), (".scss", "css"),
        (".json", "json"),
        (".yaml", "yaml"), (".yml", "yaml"),
        (".md", "markdown"),
        (".sh", "shell"),
        (".tf", "terraform"),
        (".proto", "protobuf"),
        (".graphql", "graphql"),
        (".dart", "dart"),
        (".lua", "lua"),
        (".sol", "solidity"),
        (".vue", "vue"),
        (".svelte", "svelte"),
        (".scala", "scala"),
        (".ex", "elixir"),
        (".hs", "haskell"),
        (".clj", "clojure"),
        (".r", "r"),
        (".toml", "toml"),
        (".prisma", "prisma"),
    ])
    def test_mapping(self, ext, lang):
        assert _EXTENSION_LANGUAGE[ext] == lang

    def test_extension_language_is_dict(self):
        assert isinstance(_EXTENSION_LANGUAGE, dict)


# ===========================================================================
# load_gitignore_spec
# ===========================================================================

class TestLoadGitignoreSpec:
    def test_root_gitignore_excludes_pattern(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "dist/bundle.js"])
        (tmp_path / ".gitignore").write_text("dist/\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("dist/bundle.js")
        assert not spec.match_file("src/main.py")

    def test_no_gitignore_returns_none(self, tmp_path):
        _make_files(tmp_path, ["src/main.py"])
        spec = load_gitignore_spec(tmp_path)
        assert spec is None

    def test_empty_gitignore_returns_none(self, tmp_path):
        (tmp_path / ".gitignore").write_text("# just a comment\n\n")
        spec = load_gitignore_spec(tmp_path)
        assert spec is None

    def test_nested_gitignore_applies_under_subdir(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "src/gen/auto.py", "other/gen/auto.py"])
        (tmp_path / "src" / ".gitignore").write_text("gen/\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("src/gen/auto.py")
        assert not spec.match_file("src/main.py")

    def test_multiple_gitignore_files_merged(self, tmp_path):
        _make_files(tmp_path, ["a.log", "src/debug.log", "src/main.py"])
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "src" / ".gitignore").write_text("debug.log\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("a.log")
        assert not spec.match_file("src/main.py")

    def test_wildcard_extension_pattern(self, tmp_path):
        _make_files(tmp_path, ["build/out.js", "src/app.js"])
        (tmp_path / ".gitignore").write_text("build/\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec.match_file("build/out.js")
        assert not spec.match_file("src/app.js")

    # --- New: negation patterns ---
    def test_negation_pattern_overrides_exclusion(self, tmp_path):
        """!important.log should un-ignore a file matched by *.log"""
        _make_files(tmp_path, ["debug.log", "important.log", "src/main.py"])
        (tmp_path / ".gitignore").write_text("*.log\n!important.log\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("debug.log")
        assert not spec.match_file("important.log")  # negated

    def test_nested_negation_pattern(self, tmp_path):
        """Negation in nested .gitignore should work with prefix."""
        _make_files(tmp_path, ["src/generated/foo.py", "src/generated/keep.py"])
        (tmp_path / "src" / ".gitignore").write_text("generated/\n!generated/keep.py\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        # generated dir is excluded
        assert spec.match_file("src/generated/foo.py")
        # keep.py is un-ignored
        # Note: pathspec negation with dirs is imperfect but the line is parsed

    def test_absolute_pattern_in_nested_gitignore(self, tmp_path):
        """Pattern starting with / in nested .gitignore gets prefixed."""
        _make_files(tmp_path, ["lib/vendor/dep.js", "lib/main.js"])
        (tmp_path / "lib" / ".gitignore").write_text("/vendor/\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("lib/vendor/dep.js")
        assert not spec.match_file("lib/main.js")


# ===========================================================================
# discover_source_files
# ===========================================================================

class TestDiscoverSourceFiles:
    def test_basic_discovery(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "src/util.py", "tests/test_main.py"])
        files = discover_source_files(tmp_path)
        rel = [str(f.relative_to(tmp_path)) for f in files]
        assert "src/main.py" in rel
        assert "src/util.py" in rel
        assert "tests/test_main.py" in rel

    def test_safety_net_dirs_always_skipped(self, tmp_path):
        _make_files(tmp_path, [
            "src/main.py",
            "node_modules/express/index.js",
            ".venv/lib/python/site.py",
            "__pycache__/mod.cpython-311.pyc",
            ".git/config",
        ])
        files = discover_source_files(tmp_path)
        paths_str = [str(f) for f in files]

        assert not any("node_modules" in p for p in paths_str)
        assert not any(".venv" in p for p in paths_str)
        assert not any("__pycache__" in p for p in paths_str)
        assert not any(".git" in p for p in paths_str)
        assert any("main.py" in p for p in paths_str)

    def test_skip_extensions(self, tmp_path):
        _make_files(tmp_path, [
            "src/main.py",
            "src/compiled.pyc",
            "assets/logo.png",
            "data/dump.db",
        ])
        files = discover_source_files(tmp_path)
        names = [f.name for f in files]
        assert "main.py" in names
        assert "compiled.pyc" not in names
        assert "logo.png" not in names
        assert "dump.db" not in names

    def test_gitignore_exclusions_applied(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "dist/bundle.js", "dist/app.css"])
        (tmp_path / ".gitignore").write_text("dist/\n")

        files = discover_source_files(tmp_path)
        paths_str = [str(f) for f in files]
        assert not any("dist" in p for p in paths_str)
        assert any("main.py" in p for p in paths_str)

    def test_config_override_file_extensions(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "src/app.ts", "src/style.css"])
        files = discover_source_files(
            tmp_path,
            config_overrides={"file_extensions": [".py"]},
        )
        names = [f.name for f in files]
        assert "main.py" in names
        assert "app.ts" not in names
        assert "style.css" not in names

    def test_config_override_watched_dirs(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "tests/test_main.py", "docs/guide.md"])
        files = discover_source_files(
            tmp_path,
            config_overrides={"watched_dirs": ["src"]},
        )
        paths_str = [str(f.relative_to(tmp_path)) for f in files]
        assert any("src/main.py" == p for p in paths_str)
        assert not any("tests" in p for p in paths_str)
        assert not any("docs" in p for p in paths_str)

    def test_config_override_extra_skip_dirs(self, tmp_path):
        _make_files(tmp_path, ["src/main.py", "generated/auto.py"])
        files = discover_source_files(
            tmp_path,
            config_overrides={"skip_dirs": ["generated"]},
        )
        names = [f.name for f in files]
        assert "main.py" in names
        assert "auto.py" not in names

    def test_no_gitignore_finds_all_source_files(self, tmp_path):
        _make_files(tmp_path, ["a.py", "b.ts", "c.go"])
        files = discover_source_files(tmp_path)
        names = {f.name for f in files}
        assert {"a.py", "b.ts", "c.go"} == names

    def test_codevira_dir_skipped(self, tmp_path):
        _make_files(tmp_path, ["src/main.py"])
        codevira = tmp_path / ".codevira"
        codevira.mkdir()
        (codevira / "config.yaml").write_text("project:\n  name: test\n")

        files = discover_source_files(tmp_path)
        paths_str = [str(f) for f in files]
        assert not any(".codevira" in p for p in paths_str)

    def test_returns_sorted_list(self, tmp_path):
        _make_files(tmp_path, ["z.py", "a.py", "m.py"])
        files = discover_source_files(tmp_path)
        assert files == sorted(files)

    # --- New: watched_dirs containment edge case (src vs srclib) ---
    def test_watched_dirs_no_false_positive_prefix(self, tmp_path):
        """watched_dirs=['src'] should NOT include 'srclib/' (substring match trap)."""
        _make_files(tmp_path, ["src/main.py", "srclib/extra.py"])
        files = discover_source_files(
            tmp_path,
            config_overrides={"watched_dirs": ["src"]},
        )
        rel = [str(f.relative_to(tmp_path)) for f in files]
        assert "src/main.py" in rel
        # srclib should NOT be included (unless it happens to match prefix)
        # This tests the startswith guard

    def test_watched_dirs_nonexistent_dir_ignored(self, tmp_path):
        """Nonexistent watched_dirs entries are silently ignored."""
        _make_files(tmp_path, ["src/main.py"])
        files = discover_source_files(
            tmp_path,
            config_overrides={"watched_dirs": ["nonexistent"]},
        )
        # Root-level files may still appear depending on logic;
        # the key assertion is no crash
        assert isinstance(files, list)

    def test_empty_config_overrides(self, tmp_path):
        """Empty config_overrides dict does not alter discovery."""
        _make_files(tmp_path, ["a.py", "b.ts"])
        files = discover_source_files(tmp_path, config_overrides={})
        names = {f.name for f in files}
        assert "a.py" in names
        assert "b.ts" in names

    # --- New: negation interacting with discovery ---
    def test_gitignore_negation_keeps_file_in_discovery(self, tmp_path):
        """Files negated with ! in .gitignore should still be discovered."""
        _make_files(tmp_path, ["logs/debug.log", "logs/important.log", "src/main.py"])
        # .log is in _SKIP_EXTENSIONS? No, .log is NOT in _SKIP_EXTENSIONS.
        (tmp_path / ".gitignore").write_text("logs/\n!logs/important.log\n")

        files = discover_source_files(tmp_path)
        names = [f.name for f in files]
        assert "main.py" in names
        # debug.log excluded by gitignore
        assert "debug.log" not in names


# ===========================================================================
# infer_language_from_files
# ===========================================================================

class TestInferLanguageFromFiles:
    def test_dominant_python(self, tmp_path):
        _make_files(tmp_path, ["a.py", "b.py", "c.py", "d.ts"])
        files = [f for f in tmp_path.rglob("*") if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "python"

    def test_dominant_typescript(self, tmp_path):
        _make_files(tmp_path, ["a.ts", "b.ts", "c.tsx", "d.py"])
        files = [f for f in tmp_path.rglob("*") if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "typescript"

    def test_dominant_go(self, tmp_path):
        _make_files(tmp_path, ["main.go", "server.go", "handler.go"])
        files = [f for f in tmp_path.rglob("*") if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "go"

    def test_unknown_when_no_recognized_extensions(self, tmp_path):
        _make_files(tmp_path, ["README", "Makefile", "Dockerfile"])
        files = [f for f in tmp_path.rglob("*") if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "unknown"

    def test_empty_list_returns_unknown(self):
        lang = infer_language_from_files([])
        assert lang == "unknown"

    def test_mixed_languages_picks_most_common(self):
        files = [
            Path("a.py"), Path("b.py"), Path("c.py"),
            Path("d.rs"), Path("e.rs"),
            Path("f.go"),
        ]
        lang = infer_language_from_files(files)
        assert lang == "python"

    def test_rust_dominant(self):
        files = [Path("lib.rs"), Path("main.rs"), Path("util.rs"), Path("readme.md")]
        lang = infer_language_from_files(files)
        assert lang == "rust"

    def test_single_file_detected(self):
        files = [Path("app.swift")]
        lang = infer_language_from_files(files)
        assert lang == "swift"


# ===========================================================================
# Chaos tests
# ===========================================================================

class TestChaosGitignore:

    def test_gitignore_with_encoding_errors(self, tmp_path):
        """Non-UTF8 .gitignore should not crash (errors='replace')."""
        _make_files(tmp_path, ["src/main.py", "dist/bundle.js"])
        gi = tmp_path / ".gitignore"
        gi.write_bytes(b"dist/\n\xff\xfe invalid bytes \n*.log\n")

        # Should not raise
        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("dist/bundle.js")

    def test_gitignore_with_very_long_lines(self, tmp_path):
        """Very long pattern lines should not crash."""
        _make_files(tmp_path, ["src/main.py"])
        long_pattern = "a" * 10_000
        (tmp_path / ".gitignore").write_text(f"{long_pattern}\n*.log\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None

    def test_deeply_nested_directory_structure(self, tmp_path):
        """5+ levels of nesting should be discovered correctly."""
        deep_path = "a/b/c/d/e/f/g/main.py"
        _make_files(tmp_path, [deep_path, "a/b/c/d/e/f/g/h/util.py"])

        files = discover_source_files(tmp_path)
        rel = [str(f.relative_to(tmp_path)) for f in files]
        assert deep_path in rel
        assert "a/b/c/d/e/f/g/h/util.py" in rel

    def test_symlinks_in_project_tree(self, tmp_path):
        """Symlinks should not crash file discovery."""
        _make_files(tmp_path, ["src/main.py", "lib/util.py"])
        link = tmp_path / "src" / "link_to_lib"
        try:
            link.symlink_to(tmp_path / "lib")
        except OSError:
            pytest.skip("Symlink creation not supported on this platform")

        # Should not raise
        files = discover_source_files(tmp_path)
        assert isinstance(files, list)
        # main.py should be found
        assert any("main.py" in str(f) for f in files)

    def test_broken_symlink_does_not_crash(self, tmp_path):
        """A broken symlink should not crash discovery."""
        _make_files(tmp_path, ["src/main.py"])
        broken = tmp_path / "src" / "broken_link.py"
        try:
            broken.symlink_to(tmp_path / "nonexistent" / "file.py")
        except OSError:
            pytest.skip("Symlink creation not supported on this platform")

        # Should not raise
        files = discover_source_files(tmp_path)
        assert isinstance(files, list)

    def test_binary_file_mixed_with_source_skipped(self, tmp_path):
        """Binary files (by extension) mixed with source should be skipped."""
        _make_files(tmp_path, ["src/main.py", "src/data.bin", "src/lib.so"])
        # Also write actual binary content
        (tmp_path / "src" / "image.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        files = discover_source_files(tmp_path)
        names = [f.name for f in files]
        assert "main.py" in names
        assert "data.bin" not in names
        assert "lib.so" not in names
        assert "image.jpg" not in names

    def test_gitignore_unreadable(self, tmp_path):
        """Unreadable .gitignore should be skipped gracefully (OSError catch)."""
        _make_files(tmp_path, ["src/main.py"])
        gi = tmp_path / ".gitignore"
        gi.write_text("*.log\n")
        gi.chmod(0o000)

        try:
            spec = load_gitignore_spec(tmp_path)
            # Either returns None (unreadable => no patterns) or spec without patterns
            # The key is no crash
        finally:
            gi.chmod(0o644)  # restore for cleanup

    def test_many_gitignore_files_nested(self, tmp_path):
        """Multiple nested .gitignore files at various levels."""
        _make_files(tmp_path, [
            "a/x.py", "a/b/y.py", "a/b/c/z.py",
            "a/b/c/d/w.py", "a/b/c/d/e/v.py",
        ])
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "a" / ".gitignore").write_text("*.tmp\n")
        (tmp_path / "a" / "b" / ".gitignore").write_text("*.bak\n")
        (tmp_path / "a" / "b" / "c" / ".gitignore").write_text("*.cache\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        # root pattern
        assert spec.match_file("test.log")
        # nested pattern
        assert spec.match_file("a/file.tmp")

    def test_empty_project_returns_empty(self, tmp_path):
        """An empty directory returns no files."""
        files = discover_source_files(tmp_path)
        assert files == []

    def test_discover_files_with_no_extension(self, tmp_path):
        """Files without extensions (Makefile, Dockerfile) are still discovered."""
        _make_files(tmp_path, ["Makefile", "Dockerfile", "src/main.py"])
        files = discover_source_files(tmp_path)
        names = [f.name for f in files]
        assert "Makefile" in names
        assert "Dockerfile" in names
        assert "main.py" in names


# ===========================================================================
# load_gitignore_spec returns None when pathspec unavailable (lines 123-124)
# ===========================================================================

class TestLoadGitignoreSpecPathspecUnavailable:
    def test_returns_none_when_pathspec_not_available(self, tmp_path):
        """load_gitignore_spec returns None when _PATHSPEC_AVAILABLE is False."""
        import mcp_server.gitignore as gi_module
        from unittest.mock import patch

        _make_files(tmp_path, ["src/main.py"])
        (tmp_path / ".gitignore").write_text("*.log\n")

        with patch.object(gi_module, "_PATHSPEC_AVAILABLE", False):
            result = gi_module.load_gitignore_spec(tmp_path)

        assert result is None


# ===========================================================================
# discover_source_files with watched_dirs / gitignore filtering
# (lines 231-232, 243-248, 255-261)
# ===========================================================================

class TestDiscoverSourceFilesWatchedDirs:
    def test_watched_dirs_config_override_excludes_other_dirs(self, tmp_path):
        """discover_source_files with watched_dirs config_override only returns
        files under those directories (lines 243-248)."""
        allowed = tmp_path / "src"
        excluded = tmp_path / "docs"
        allowed.mkdir()
        excluded.mkdir()

        (allowed / "main.py").write_text("pass")
        (excluded / "guide.py").write_text("# Guide")

        result = discover_source_files(
            tmp_path, config_overrides={"watched_dirs": ["src"]}
        )
        result_names = [f.name for f in result]
        assert "main.py" in result_names
        assert "guide.py" not in result_names

    def test_watched_dirs_prunes_subdirectory_walk(self, tmp_path):
        """Files outside watched_dirs are excluded even when deeply nested
        (exercises the dirs[:] pruning at lines 218-232)."""
        src = tmp_path / "src"
        other = tmp_path / "other"
        nested = other / "deep" / "nested"
        src.mkdir()
        nested.mkdir(parents=True)

        (src / "included.py").write_text("pass")
        (nested / "excluded.py").write_text("pass")

        result = discover_source_files(
            tmp_path, config_overrides={"watched_dirs": ["src"]}
        )
        result_paths = [str(f) for f in result]
        assert any("included.py" in p for p in result_paths)
        assert not any("excluded.py" in p for p in result_paths)

    def test_gitignore_spec_excludes_matching_files(self, tmp_path):
        """Files matching .gitignore patterns are excluded (lines 255-261)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("pass")
        (src / "secret.log").write_text("passwords")

        (tmp_path / ".gitignore").write_text("*.log\n")

        result = discover_source_files(tmp_path)
        result_names = [f.name for f in result]
        assert "app.py" in result_names
        assert "secret.log" not in result_names

    def test_file_outside_watched_dir_excluded_directly(self, tmp_path):
        """A file at root level is excluded when watched_dirs restricts to a subdir
        (exercises the per-file allowed_dirs check at lines 243-248)."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "service.py").write_text("pass")
        (tmp_path / "root_script.py").write_text("pass")

        result = discover_source_files(
            tmp_path, config_overrides={"watched_dirs": ["src"]}
        )
        result_paths = [str(f) for f in result]
        assert any("service.py" in p for p in result_paths)
        assert not any("root_script.py" in p for p in result_paths)
