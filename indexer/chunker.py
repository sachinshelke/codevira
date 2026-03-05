"""
AST-based Python source chunker for codebase indexing.
Splits source files into function/class/module chunks for semantic search.

Language support: Python only (uses ast module).
For TypeScript/Go/Rust, a regex-based fallback or tree-sitter integration is needed.
"""
import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _load_config() -> dict:
    """Load .agents/config.yaml if present."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


_config = _load_config()
_project_cfg = _config.get("project", {})

SKIP_DIRS = {"__pycache__", ".venv", "venv", ".git", "node_modules", "migrations"}
SKIP_FILES = {"__init__.py"}
TARGET_DIRS: set[str] = set(_project_cfg.get("watched_dirs", ["src"]))


@dataclass
class CodeChunk:
    file_path: str        # relative to project root
    chunk_type: str       # "function" | "class" | "module"
    name: str             # function/class name or filename for module chunks
    source_text: str      # the actual source code
    start_line: int
    end_line: int
    docstring: str        # first docstring if present, else ""
    layer: str            # inferred from file path


def _infer_layer(file_path: str) -> str:
    parts = Path(file_path).parts
    for i, part in enumerate(parts):
        if part in {"generator", "assembler", "indexer", "scanner", "drift", "graph", "context"}:
            return part
        if part in {"api", "routes"}:
            return "api"
        if part in {"core", "datastore", "schemas"}:
            return part
        if part in {"contexts", "application", "providers", "control", "services", "handlers"}:
            return part
    return "unknown"


def _get_docstring(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""
    except Exception:
        return ""


def _extract_source_lines(source_lines: list[str], start: int, end: int) -> str:
    return "".join(source_lines[start - 1:end])


def extract_imports(file_path: str, project_root: str) -> list[str]:
    """
    Parse a Python file's import statements and return relative paths of
    project-local imports only (skips stdlib and third-party packages).

    Returns list of relative file paths (e.g. 'src/services/provider.py').
    Paths that cannot be resolved to an existing file are omitted.
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
    project_packages = set(TARGET_DIRS)

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
            if node.module:
                if node.level and node.level > 0:
                    file_rel = os.path.relpath(file_path, project_root)
                    file_parts = Path(file_rel).parts
                    base_parts = list(file_parts[:-node.level]) if node.level < len(file_parts) else []
                    module_parts = base_parts + node.module.split(".")
                    abs_module = ".".join(module_parts)
                else:
                    abs_module = node.module
                path = _module_to_path(abs_module)
                if path and path not in results:
                    results.append(path)

    return results


def chunk_file(file_path: str, project_root: str) -> list[CodeChunk]:
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
        chunks.append(CodeChunk(
            file_path=rel_path,
            chunk_type="module",
            name=Path(file_path).stem,
            source_text=module_doc,
            start_line=1,
            end_line=1,
            docstring=module_doc,
            layer=layer,
        ))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            end_line = getattr(node, "end_lineno", node.lineno)
            source_text = _extract_source_lines(source_lines, node.lineno, end_line)
            if end_line - node.lineno < 3:
                continue
            chunks.append(CodeChunk(
                file_path=rel_path,
                chunk_type="function",
                name=node.name,
                source_text=source_text,
                start_line=node.lineno,
                end_line=end_line,
                docstring=_get_docstring(node),
                layer=layer,
            ))

        elif isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno)
            sig_end = min(node.lineno + 15, end_line)
            source_text = _extract_source_lines(source_lines, node.lineno, sig_end)
            chunks.append(CodeChunk(
                file_path=rel_path,
                chunk_type="class",
                name=node.name,
                source_text=source_text,
                start_line=node.lineno,
                end_line=end_line,
                docstring=_get_docstring(node),
                layer=layer,
            ))

    return chunks


def iter_source_files(project_root: str) -> Iterator[str]:
    """Yield all Python files in TARGET_DIRS, skipping SKIP_DIRS."""
    for target_dir in TARGET_DIRS:
        target_path = os.path.join(project_root, target_dir)
        if not os.path.exists(target_path):
            continue
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname.endswith(".py") and fname not in SKIP_FILES:
                    yield os.path.join(root, fname)


def chunk_project(project_root: str) -> list[CodeChunk]:
    """Chunk all Python files in the project. Returns flat list of all chunks."""
    all_chunks: list[CodeChunk] = []
    for file_path in iter_source_files(project_root):
        all_chunks.extend(chunk_file(file_path, project_root))
    return all_chunks
