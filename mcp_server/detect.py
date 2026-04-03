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
        from mcp_server.gitignore import discover_source_files, infer_language_from_files
        files = discover_source_files(root)
        lang = infer_language_from_files(files)
        if lang != "unknown":
            return lang
    except Exception:
        pass

    # Legacy fallback: depth-limited walk
    from collections import Counter

    counts: Counter[str] = Counter()
    skip_dirs = {".git", ".codevira", "node_modules", "__pycache__", ".venv",
                 "venv", ".tox", "dist", "build", "target", ".next", ".nuxt"}

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
    "node_modules", ".git", ".codevira", "__pycache__", ".venv", "venv",
    "env", ".env", ".tox", "dist", "build", "target", ".next", ".nuxt",
    ".turbo", ".cache", "coverage", ".nyc_output", "htmlcov", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".eggs", "*.egg-info", "vendor",
    ".idea", ".vscode", "__snapshots__", ".storybook", "storybook-static",
    "public", "static", "assets", "migrations", "fixtures",
}


def detect_watched_dirs(root: Path, language: str) -> list[str]:
    """
    Detect source directories by scanning the actual project.

    Strategy:
      1. Use gitignore-aware discovery to find all source files.
      2. Extract unique top-level directories that contain source files.
      3. Fall back to convention list if nothing found.
      4. Ultimate fallback: ["."]
    """
    # Try gitignore-aware discovery first
    try:
        from mcp_server.gitignore import discover_source_files
        extensions = set(LANGUAGE_EXTENSIONS.get(language, [".py"]))
        files = discover_source_files(root)
        # Filter to language-appropriate files for better dir detection
        lang_files = [f for f in files if f.suffix.lower() in extensions]
        if not lang_files:
            lang_files = files  # fall through to all files if none match

        # Extract unique top-level dirs relative to project root
        top_dirs: set[str] = set()
        for f in lang_files:
            try:
                rel = f.relative_to(root)
                if len(rel.parts) > 1:
                    top_dirs.add(rel.parts[0])
            except ValueError:
                pass

        # Filter out noise dirs
        found = sorted(
            d for d in top_dirs
            if not d.startswith(".") and d not in _SKIP_DIRS and not d.endswith("-info")
        )
        if found:
            return found
    except Exception:
        pass

    # Legacy fallback: scan top-level dirs manually
    extensions = set(LANGUAGE_EXTENSIONS.get(language, [".py"]))
    found_legacy: list[str] = []

    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIRS or name.endswith("-info"):
                continue
            if _dir_has_sources(entry, extensions, max_depth=6):
                found_legacy.append(name)
    except PermissionError:
        pass

    if found_legacy:
        return found_legacy

    # Convention fallback
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
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in _SKIP_DIRS:
                if _dir_has_sources(entry, extensions, max_depth - 1):
                    return True
    except PermissionError:
        pass
    return False


def language_extensions(language: str) -> list[str]:
    """Get file extensions for a language."""
    return LANGUAGE_EXTENSIONS.get(language, [".py"])


def auto_detect_project(root: Path) -> dict:
    """
    Auto-detect everything needed for codevira init — zero prompts.

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
    extensions = language_extensions(language)
    collection_name = name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")

    logger.info("Auto-detected: language=%s, dirs=%s, exts=%s", language, watched_dirs, extensions)

    return {
        "name": name,
        "language": language,
        "watched_dirs": watched_dirs,
        "file_extensions": extensions,
        "collection_name": collection_name,
    }
