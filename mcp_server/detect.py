"""
detect.py — Zero-config project auto-detection.

Detects language, source directories, file extensions, and project name
from project markers (package.json, go.mod, Cargo.toml, etc.) with zero
interactive prompts. Supports 15+ languages.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language marker map — checked in order, first match wins
# ---------------------------------------------------------------------------

LANGUAGE_MARKERS: list[tuple[str, str]] = [
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("tsconfig.json", "typescript"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
    ("pom.xml", "java"),
    ("build.gradle.kts", "kotlin"),
    ("build.gradle", "java"),
    ("Gemfile", "ruby"),
    ("Package.swift", "swift"),
    ("composer.json", "php"),
    ("CMakeLists.txt", "cpp"),
    # package.json last — needs disambiguation between TS and JS
    ("package.json", "_js_or_ts"),
]

# ---------------------------------------------------------------------------
# Per-language conventions
# ---------------------------------------------------------------------------

LANGUAGE_DIRS: dict[str, list[str]] = {
    "python": ["src", "lib", "app"],
    "typescript": ["src", "lib", "app", "pages", "components"],
    "javascript": ["src", "lib", "app", "pages", "components"],
    "go": ["cmd", "pkg", "internal"],
    "rust": ["src"],
    "java": ["src/main/java", "src"],
    "kotlin": ["src/main/kotlin", "src"],
    "ruby": ["lib", "app"],
    "csharp": ["src"],
    "cpp": ["src", "include", "lib"],
    "c": ["src", "include", "lib"],
    "swift": ["Sources", "src"],
    "php": ["src", "app", "lib"],
}

LANGUAGE_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx"],
    "go": [".go"],
    "rust": [".rs"],
    "java": [".java"],
    "kotlin": [".kt", ".kts"],
    "ruby": [".rb"],
    "csharp": [".cs"],
    "cpp": [".cpp", ".cc", ".cxx", ".h", ".hpp"],
    "c": [".c", ".h"],
    "swift": [".swift"],
    "php": [".php"],
    "solidity": [".sol"],
    "vue": [".vue"],
}

# Reverse map: extension → language (for fallback scanning)
_EXT_TO_LANG: dict[str, str] = {}
for _lang, _exts in LANGUAGE_EXTENSIONS.items():
    for _ext in _exts:
        if _ext not in _EXT_TO_LANG:  # first language wins
            _EXT_TO_LANG[_ext] = _lang


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------


def detect_language(root: Path) -> str:
    """Detect primary language from project markers. Falls back to file extension scan."""
    root = root.resolve()

    for marker, lang in LANGUAGE_MARKERS:
        if (root / marker).exists():
            if lang == "_js_or_ts":
                return _disambiguate_js_ts(root)
            return lang

    # Fallback: scan files 2 levels deep, count extensions
    return _scan_dominant_language(root)


def _disambiguate_js_ts(root: Path) -> str:
    """Determine if a package.json project is TypeScript or JavaScript."""
    if (root / "tsconfig.json").exists():
        return "typescript"

    # Check for any .ts/.tsx files in first 2 levels
    for depth_glob in ["*.ts", "*.tsx", "*/*.ts", "*/*.tsx", "*/*/*.ts"]:
        if list(root.glob(depth_glob)):
            return "typescript"

    return "javascript"


def _scan_dominant_language(root: Path, max_depth: int = 2) -> str:
    """Scan file extensions to find the dominant language.

    Uses gitignore-aware discovery when pathspec is available, falls back
    to a simple depth-limited walk otherwise.
    """
    try:
        from mcp_server.gitignore import (
            discover_source_files,
            infer_language_from_files,
        )

        files = discover_source_files(root)
        lang = infer_language_from_files(files)
        if lang != "unknown":
            return lang
    except Exception:
        pass

    # Legacy fallback: depth-limited walk
    from collections import Counter

    counts: Counter[str] = Counter()
    skip_dirs = {
        ".git",
        ".codevira",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
    }

    for path in root.rglob("*"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth + 1:
            continue
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.is_file() and path.suffix in _EXT_TO_LANG:
            counts[_EXT_TO_LANG[path.suffix]] += 1

    if counts:
        return counts.most_common(1)[0][0]

    return "python"  # ultimate fallback


# Top-level directories that never contain user code
_SKIP_DIRS: set[str] = {
    "node_modules",
    ".git",
    ".codevira",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    ".tox",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    "coverage",
    ".nyc_output",
    "htmlcov",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".eggs",
    "*.egg-info",
    "vendor",
    ".idea",
    ".vscode",
    "__snapshots__",
    ".storybook",
    "storybook-static",
    "public",
    "static",
    "assets",
    "migrations",
    "fixtures",
    # v1.8.1 — refuse macOS/Linux user-data dirs and cloud-sync top-levels.
    # These show up as "subdirs" if a user accidentally bootstraps a
    # project at $HOME (see crash-log analysis 2026-04-24). Even with
    # is_invalid_project_root() refusing $HOME at the bootstrap layer,
    # this denylist is defense-in-depth: it stops auto-detect from ever
    # putting user-data trees under watch even on edge-case layouts.
    # macOS user-data dirs (typical ~ subdirs)
    "Library",
    "Downloads",
    "Music",
    "Movies",
    "Pictures",
    "Desktop",
    "Public",
    "Applications",
    # Linux user-data dirs (typical ~ subdirs)
    "Videos",
    "Templates",
    # Cloud-sync top-levels (often ~ subdirs; spaces preserved as in macOS UI)
    "Dropbox",
    "iCloud Drive",
    "OneDrive",
    "Google Drive",
    "Box",
}


def detect_watched_dirs(root: Path, language: str) -> list[str]:
    """
    Detect source directories by scanning the actual project.

    Strategy:
      1. Use gitignore-aware discovery to find all source files.
      2. Filter against the BROAD extension set (not single-language) —
         a Python project with docs/, migrations/, configs/ etc. should
         see those dirs surface too. (Bug F fix.)
      3. Extract unique top-level directories that contain matching files.
      4. Include "." in the result when top-level files match (Bug F
         variant — root-level CLAUDE.md, README.md etc. were dropped
         because the old code only added dirs deeper than the root).
      5. Fall back to convention list if nothing found.
      6. Ultimate fallback: ["."]

    P6 (predictable detection): this function and ``auto_detect_project``
    now agree on what counts as a source file. They use the same broad
    extension set, so "discovery says N files; indexing says 0 files"
    can no longer happen for the per-language case.
    """
    # Broad extension set — matches what auto_detect_project uses for
    # indexing. Without this match, detect_watched_dirs and the indexer
    # diverge and produce the configure-vs-index mismatch (Bug A).
    broad_extensions = set(_ALL_SOURCE_EXTENSIONS)

    # Try gitignore-aware discovery first.
    try:
        from mcp_server.gitignore import discover_source_files

        files = discover_source_files(root)
        # Filter to BROAD-extension files (was: single-language only).
        # Bug L+F: previously a Python project with only docs/ would
        # report "Source dirs: docs" missing, because the matcher only
        # accepted .py files and docs/ has none.
        matching_files = [f for f in files if f.suffix.lower() in broad_extensions]
        if not matching_files:
            matching_files = files  # fall through if even the broad set misses

        # Extract unique top-level dirs relative to project root.
        # Bug F variant: include "." when top-level files exist.
        # Previously len(rel.parts) > 1 filter skipped root files entirely.
        top_dirs: set[str] = set()
        has_top_level_files = False
        for f in matching_files:
            try:
                rel = f.relative_to(root)
                if len(rel.parts) == 1:
                    has_top_level_files = True
                else:
                    top_dirs.add(rel.parts[0])
            except ValueError:
                pass

        # Filter out noise dirs.
        found = sorted(
            d
            for d in top_dirs
            if not d.startswith(".") and d not in _SKIP_DIRS and not d.endswith("-info")
        )
        # Prepend "." if top-level has matching files. (Bug F fix.)
        if has_top_level_files:
            found = ["."] + found
        if found:
            return found
    except Exception as exc:
        # P4 (defensive parsing): log but don't crash. Fall through to
        # the manual scan below.
        logger.warning(
            "discover_source_files failed for %s: %s — falling back to manual scan",
            root,
            exc,
        )

    # Legacy fallback: scan top-level dirs manually using broad extensions
    # (was: single-language only — same Bug F/L surface).
    found_legacy: list[str] = []
    legacy_has_top_level = False
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix.lower() in broad_extensions:
                legacy_has_top_level = True
                continue
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIRS or name.endswith("-info"):
                continue
            if _dir_has_sources(entry, broad_extensions, max_depth=6):
                found_legacy.append(name)
    except PermissionError:
        pass

    if legacy_has_top_level:
        found_legacy = ["."] + found_legacy
    if found_legacy:
        return found_legacy

    # Convention fallback.
    candidates = LANGUAGE_DIRS.get(language, [])
    convention = [d for d in candidates if (root / d).is_dir()]
    if convention:
        return convention

    return ["."]


def _dir_has_sources(path: Path, extensions: set[str], max_depth: int) -> bool:
    """Return True if path contains at least one file with a matching extension."""
    if max_depth == 0:
        return False
    try:
        for entry in path.iterdir():
            if entry.is_file() and entry.suffix in extensions:
                return True
            if (
                entry.is_dir()
                and not entry.name.startswith(".")
                and entry.name not in _SKIP_DIRS
            ):
                if _dir_has_sources(entry, extensions, max_depth - 1):
                    return True
    except PermissionError:
        pass
    return False


def language_extensions(language: str) -> list[str]:
    """Get file extensions for a language."""
    return LANGUAGE_EXTENSIONS.get(language, [".py"])


# rc.5 (P1-2 / "index all the code" question): the union of every meaningful
# source / config / docs extension across all languages we know about. This
# is what `auto_detect_project` returns by default now — narrowing to one
# language's extensions was the cause of polyglot codebases having .yaml,
# .md, .html, .css, .go etc. silently dropped from the index.
_ALL_SOURCE_EXTENSIONS: list[str] = sorted(
    {
        # Code
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".java",
        ".kt",
        ".scala",
        ".swift",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".m",
        ".mm",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".lua",
        ".pl",
        ".r",
        ".jl",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".clj",
        ".cljs",
        ".elm",
        ".hs",
        ".ml",
        ".mli",
        ".v",
        # Config / data — often where the architecture lives
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".jsonl",
        ".xml",
        ".ini",
        ".env",
        # Schemas / IDL
        ".proto",
        ".graphql",
        ".prisma",
        ".sql",
        ".thrift",
        ".cap",
        # Docs
        ".md",
        ".mdx",
        ".rst",
        ".adoc",
        ".txt",
        # Web
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
        ".astro",
        # Build
        ".dockerfile",
        ".gradle",
        ".bazel",
    }
)


def all_source_extensions() -> list[str]:
    """rc.5: full union of source/config/docs extensions across all known languages.

    Used as the default for ``auto_detect_project`` so polyglot projects don't
    silently lose half their files to single-language narrowing.
    """
    return list(_ALL_SOURCE_EXTENSIONS)


def auto_detect_project(root: Path, *, single_language: bool = False) -> dict:
    """
    Auto-detect everything needed for codevira init — zero prompts.

    rc.5 (P1-2 + "index all the code"): default behavior is now to index the
    UNION of every meaningful source/config/docs extension we know about, plus
    every extension actually present in the project. The ``language`` label
    still picks the dominant tree-sitter parser for symbol extraction, but
    extension filtering no longer drops .yaml/.md/.html/.go from a "Python"
    project. Pass ``single_language=True`` to restore the legacy narrowing
    (only that language's extensions).

    Returns:
        {
            "name": str,
            "language": str,
            "watched_dirs": list[str],
            "file_extensions": list[str],
            "collection_name": str,
        }
    """
    root = root.resolve()
    name = root.name
    language = detect_language(root)
    watched_dirs = detect_watched_dirs(root, language)
    if single_language:
        extensions = language_extensions(language)
    else:
        # Bug M (P1 fix): return ONLY extensions actually present on disk,
        # intersected with our known set. Previously this returned the
        # union of all ~80 known extensions regardless of what was on disk,
        # so a Python-only project saw ".swift, .elm, .dart" in its
        # "Auto-detected" output and rightly lost trust.
        #
        # The new logic:
        #   seen_on_disk = every extension actually present
        #   extensions   = intersection with known set, PLUS any seen-on-disk
        #                  extension that isn't in our known set (defends
        #                  against project-specific .myext that we'd
        #                  otherwise drop)
        seen_on_disk: set[str] = set()
        try:
            from mcp_server.gitignore import discover_source_files

            for f in discover_source_files(root):
                if f.suffix:
                    seen_on_disk.add(f.suffix.lower())
        except Exception as exc:
            # P4 (defensive parsing): log but never crash detection.
            logger.warning(
                "discover_source_files failed in auto_detect_project: %s", exc
            )

        known_set = set(_ALL_SOURCE_EXTENSIONS)
        # Intersection of known + on-disk gives us the curated extensions
        # the project actually uses. Plus any project-specific extensions
        # (suffixes seen on disk but not in our known list).
        in_both = known_set & seen_on_disk
        unknown_but_seen = seen_on_disk - known_set
        # If we found NOTHING on disk (edge case: empty repo), fall back
        # to the broad set so init still produces a usable config that
        # could pick up files added later.
        if seen_on_disk:
            extensions = sorted(in_both | unknown_but_seen)
        else:
            extensions = sorted(known_set)
            logger.info(
                "auto_detect_project: no files seen on disk — defaulting to broad extension set"
            )
    collection_name = name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")

    logger.info(
        "Auto-detected: language=%s, dirs=%s, exts=%d files",
        language,
        watched_dirs,
        len(extensions),
    )

    return {
        "name": name,
        "language": language,
        "watched_dirs": watched_dirs,
        "file_extensions": extensions,
        "collection_name": collection_name,
    }
