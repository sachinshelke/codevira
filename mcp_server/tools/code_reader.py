"""
code_reader.py — MCP tools for reading source files

get_signature(file_path)       → skeleton: all public symbols, signatures, docstrings, line ranges
get_code(file_path, symbol)    → full source of one named function or class from disk

Always reads from disk. No index, no ChromaDB, no staleness risk.

Language support:
  - Python: stdlib ast module (full support)
  - TypeScript, Go, Rust: tree-sitter grammars via treesitter_parser
"""
import ast
from pathlib import Path

from mcp_server.paths import get_project_root
from indexer.treesitter_parser import (
    parse_file as ts_parse_file,
    get_symbol_source as ts_get_symbol_source,
    get_language as ts_get_language,
    EXTENSION_MAP as TS_EXTENSION_MAP,
)


def _resolve(file_path: str) -> Path:
    """Resolve relative file path to absolute using the project root."""
    p = Path(file_path)
    if p.is_absolute():
        return p
    return get_project_root() / p


def _is_private(name: str) -> bool:
    return name.startswith("_")


def _node_kind(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    return "function"


def _signature_line(source_lines: list[str], node: ast.AST) -> str:
    """Return the def/class line (first line of the node), stripped."""
    return source_lines[node.lineno - 1].strip()


def get_signature(file_path: str) -> dict:
    """
    Get the skeleton of a source file: all public function and class names,
    their signatures, docstrings, and line ranges.

    Supports Python, TypeScript, Go, and Rust.

    Args:
        file_path: Relative path from project root (e.g. 'src/services/generator.py')

    Returns:
        dict with module_docstring, symbols list, and file metadata
    """
    abs_path = _resolve(file_path)

    if not abs_path.exists():
        return {
            "found": False,
            "file_path": file_path,
            "error": f"File not found: {abs_path}",
        }

    ext = abs_path.suffix.lower()

    # Non-Python: dispatch to tree-sitter
    if ext in TS_EXTENSION_MAP:
        return _get_signature_treesitter(file_path, abs_path)

    # Python: existing ast-based extraction
    if ext == ".py":
        return _get_signature_python(file_path, abs_path)

    return {
        "found": False,
        "file_path": file_path,
        "error": f"Unsupported file type: {ext}. Supported: .py, .ts, .tsx, .go, .rs",
    }


def _get_signature_treesitter(file_path: str, abs_path: Path) -> dict:
    """Get file skeleton using tree-sitter for TS/Go/Rust files."""
    try:
        parsed = ts_parse_file(str(abs_path))
    except (ValueError, FileNotFoundError) as e:
        return {
            "found": False,
            "file_path": file_path,
            "error": str(e),
        }

    symbols = []
    for sym in parsed.symbols:
        if not sym.is_public:
            continue

        entry = {
            "name": sym.name,
            "kind": sym.kind,
            "signature_line": sym.signature_line,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
        }
        if sym.docstring:
            first_line = sym.docstring.strip().splitlines()[0]
            entry["docstring"] = first_line

        if sym.methods:
            entry["public_methods"] = sym.methods

        symbols.append(entry)

    return {
        "found": True,
        "file_path": file_path,
        "language": parsed.language,
        "module_docstring": parsed.module_docstring,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "hint": "Use get_code(file_path, symbol) to read the full body of any symbol listed above.",
    }


def _get_signature_python(file_path: str, abs_path: Path) -> dict:
    """Get file skeleton using Python ast module."""
    source = abs_path.read_text(encoding="utf-8")
    source_lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=str(abs_path))
    except SyntaxError as e:
        return {
            "found": False,
            "file_path": file_path,
            "error": f"Syntax error: {e}",
        }

    module_docstring = ast.get_docstring(tree) or None

    symbols = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _is_private(node.name):
            continue

        kind = _node_kind(node)
        sig_line = _signature_line(source_lines, node)
        docstring = ast.get_docstring(node) or None

        entry = {
            "name": node.name,
            "kind": kind,
            "signature_line": sig_line,
            "start_line": node.lineno,
            "end_line": node.end_lineno,
        }
        if docstring:
            first_line = docstring.strip().splitlines()[0]
            entry["docstring"] = first_line

        if kind == "class":
            methods = []
            for child in ast.walk(node):
                if child is node:
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not _is_private(child.name) and child.name != "__init__":
                        methods.append(child.name)
            if methods:
                entry["public_methods"] = methods

        symbols.append(entry)

    return {
        "found": True,
        "file_path": file_path,
        "module_docstring": module_docstring,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "hint": "Use get_code(file_path, symbol) to read the full body of any symbol listed above.",
    }


def get_code(file_path: str, symbol: str | None = None) -> dict:
    """
    Get the full source of a single function or class by name.
    Always reads from disk — always current.

    Supports Python, TypeScript, Go, and Rust.

    Args:
        file_path: Relative path from project root
        symbol: Function or class name. Omit to get module-level constants/assignments only.

    Returns:
        dict with source, start_line, end_line, kind, docstring
    """
    abs_path = _resolve(file_path)

    if not abs_path.exists():
        return {
            "found": False,
            "file_path": file_path,
            "symbol": symbol,
            "error": f"File not found: {abs_path}",
        }

    ext = abs_path.suffix.lower()

    # Non-Python: dispatch to tree-sitter
    if ext in TS_EXTENSION_MAP:
        return _get_code_treesitter(file_path, abs_path, symbol)

    # Python: existing ast-based extraction
    if ext == ".py":
        return _get_code_python(file_path, abs_path, symbol)

    return {
        "found": False,
        "file_path": file_path,
        "symbol": symbol,
        "error": f"Unsupported file type: {ext}. Supported: .py, .ts, .tsx, .go, .rs",
    }


def _get_code_treesitter(file_path: str, abs_path: Path, symbol: str | None) -> dict:
    """Get symbol source using tree-sitter for TS/Go/Rust files."""
    # symbol=None → return module-level info
    if symbol is None:
        try:
            parsed = ts_parse_file(str(abs_path))
        except (ValueError, FileNotFoundError) as e:
            return {
                "found": False,
                "file_path": file_path,
                "symbol": None,
                "error": str(e),
            }
        return {
            "found": True,
            "file_path": file_path,
            "symbol": None,
            "kind": "module_info",
            "language": parsed.language,
            "module_docstring": parsed.module_docstring,
            "imports": [imp.module for imp in parsed.imports],
        }

    # Named symbol lookup
    result = ts_get_symbol_source(str(abs_path), symbol)
    result["file_path"] = file_path
    return result


def _get_code_python(file_path: str, abs_path: Path, symbol: str | None) -> dict:
    """Get symbol source using Python ast module."""
    source = abs_path.read_text(encoding="utf-8")
    source_lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=str(abs_path))
    except SyntaxError as e:
        return {
            "found": False,
            "file_path": file_path,
            "symbol": symbol,
            "error": f"Syntax error: {e}",
        }

    # symbol=None → return module-level non-function, non-class content
    if symbol is None:
        module_docstring = ast.get_docstring(tree) or None
        assignments = []
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                lines = source_lines[node.lineno - 1 : node.end_lineno]
                assignments.append({
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "source": "\n".join(lines),
                })
        return {
            "found": True,
            "file_path": file_path,
            "symbol": None,
            "kind": "module_constants",
            "module_docstring": module_docstring,
            "assignments": assignments,
        }

    # Walk all nodes to find functions inside classes too
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name != symbol:
            continue

        kind = _node_kind(node)
        docstring = ast.get_docstring(node) or None
        lines = source_lines[node.lineno - 1 : node.end_lineno]

        return {
            "found": True,
            "file_path": file_path,
            "symbol": symbol,
            "kind": kind,
            "start_line": node.lineno,
            "end_line": node.end_lineno,
            "docstring": docstring,
            "source": "\n".join(lines),
        }

    # Symbol not found — provide available symbol names as a hint
    available = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            available.append(node.name)

    return {
        "found": False,
        "file_path": file_path,
        "symbol": symbol,
        "error": f"Symbol '{symbol}' not found in {file_path}",
        "available_symbols": sorted(set(available)),
        "hint": "Call get_signature(file_path) to see public symbols with line ranges.",
    }
