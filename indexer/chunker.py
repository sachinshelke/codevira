"""
Multi-language source chunker for codebase indexing.
Splits source files into function/class/module chunks for semantic search.

Language support:
  - Python: stdlib ast module (full support)
  - TypeScript, Go, Rust: tree-sitter grammars via treesitter_parser
"""

from __future__ import annotations

import ast
import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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


@dataclass
class CodeChunk:
    file_path: str  # relative to project root
    chunk_type: str  # "function" | "class" | "module"
    name: str  # function/class name or filename for module chunks
    source_text: str  # the actual source code
    start_line: int
    end_line: int
    docstring: str  # first docstring if present, else ""
    layer: str  # inferred from file path


def _infer_layer(file_path: str) -> str:
    parts = Path(file_path).parts
    for i, part in enumerate(parts):
        if part in {
            "generator",
            "assembler",
            "indexer",
            "scanner",
            "drift",
            "graph",
            "context",
        }:
            return part
        if part in {"api", "routes"}:
            return "api"
        if part in {"core", "datastore", "schemas"}:
            return part
        if part in {
            "contexts",
            "application",
            "providers",
            "control",
            "services",
            "handlers",
        }:
            return part
    return "unknown"


def _get_docstring(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""
    except Exception:
        return ""


def _extract_source_lines(source_lines: list[str], start: int, end: int) -> str:
    return "".join(source_lines[start - 1 : end])


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


def _resolve_ts_import(
    raw_module: str, file_dir: Path, project_root: Path
) -> str | None:
    """
    Try to resolve a tree-sitter import string to a relative file path.
    Handles TypeScript/JS relative imports, Go package imports, and Rust use paths.
    """
    # TypeScript/JS: relative imports like './foo' or '../bar'
    if raw_module.startswith("."):
        # Resolve relative to the importing file's directory
        candidates = [
            file_dir / f"{raw_module}.ts",
            file_dir / f"{raw_module}.tsx",
            file_dir / f"{raw_module}.js",
            file_dir / f"{raw_module}.jsx",
            file_dir / raw_module / "index.ts",
            file_dir / raw_module / "index.tsx",
            file_dir / raw_module / "index.js",
        ]
        for c in candidates:
            resolved = c.resolve()
            if resolved.exists():
                try:
                    return str(resolved.relative_to(project_root))
                except ValueError:
                    continue
        return None

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
    target_dirs, _ = _get_project_config()
    project_packages = set(target_dirs)

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


def chunk_file(file_path: str, project_root: str) -> list[CodeChunk]:
    """
    Parse a source file and return all meaningful code chunks.
    Dispatches by extension: tree-sitter, Python AST, markdown, or generic text.

    2026-05-17 Bug E fix: previously, any file that wasn't Python or in the
    tree-sitter extension map silently produced [] because `_chunk_file_python`
    can't parse non-Python text. Docs-only repos hit this and went silent.
    Now markdown gets heading-based chunks; generic text gets paragraph chunks.
    """
    ext = Path(file_path).suffix.lower()

    # Tree-sitter supported (TS/Go/Rust/Java/etc.)
    if ext in _TS_SUPPORTED_EXTENSIONS:
        return _chunk_file_treesitter(file_path, project_root)

    # Markdown / prose
    if ext in _MARKDOWN_EXTENSIONS:
        return _chunk_file_markdown(file_path, project_root)

    # Generic text (JSON, YAML, INI, plain text) — paragraph chunks
    if ext in _GENERIC_TEXT_EXTENSIONS:
        return _chunk_file_text(file_path, project_root)

    # Python files: existing ast-based chunking (default fallback for unknown extensions
    # remains the Python parser — preserves prior behavior for .py-adjacent files like .pyi).
    return _chunk_file_python(file_path, project_root)


def _chunk_file_markdown(file_path: str, project_root: str) -> list[CodeChunk]:
    """Split a markdown file into chunks by H1/H2/H3 headings.

    Each section from one heading to the next becomes one chunk. Files with
    no headings get one chunk for the whole document.

    P4 (defensive parsing): any read error → returns []. Empty file → [].
    P9 (graceful): even a malformed-markdown file still yields the
                   whole-file fallback chunk so the indexer never silently
                   loses prose content.
    """
    rel_path = os.path.relpath(file_path, project_root)
    layer = _infer_layer(rel_path)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    if not text.strip():
        return []

    chunks: list[CodeChunk] = []
    lines = text.splitlines(keepends=True)
    sections: list[tuple[str, int, int]] = []  # (heading, start_line, end_line)
    current_heading = Path(file_path).stem
    current_start = 1
    for i, line in enumerate(lines, start=1):
        # Match H1/H2/H3 headings (#, ##, ###). Conservative — avoids matching
        # lines that look like headings but aren't (no #-only matches inside code blocks).
        stripped = line.lstrip()
        if (
            stripped.startswith("# ")
            or stripped.startswith("## ")
            or stripped.startswith("### ")
        ):
            if i > current_start:
                sections.append((current_heading, current_start, i - 1))
            current_heading = stripped.lstrip("#").strip()[:80] or current_heading
            current_start = i
    # Final section.
    if current_start <= len(lines):
        sections.append((current_heading, current_start, len(lines)))

    # If we found no sections at all, emit the whole file as one chunk.
    if not sections:
        sections = [(Path(file_path).stem, 1, len(lines))]

    for heading, start, end in sections:
        section_text = "".join(lines[start - 1 : end])
        if not section_text.strip():
            continue
        chunks.append(
            CodeChunk(
                file_path=rel_path,
                chunk_type="markdown_section",
                name=heading,
                source_text=section_text[:2000],  # cap so embeddings stay tractable
                start_line=start,
                end_line=end,
                docstring="",
                layer=layer,
            )
        )
    return chunks


def _chunk_file_text(file_path: str, project_root: str) -> list[CodeChunk]:
    """Split a generic text/config file into paragraph chunks.

    Heuristic: split on blank-line boundaries; cap each chunk at ~800 chars
    so a 50KB JSON schema doesn't become one mega-chunk. Returns at least one
    chunk for any non-empty file (P1: never silently produce 0 results).

    P4 (defensive): read errors → []. Empty file → [].
    """
    rel_path = os.path.relpath(file_path, project_root)
    layer = _infer_layer(rel_path)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    if not text.strip():
        return []

    # Split on blank-line boundaries; for files with no blank lines (e.g.
    # one-line JSON), fall through to the whole-file chunk.
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[CodeChunk] = []
    line_cursor = 1
    for idx, para in enumerate(paragraphs):
        line_count = para.count("\n") + 1
        # Cap chunk size — anything bigger than ~800 chars gets truncated
        # with a "(truncated)" marker. The full file is still on disk for
        # the agent to Read directly.
        body = (
            para if len(para) <= 800 else para[:800] + "\n... (truncated for embedding)"
        )
        chunks.append(
            CodeChunk(
                file_path=rel_path,
                chunk_type="text_paragraph",
                name=f"{Path(file_path).stem}#{idx + 1}",
                source_text=body,
                start_line=line_cursor,
                end_line=line_cursor + line_count - 1,
                docstring="",
                layer=layer,
            )
        )
        line_cursor += line_count + 1  # +1 for the blank-line separator
    return chunks


def _chunk_file_treesitter(file_path: str, project_root: str) -> list[CodeChunk]:
    """Chunk a non-Python file using tree-sitter symbol extraction."""
    rel_path = os.path.relpath(file_path, project_root)
    layer = _infer_layer(rel_path)

    try:
        parsed = ts_parse_file(file_path)
    except (FileNotFoundError, ValueError):
        return []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source_lines = f.read().splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return []

    chunks: list[CodeChunk] = []

    # Module-level docstring chunk
    if parsed.module_docstring:
        chunks.append(
            CodeChunk(
                file_path=rel_path,
                chunk_type="module",
                name=Path(file_path).stem,
                source_text=parsed.module_docstring,
                start_line=1,
                end_line=1,
                docstring=parsed.module_docstring,
                layer=layer,
            )
        )

    for sym in parsed.symbols:
        # Skip very short symbols (< 3 lines) like Python chunker does
        if sym.end_line - sym.start_line < 3:
            continue

        source_text = _extract_source_lines(source_lines, sym.start_line, sym.end_line)

        # For classes/structs/impl, limit source to first 15 lines (like Python chunker)
        chunk_type = sym.kind
        if chunk_type in ("class", "struct", "impl", "interface", "trait", "enum"):
            sig_end = min(sym.start_line + 15, sym.end_line)
            source_text = _extract_source_lines(source_lines, sym.start_line, sig_end)

        chunks.append(
            CodeChunk(
                file_path=rel_path,
                chunk_type=chunk_type,
                name=sym.name,
                source_text=source_text,
                start_line=sym.start_line,
                end_line=sym.end_line,
                docstring=sym.docstring or "",
                layer=layer,
            )
        )

    return chunks


def _chunk_file_python(file_path: str, project_root: str) -> list[CodeChunk]:
    """Parse a Python file and return all meaningful code chunks."""
    rel_path = os.path.relpath(file_path, project_root)
    layer = _infer_layer(rel_path)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
            source_lines = source.splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    chunks: list[CodeChunk] = []

    # Module-level docstring chunk
    module_doc = _get_docstring(tree)
    if module_doc:
        chunks.append(
            CodeChunk(
                file_path=rel_path,
                chunk_type="module",
                name=Path(file_path).stem,
                source_text=module_doc,
                start_line=1,
                end_line=1,
                docstring=module_doc,
                layer=layer,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            end_line = getattr(node, "end_lineno", node.lineno)
            source_text = _extract_source_lines(source_lines, node.lineno, end_line)
            if end_line - node.lineno < 3:
                continue
            chunks.append(
                CodeChunk(
                    file_path=rel_path,
                    chunk_type="function",
                    name=node.name,
                    source_text=source_text,
                    start_line=node.lineno,
                    end_line=end_line,
                    docstring=_get_docstring(node),
                    layer=layer,
                )
            )

        elif isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno)
            sig_end = min(node.lineno + 15, end_line)
            source_text = _extract_source_lines(source_lines, node.lineno, sig_end)
            chunks.append(
                CodeChunk(
                    file_path=rel_path,
                    chunk_type="class",
                    name=node.name,
                    source_text=source_text,
                    start_line=node.lineno,
                    end_line=end_line,
                    docstring=_get_docstring(node),
                    layer=layer,
                )
            )

    return chunks


def iter_source_files(project_root: str) -> Iterator[str]:
    """Yield source files in TARGET_DIRS matching configured file_extensions."""
    target_dirs, file_extensions = _get_project_config()
    extensions = file_extensions
    seen_files = set()

    for target_dir in target_dirs:
        target_path = os.path.join(project_root, target_dir)
        if not os.path.exists(target_path):
            continue

        for root, dirs, files in os.walk(target_path):
            # Prune skipped dirs
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            for fname in files:
                if fname.endswith(extensions) and fname not in SKIP_FILES:
                    full_path = os.path.abspath(os.path.join(root, fname))
                    if full_path not in seen_files:
                        seen_files.add(full_path)
                        yield full_path


def chunk_project(project_root: str) -> list[CodeChunk]:
    """Chunk all source files in the project. Returns flat list of all chunks."""
    all_chunks: list[CodeChunk] = []
    for file_path in iter_source_files(project_root):
        all_chunks.extend(chunk_file(file_path, project_root))
    return all_chunks
