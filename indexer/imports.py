"""
Import extraction for the code graph.

Reads a source file and returns the modules it imports, resolving
TypeScript path aliases (``@/x`` -> ``src/x``) through the project's
tsconfig so the graph links to real files rather than to alias strings.

Language support:
  - Python: stdlib ``ast``
  - TypeScript / JS / Go / Rust: tree-sitter grammars via treesitter_parser

Was ``chunker.py`` until 4.0.1. It carried a full source-chunker whose only
purpose was feeding embeddings to ChromaDB; v2.2.0 removed ChromaDB,
sentence-transformers and torch, which left the chunking half calling into
nothing and reachable from nothing. Only the import extraction was ever wired
to the graph, so the file now says what it does.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
from pathlib import Path

from indexer.treesitter_parser import (
    parse_file as ts_parse_file,
    EXTENSION_MAP as TS_EXTENSION_MAP,
)


def _load_config() -> dict:
    from mcp_server.paths import get_data_dir

    config_path = get_data_dir() / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


SKIP_DIRS = {"__pycache__", ".venv", "venv", ".git", "node_modules", "migrations"}
SKIP_FILES = {"__init__.py"}

# All tree-sitter supported extensions for dispatch
_TS_SUPPORTED_EXTENSIONS = set(TS_EXTENSION_MAP.keys())


@functools.lru_cache(maxsize=None)
def _get_project_config() -> tuple[frozenset[str], tuple[str, ...]]:
    """Lazily load config.yaml and return (TARGET_DIRS, FILE_EXTENSIONS).

    Cached so subsequent calls are free. lru_cache is used so that the
    config is only loaded once per process after the data directory is known.
    """
    cfg = _load_config()
    project_cfg = cfg.get("project", cfg)
    target_dirs: frozenset[str] = frozenset(project_cfg.get("watched_dirs", ["src"]))
    file_extensions: tuple[str, ...] = tuple(
        project_cfg.get("file_extensions", [".py"])
    )
    return target_dirs, file_extensions


def extract_imports(file_path: str, project_root: str) -> list[str]:
    """
    Parse a source file's import statements and return relative paths of
    project-local imports only (skips stdlib and third-party packages).

    Dispatches to Python ast or tree-sitter based on file extension.
    Returns list of relative file paths (e.g. 'src/services/provider.py').
    Paths that cannot be resolved to an existing file are omitted.
    """
    ext = Path(file_path).suffix.lower()

    # Non-Python files: use tree-sitter import extraction
    if ext in _TS_SUPPORTED_EXTENSIONS:
        return _extract_imports_treesitter(file_path, project_root)

    # Python files: existing ast-based extraction
    return _extract_imports_python(file_path, project_root)


def _extract_imports_treesitter(file_path: str, project_root: str) -> list[str]:
    """
    Extract import paths from a non-Python file using tree-sitter.
    Resolves relative/local imports to actual project file paths where possible.
    Falls back to raw module strings for unresolvable imports.
    """
    try:
        parsed = ts_parse_file(file_path)
    except (FileNotFoundError, ValueError):
        return []

    project_root_path = Path(project_root)
    file_dir = Path(file_path).parent
    results: list[str] = []

    for imp in parsed.imports:
        raw = imp.module
        resolved = _resolve_ts_import(raw, file_dir, project_root_path)
        if resolved and resolved not in results:
            results.append(resolved)

    return results


def _strip_jsonc(text: str) -> str:
    """Strip JSONC extras (// and /* */ comments, trailing commas) so a
    tsconfig.json/jsconfig.json parses with json.loads. String-aware: comment
    markers inside string values (e.g. the `/*` in a path alias "src/*") are
    left intact — a regex stripper would corrupt those."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # keep escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":  # // line comment
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":  # /* block */
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))  # trailing comma


@functools.lru_cache(maxsize=None)
def _load_tsconfig(
    project_root_str: str,
) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Read compilerOptions.baseUrl + paths from tsconfig.json (or jsconfig.json)
    at the project root. Returns (base_url, paths) where base_url is a
    project-root-relative string ('' if unset) and paths is a hashable tuple of
    (alias_pattern, (target, ...)). Returns ('', ()) on any failure.

    `extends` chains and non-root tsconfig files are NOT followed (a known
    limitation — see D000124); this covers the common single-root case.
    """
    root = Path(project_root_str)
    for name in ("tsconfig.json", "jsconfig.json"):
        cfg_path = root / name
        if not cfg_path.is_file():
            continue
        try:
            data = json.loads(_strip_jsonc(cfg_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        opts = (data or {}).get("compilerOptions") or {}
        base_url = opts.get("baseUrl") or ""
        raw_paths = opts.get("paths") or {}
        paths: list[tuple[str, tuple[str, ...]]] = []
        if isinstance(raw_paths, dict):
            for pattern, targets in raw_paths.items():
                if isinstance(targets, list):
                    paths.append(
                        (pattern, tuple(t for t in targets if isinstance(t, str)))
                    )
        return str(base_url), tuple(paths)
    return "", ()


def _match_alias(pattern: str, raw_module: str) -> str | None:
    """If `raw_module` matches a tsconfig path `pattern`, return the text captured
    by the `*` wildcard ('' for an exact, wildcard-free match); else None."""
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        if (
            raw_module.startswith(prefix)
            and raw_module.endswith(suffix)
            and len(raw_module) >= len(prefix) + len(suffix)
        ):
            end = len(raw_module) - len(suffix) if suffix else len(raw_module)
            return raw_module[len(prefix) : end]
        return None
    return "" if raw_module == pattern else None


def _expand_alias(
    raw_module: str, base_url: str, paths: tuple[tuple[str, tuple[str, ...]], ...]
) -> list[str]:
    """Turn an aliased specifier into candidate project-root-relative base paths
    (no extension) using tsconfig baseUrl + paths."""
    out: list[str] = []
    for pattern, targets in paths:
        captured = _match_alias(pattern, raw_module)
        if captured is None:
            continue
        for tgt in targets:
            sub = tgt.replace("*", captured) if "*" in tgt else tgt
            rel = str(Path(base_url) / sub) if base_url else sub
            if rel not in out:
                out.append(rel)
    return out


@functools.lru_cache(maxsize=64)
def _resolved_root(project_root_str: str) -> Path:
    """``project_root.resolve()``, memoised.

    ``resolve()`` walks and stats every path component, and this used to run
    once per candidate probe: on a 2,000-file TS project with ~10 imports
    each that is ~20,000 identical calls for one value. There are only ever a
    handful of distinct roots in a process.
    """
    return Path(project_root_str).resolve()


def _probe_ts_file(base_no_ext: Path, project_root: Path) -> str | None:
    """Probe TS/JS extension + index-file candidates for a base path and return
    the first existing one, project-root-relative. project_root is resolved so
    relative_to() matches even when it points through a symlink (e.g. macOS
    /var -> /private/var)."""
    root = _resolved_root(str(project_root))
    candidates = [
        base_no_ext.with_name(base_no_ext.name + ".ts"),
        base_no_ext.with_name(base_no_ext.name + ".tsx"),
        base_no_ext.with_name(base_no_ext.name + ".js"),
        base_no_ext.with_name(base_no_ext.name + ".jsx"),
        base_no_ext / "index.ts",
        base_no_ext / "index.tsx",
        base_no_ext / "index.js",
    ]
    for c in candidates:
        # exists() first: a miss is the common case and costs one stat,
        # where resolve() walks every component of the path.
        if not c.exists():
            continue
        resolved = c.resolve()
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            continue
    return None


def _resolve_ts_import(
    raw_module: str, file_dir: Path, project_root: Path
) -> str | None:
    """
    Try to resolve a tree-sitter import string to a relative file path.
    Handles TypeScript/JS relative imports, Go package imports, and Rust use paths.
    """
    # TypeScript/JS: relative imports like './foo' or '../bar'
    if raw_module.startswith("."):
        hit = _probe_ts_file(file_dir / raw_module, project_root)
        if hit:
            return hit
        return None

    # Non-relative TS/JS: try tsconfig path aliases (@/foo, ~/bar) + baseUrl
    # before falling back to a literal project-root probe. Without this, aliased
    # imports resolve to nothing and their dependency edges are dropped (D000124).
    base_url, ts_paths = _load_tsconfig(str(project_root))
    for base_rel in _expand_alias(raw_module, base_url, ts_paths):
        hit = _probe_ts_file(project_root / base_rel, project_root)
        if hit:
            return hit
    if base_url:
        hit = _probe_ts_file(project_root / base_url / raw_module, project_root)
        if hit:
            return hit

    # Non-relative: try as a project-local path (e.g. 'src/utils/foo')
    # Check common extensions
    for ext in [".ts", ".tsx", ".js", ".go", ".rs"]:
        candidate = project_root / f"{raw_module}{ext}"
        if candidate.exists():
            return str(candidate.relative_to(project_root))

    # Try as directory with index file
    for index in ["index.ts", "index.tsx", "index.js", "mod.rs"]:
        candidate = project_root / raw_module / index
        if candidate.exists():
            return str(candidate.relative_to(project_root))

    # Go: package paths like 'project/internal/services'
    # Try mapping to directory with .go files
    candidate_dir = project_root / raw_module
    if candidate_dir.is_dir():
        go_files = list(candidate_dir.glob("*.go"))
        if go_files:
            return str(go_files[0].relative_to(project_root))

    return None


#: Directory names that are never a project's own import roots.
_NON_PACKAGE_DIRS = frozenset(
    {
        "node_modules",
        "venv",
        ".venv",
        "env",
        "build",
        "dist",
        "target",
        "vendor",
        "site-packages",
        "__pycache__",
        "htmlcov",
        "docs",
        "tests",
    }
)


@functools.lru_cache(maxsize=32)
def _project_packages(project_root: Path) -> frozenset[str]:
    """Top-level names an intra-project import can legitimately start with.

    The configured ``watched_dirs`` UNION the directories that actually look
    importable on disk. The union is deliberate: it can only add correct
    edges, never drop one an existing project already gets, so upgrading
    cannot shrink anybody's graph.

    Why the disk half is needed at all: ``watched_dirs`` is written by the
    legacy ``init`` scaffold into ``<data_dir>/config.yaml``, while the v2.2
    scaffold writes ``<project>/.codevira/config.yaml`` WITHOUT it — and
    creating that second file is exactly what flips ``get_data_dir()`` to the
    in-repo path, so the config that survives is the one missing the key.
    ``_get_project_config()`` then fell back to a hardcoded ``["src"]``, and
    every project not laid out as ``src/`` resolved zero imports: 1,197 nodes
    and 0 edges on codevira's own repo, which silently emptied ``get_impact``.
    """
    names: set[str] = set()
    try:
        configured, _ = _get_project_config()
        names |= {str(d).strip("/. ") for d in configured if str(d).strip("/. ")}
    except Exception:  # noqa: BLE001 — config is advisory here, disk is truth
        pass

    try:
        for entry in project_root.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name in _NON_PACKAGE_DIRS:
                continue
            # Importable if it is a package, or simply holds source files.
            if (entry / "__init__.py").exists() or any(entry.glob("*.py")):
                names.add(name)
    except (OSError, PermissionError):
        pass

    return frozenset(names)


def _extract_imports_python(file_path: str, project_root: str) -> list[str]:
    """
    Parse a Python file's import statements and return relative paths of
    project-local imports only (skips stdlib and third-party packages).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    project_root_path = Path(project_root)
    project_packages = _project_packages(project_root_path)

    results: list[str] = []

    def _module_to_path(module: str) -> str | None:
        """Convert a dotted module name to a relative file path if project-local."""
        parts = module.split(".")
        if not parts or parts[0] not in project_packages:
            return None
        candidates = [
            project_root_path / Path(*parts) / "__init__.py",
            project_root_path / Path(*parts[:-1]) / f"{parts[-1]}.py",
            project_root_path / Path(*parts).with_suffix(".py"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate.relative_to(project_root_path))
        direct = project_root_path / Path(*parts[:-1]) / f"{parts[-1]}.py"
        rel = str(direct.relative_to(project_root_path))
        if (project_root_path / Path(*parts[:-1])).exists():
            return rel
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = _module_to_path(alias.name)
                if path and path not in results:
                    results.append(path)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                file_rel = os.path.relpath(file_path, project_root)
                file_parts = Path(file_rel).parts
                base_parts = (
                    list(file_parts[: -node.level])
                    if node.level < len(file_parts)
                    else []
                )
                if node.module:
                    module_parts = base_parts + str(node.module).split(".")
                else:
                    module_parts = base_parts
                abs_module = ".".join(module_parts)
            elif node.module:
                abs_module = str(node.module)
            else:
                continue
            path = _module_to_path(abs_module)
            if path and path not in results:
                results.append(path)

    return results


# 2026-05-17 Bug E fix (P1): docs-only repos (lh-interface, README-heavy
# projects, schema-doc repos) silently produced 0 chunks because every
# non-code file fell through to the Python AST parser which returns [].
# Add explicit handling for markdown + generic text formats so the chunker
# always produces SOMETHING usable when files are present.

_MARKDOWN_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".markdown"}
_GENERIC_TEXT_EXTENSIONS = {
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".env",
    ".env.example",
    ".cfg",
    ".conf",
}
