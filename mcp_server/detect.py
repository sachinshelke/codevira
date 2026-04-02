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
    """Scan file extensions to find the dominant language."""
    from collections import Counter

    counts: Counter[str] = Counter()
    skip_dirs = {".git", ".codevira", "node_modules", "__pycache__", ".venv",
                 "venv", ".tox", "dist", "build", "target", ".next", ".nuxt"}

    for path in root.rglob("*"):
        # Limit depth
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth + 1:
            continue

        # Skip ignored directories
        if any(part in skip_dirs for part in rel.parts):
            continue

        if path.is_file() and path.suffix in _EXT_TO_LANG:
            counts[_EXT_TO_LANG[path.suffix]] += 1

    if counts:
        return counts.most_common(1)[0][0]

    return "python"  # ultimate fallback


def detect_watched_dirs(root: Path, language: str) -> list[str]:
    """Detect source directories based on language conventions."""
    candidates = LANGUAGE_DIRS.get(language, [])
    found = [d for d in candidates if (root / d).is_dir()]

    if found:
        return found

    # Fallback: use "." (project root)
    return ["."]


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
