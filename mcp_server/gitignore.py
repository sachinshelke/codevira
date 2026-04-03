"""
gitignore.py — .gitignore-aware file discovery for Codevira v1.6.

Replaces the old "scan fixed watched_dirs" approach with a model that:
  1. Walks the entire project tree
  2. Respects .gitignore + nested .gitignore files (via pathspec)
  3. Always skips a safety-net of well-known noise directories
  4. Optionally filters by config.yaml watched_dirs / file_extensions overrides

Usage:
    from mcp_server.gitignore import discover_source_files, infer_language_from_files

    files = discover_source_files(project_root)
    lang  = infer_language_from_files(files)
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

try:
    import pathspec
    _PATHSPEC_AVAILABLE = True
except ImportError:
    _PATHSPEC_AVAILABLE = False


# Directories that are ALWAYS skipped regardless of .gitignore contents.
# These are well-known noise directories that developers almost never want
# to index: build artifacts, dependency caches, IDE state, etc.
_SAFETY_NET_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    "dist",
    "build",
    "out",
    ".build",
    ".svelte-kit",
    ".parcel-cache",
    "coverage",
    ".nyc_output",
    "target",       # Rust / Maven build output
    "vendor",       # Go / PHP vendor dirs
    ".codevira",    # Our own data dir
    ".codevira.migrated",
})

# File name suffixes (extensions) that are never indexed.
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".so", ".dylib", ".dll", ".exe",
    ".o", ".a", ".lib",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".lock",    # package-lock.json, Cargo.lock, etc. — not useful for search
})

# Extension → language mapping for language inference
_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".swift": "swift",
    ".sol": "solidity",
    ".vue": "vue",
    ".svelte": "svelte",
    ".scala": "scala",
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml",
    ".clj": "clojure", ".cljs": "clojure",
    ".dart": "dart",
    ".r": "r", ".R": "r",
    ".lua": "lua",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".tf": "terraform", ".tfvars": "terraform",
    ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf",
    ".sql": "sql",
    ".prisma": "prisma",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown", ".mdx": "markdown",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
}


def load_gitignore_spec(project_root: Path) -> "pathspec.PathSpec | None":
    """Load and merge all .gitignore files from the project tree.

    Recursively finds all .gitignore files (root and nested) and builds a
    combined pathspec. Patterns in nested .gitignore files are prefixed with
    their relative directory so they apply only under that subtree.

    Returns None if pathspec is not installed (fail-open — no exclusions).
    """
    if not _PATHSPEC_AVAILABLE:
        return None

    lines: list[str] = []

    for root, dirs, files in os.walk(str(project_root)):
        # Prune safety-net dirs so we don't descend into them
        dirs[:] = [d for d in dirs if d not in _SAFETY_NET_DIRS]

        if ".gitignore" in files:
            rel_dir = Path(root).relative_to(project_root)
            try:
                content = (Path(root) / ".gitignore").read_text(errors="replace")
            except OSError:
                continue

            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # Prefix nested .gitignore rules with the subdirectory
                if rel_dir != Path("."):
                    # Pathspec supports dir-prefixed patterns like "src/*.pyc"
                    prefix = str(rel_dir).replace("\\", "/")
                    if stripped.startswith("!"):
                        lines.append(f"!{prefix}/{stripped[1:]}")
                    elif stripped.startswith("/"):
                        lines.append(f"{prefix}{stripped}")
                    else:
                        lines.append(f"{prefix}/{stripped}")
                else:
                    lines.append(stripped)

    if not lines:
        return None

    return pathspec.PathSpec.from_lines("gitignore", lines)


def discover_source_files(
    project_root: Path,
    config_overrides: dict | None = None,
) -> list[Path]:
    """Walk the project tree and return all source files to index.

    Exclusion order (highest priority first):
      1. Safety-net directories (always skipped)
      2. .gitignore patterns (skipped if pathspec available)
      3. Skip extensions (_SKIP_EXTENSIONS — binaries, compiled outputs, etc.)

    If config_overrides provides watched_dirs and/or file_extensions, those
    act as an allowlist filter on top of the above exclusions (backward compat
    with projects that have explicit config.yaml settings).

    Args:
        project_root: Absolute path to the project root.
        config_overrides: Dict with optional keys:
            - watched_dirs: list[str] — restrict to these subdirs
            - file_extensions: list[str] — restrict to these extensions
            - skip_dirs: list[str] — additional dirs to skip

    Returns:
        Sorted list of absolute Path objects for all indexable source files.
    """
    spec = load_gitignore_spec(project_root)

    extra_skip_dirs: set[str] = set()
    allowed_extensions: set[str] | None = None
    allowed_dirs: list[Path] | None = None

    if config_overrides:
        if "skip_dirs" in config_overrides:
            extra_skip_dirs = set(config_overrides["skip_dirs"])
        if "file_extensions" in config_overrides:
            allowed_extensions = set(config_overrides["file_extensions"])
        if "watched_dirs" in config_overrides:
            allowed_dirs = [
                project_root / d for d in config_overrides["watched_dirs"]
                if (project_root / d).exists()
            ]

    skip_dirs_all = _SAFETY_NET_DIRS | extra_skip_dirs
    result: list[Path] = []

    for root, dirs, files in os.walk(str(project_root)):
        root_path = Path(root)

        # Prune skipped directories in-place so os.walk doesn't descend
        dirs[:] = [
            d for d in dirs
            if d not in skip_dirs_all
        ]

        # If watched_dirs override is set, skip dirs not under any allowed dir
        if allowed_dirs is not None:
            dirs[:] = [
                d for d in dirs
                if any(
                    str(root_path / d).startswith(str(a)) or a == root_path / d
                    for a in allowed_dirs
                )
            ]
            # Also check the current root itself
            if not any(
                str(root_path).startswith(str(a)) or root_path == project_root
                for a in allowed_dirs
            ):
                if root_path != project_root:
                    dirs.clear()
                    continue

        for fname in files:
            fpath = root_path / fname
            suffix = fpath.suffix.lower()

            # Skip known non-source extensions
            if suffix in _SKIP_EXTENSIONS:
                continue

            # Apply watched_dirs filter
            if allowed_dirs is not None:
                if not any(
                    str(fpath).startswith(str(a))
                    for a in allowed_dirs
                ):
                    continue

            # Apply file_extensions filter from config
            if allowed_extensions is not None and suffix not in allowed_extensions:
                continue

            # Apply .gitignore spec
            if spec is not None:
                try:
                    rel = str(fpath.relative_to(project_root)).replace("\\", "/")
                    if spec.match_file(rel):
                        continue
                except ValueError:
                    pass

            result.append(fpath)

    result.sort()
    return result


def infer_language_from_files(files: list[Path]) -> str:
    """Infer the dominant programming language from a list of source files.

    Counts extensions, maps to languages, and returns the most frequent one.
    Falls back to "unknown" if no recognizable extensions are found.
    """
    counts: Counter[str] = Counter()
    for f in files:
        lang = _EXTENSION_LANGUAGE.get(f.suffix.lower())
        if lang:
            counts[lang] += 1

    if not counts:
        return "unknown"

    return counts.most_common(1)[0][0]
