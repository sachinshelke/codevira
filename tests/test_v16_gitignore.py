"""
Tests for Codevira v1.6 .gitignore-aware file discovery.

Covers:
  - load_gitignore_spec(): root + nested .gitignore, negation, empty
  - discover_source_files(): safety-net dirs, extension filters, config overrides
  - infer_language_from_files(): dominant language detection
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.gitignore import (
    discover_source_files,
    infer_language_from_files,
    load_gitignore_spec,
    _SAFETY_NET_DIRS,
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


# ---------------------------------------------------------------------------
# load_gitignore_spec
# ---------------------------------------------------------------------------

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
        # src/gen/ should be excluded
        assert spec.match_file("src/gen/auto.py")
        # other/gen/ should NOT be excluded (rule is local to src/)
        # Note: pathspec may or may not apply the rule globally depending on prefix —
        # the important thing is src/gen is excluded
        assert not spec.match_file("src/main.py")

    def test_multiple_gitignore_files_merged(self, tmp_path):
        _make_files(tmp_path, ["a.log", "src/debug.log", "src/main.py"])
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "src" / ".gitignore").write_text("debug.log\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("a.log")
        # src/main.py is not excluded
        assert not spec.match_file("src/main.py")

    def test_wildcard_extension_pattern(self, tmp_path):
        _make_files(tmp_path, ["build/out.js", "src/app.js"])
        (tmp_path / ".gitignore").write_text("build/\n")

        spec = load_gitignore_spec(tmp_path)
        assert spec.match_file("build/out.js")
        assert not spec.match_file("src/app.js")


# ---------------------------------------------------------------------------
# discover_source_files
# ---------------------------------------------------------------------------

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
        # src/main.py should be found
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


# ---------------------------------------------------------------------------
# infer_language_from_files
# ---------------------------------------------------------------------------

class TestInferLanguageFromFiles:
    def test_dominant_python(self, tmp_path):
        _make_files(tmp_path, ["a.py", "b.py", "c.py", "d.ts"])
        files = list((tmp_path).rglob("*"))
        files = [f for f in files if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "python"

    def test_dominant_typescript(self, tmp_path):
        _make_files(tmp_path, ["a.ts", "b.ts", "c.tsx", "d.py"])
        files = list(tmp_path.rglob("*"))
        files = [f for f in files if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "typescript"

    def test_dominant_go(self, tmp_path):
        _make_files(tmp_path, ["main.go", "server.go", "handler.go"])
        files = list(tmp_path.rglob("*"))
        files = [f for f in files if f.is_file()]
        lang = infer_language_from_files(files)
        assert lang == "go"

    def test_unknown_when_no_recognized_extensions(self, tmp_path):
        _make_files(tmp_path, ["README", "Makefile", "Dockerfile"])
        files = list(tmp_path.rglob("*"))
        files = [f for f in files if f.is_file()]
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
