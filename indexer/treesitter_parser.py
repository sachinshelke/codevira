"""
treesitter_parser.py — Unified tree-sitter parser for TypeScript, Go, and Rust.

Provides the same extraction API that Python's `ast` module gives us,
but powered by tree-sitter grammars. Python files are NOT handled here —
they continue to use the stdlib `ast` module in chunker.py and code_reader.py.

Language support: TypeScript (.ts, .tsx), Go (.go), Rust (.rs)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Language, Parser, Node, Query, QueryCursor


# ---------------------------------------------------------------------------
# Language registry — lazy-loaded singletons
# ---------------------------------------------------------------------------

_LANGUAGES: dict[str, Language] = {}
_PARSERS: dict[str, Parser] = {}

# Extension → language key mapping
EXTENSION_MAP: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}

# Language key → Python import module name
_LANGUAGE_MODULES: dict[str, str] = {
    "typescript": "tree_sitter_typescript",
    "tsx": "tree_sitter_typescript",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
}


def get_language(extension: str) -> str | None:
    """Map a file extension to a tree-sitter language key. Returns None if unsupported."""
    return EXTENSION_MAP.get(extension)


def _load_language(lang_key: str) -> Language:
    """Lazy-load and cache a tree-sitter Language object."""
    if lang_key in _LANGUAGES:
        return _LANGUAGES[lang_key]

    module_name = _LANGUAGE_MODULES.get(lang_key)
    if not module_name:
        raise ValueError(f"Unsupported language: {lang_key}")

    import importlib
    mod = importlib.import_module(module_name)

    # tree-sitter-typescript exposes language_typescript() and language_tsx()
    if lang_key == "typescript":
        lang = Language(mod.language_typescript())
    elif lang_key == "tsx":
        lang = Language(mod.language_tsx())
    else:
        lang = Language(mod.language())

    _LANGUAGES[lang_key] = lang
    return lang


def _get_parser(lang_key: str) -> Parser:
    """Get a cached Parser for the given language."""
    if lang_key not in _PARSERS:
        _PARSERS[lang_key] = Parser(_load_language(lang_key))
    return _PARSERS[lang_key]


# ---------------------------------------------------------------------------
# Data classes — unified output across all languages
# ---------------------------------------------------------------------------

@dataclass
class ParsedSymbol:
    name: str
    kind: str             # "function" | "class" | "method" | "interface" | "struct" | "enum" | "trait" | "impl"
    signature_line: str   # the def/func/fn line
    start_line: int       # 1-indexed
    end_line: int         # 1-indexed
    docstring: str | None = None
    is_public: bool = True
    methods: list[str] = field(default_factory=list)


@dataclass
class ParsedImport:
    module: str           # the imported path/module
    raw_line: str         # original import statement


@dataclass
class ParsedFile:
    language: str
    file_path: str
    module_docstring: str | None
    symbols: list[ParsedSymbol]
    imports: list[ParsedImport]


# ---------------------------------------------------------------------------
# Tree-sitter query definitions per language
# ---------------------------------------------------------------------------

# TypeScript / TSX queries
_TS_QUERIES = """
(function_declaration
  name: (identifier) @func.name) @func.def

(class_declaration
  name: (type_identifier) @class.name) @class.def

(interface_declaration
  name: (type_identifier) @iface.name) @iface.def

(method_definition
  name: (property_identifier) @method.name) @method.def

(export_statement
  declaration: (function_declaration
    name: (identifier) @export_func.name)) @export_func.def

(export_statement
  declaration: (class_declaration
    name: (type_identifier) @export_class.name)) @export_class.def

(import_statement) @import.stmt
"""

# Go queries
_GO_QUERIES = """
(function_declaration
  name: (identifier) @func.name) @func.def

(method_declaration
  name: (field_identifier) @method.name) @method.def

(type_declaration
  (type_spec
    name: (type_identifier) @type.name
    type: (struct_type))) @struct.def

(type_declaration
  (type_spec
    name: (type_identifier) @iface_type.name
    type: (interface_type))) @iface.def

(import_declaration) @import.stmt
"""

# Rust queries
_RS_QUERIES = """
(function_item
  name: (identifier) @func.name) @func.def

(struct_item
  name: (type_identifier) @struct.name) @struct.def

(enum_item
  name: (type_identifier) @enum.name) @enum.def

(trait_item
  name: (type_identifier) @trait.name) @trait.def

(impl_item
  type: (type_identifier) @impl.name) @impl.def

(use_declaration) @import.stmt
"""


# ---------------------------------------------------------------------------
# Comment / docstring extraction helpers
# ---------------------------------------------------------------------------

def _get_preceding_comment(node: Node, source_lines: list[str]) -> str | None:
    """
    Extract comment block immediately preceding a node.
    Handles // line comments, /** JSDoc */, /// rust doc comments, and Go // comments.
    Requires the comment to be directly adjacent (no blank lines between comment and node).
    """
    # tree-sitter start_point is (row, col), 0-indexed
    start_row = node.start_point[0]
    if start_row == 0:
        return None

    # Check if the line immediately before the node is a comment or blank
    row = start_row - 1
    first_line = source_lines[row].strip()

    # If the line directly above is blank, there's no adjacent docstring
    if not first_line:
        return None

    doc_lines: list[str] = []

    while row >= 0:
        line = source_lines[row].strip()

        # Blank line breaks adjacency
        if not line:
            break

        # JSDoc / block comment end
        if line.endswith("*/"):
            # Single-line block comment: /** ... */ or /* ... */
            if line.startswith("/*") or line.startswith("/**"):
                cleaned = line.lstrip("/*").rstrip("*/").lstrip("* ").strip()
                return cleaned if cleaned else None

            # Multi-line block comment: walk up to find the opening
            block_lines = [line]
            row -= 1
            while row >= 0:
                bl = source_lines[row].strip()
                block_lines.insert(0, bl)
                if bl.startswith("/*") or bl.startswith("/**"):
                    break
                row -= 1
            # Clean up the block
            cleaned_lines = []
            for bl in block_lines:
                bl = bl.lstrip("/*").rstrip("*/").strip()
                bl = bl.lstrip("* ").strip()
                if bl:
                    cleaned_lines.append(bl)
            return "\n".join(cleaned_lines) if cleaned_lines else None

        # Rust /// doc comment or Go/TS // comment
        if line.startswith("///"):
            doc_lines.insert(0, line.lstrip("/").strip())
            row -= 1
            continue
        elif line.startswith("//"):
            doc_lines.insert(0, line.lstrip("/").strip())
            row -= 1
            continue

        # Not a comment line — stop
        break

    return "\n".join(doc_lines) if doc_lines else None


def _node_text(node: Node, source_bytes: bytes) -> str:
    """Extract the text content of a tree-sitter node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_line_text(node: Node, source_lines: list[str]) -> str:
    """Get the first source line of a node (the signature line)."""
    return source_lines[node.start_point[0]].strip()


# ---------------------------------------------------------------------------
# Visibility helpers
# ---------------------------------------------------------------------------

def _is_public_ts(name: str, node: Node) -> bool:
    """In TypeScript, symbols are public unless prefixed with _ or marked private."""
    if name.startswith("_"):
        return False
    # Check if parent is an export_statement
    parent = node.parent
    if parent and parent.type == "export_statement":
        return True
    return True  # TS symbols without export are still module-level


def _is_public_go(name: str) -> bool:
    """In Go, exported symbols start with an uppercase letter."""
    return len(name) > 0 and name[0].isupper()


def _is_public_rust(name: str, node: Node) -> bool:
    """In Rust, public items have a `pub` visibility modifier."""
    if name.startswith("_"):
        return False
    # Check for `pub` keyword in children
    for child in node.children:
        if child.type == "visibility_modifier":
            return True
    return False


# ---------------------------------------------------------------------------
# Per-language symbol extractors
# ---------------------------------------------------------------------------

def _extract_methods_from_class(node: Node, source_bytes: bytes) -> list[str]:
    """Extract public method names from a class/struct/impl body."""
    methods = []
    for child in node.children:
        if child.type == "class_body":
            for member in child.children:
                if member.type == "method_definition":
                    name_node = member.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node, source_bytes)
                        if not name.startswith("_"):
                            methods.append(name)
        elif child.type == "declaration_list":
            # Rust impl block
            for member in child.children:
                if member.type == "function_item":
                    name_node = member.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node, source_bytes)
                        if not name.startswith("_"):
                            methods.append(name)
    return methods


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_file(file_path: str, language: str | None = None) -> ParsedFile:
    """
    Parse a source file using tree-sitter and extract symbols + imports.

    Args:
        file_path: Absolute or relative path to the source file
        language: Language key (e.g. "typescript", "go", "rust").
                  If None, inferred from file extension.

    Returns:
        ParsedFile with symbols, imports, and module_docstring.

    Raises:
        ValueError: if language is unsupported or cannot be inferred
        FileNotFoundError: if file does not exist
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if language is None:
        language = get_language(path.suffix)
        if language is None:
            raise ValueError(f"Cannot infer language for extension: {path.suffix}")

    source_bytes = path.read_bytes()
    source_text = source_bytes.decode("utf-8", errors="replace")
    source_lines = source_text.splitlines()

    parser = _get_parser(language)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    # Select queries based on language
    if language in ("typescript", "tsx"):
        query_text = _TS_QUERIES
    elif language == "go":
        query_text = _GO_QUERIES
    elif language == "rust":
        query_text = _RS_QUERIES
    else:
        raise ValueError(f"No queries defined for language: {language}")

    lang_obj = _load_language(language)
    query = Query(lang_obj, query_text)
    cursor = QueryCursor(query)
    captures = cursor.captures(root)

    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    seen_names: set[str] = set()

    # Build capture-name → node list map
    # captures is a dict[str, list[Node]]
    capture_map: dict[str, list[Node]] = captures if isinstance(captures, dict) else {}

    # Process based on language
    if language in ("typescript", "tsx"):
        _process_ts_captures(capture_map, source_bytes, source_lines, symbols, imports, seen_names)
    elif language == "go":
        _process_go_captures(capture_map, source_bytes, source_lines, symbols, imports, seen_names)
    elif language == "rust":
        _process_rust_captures(capture_map, source_bytes, source_lines, symbols, imports, seen_names)

    # Extract module-level docstring (first comment block at top of file)
    module_docstring = _extract_module_docstring(root, source_lines)

    return ParsedFile(
        language=language,
        file_path=file_path,
        module_docstring=module_docstring,
        symbols=symbols,
        imports=imports,
    )


def _extract_module_docstring(root: Node, source_lines: list[str]) -> str | None:
    """Extract the first comment block at the top of the file as a module docstring."""
    doc_lines: list[str] = []
    for i, line in enumerate(source_lines):
        stripped = line.strip()
        if not stripped:
            if doc_lines:
                break
            continue
        if stripped.startswith("//") or stripped.startswith("///"):
            doc_lines.append(stripped.lstrip("/").strip())
        elif stripped.startswith("/*") or stripped.startswith("/**"):
            # Collect block comment
            block = []
            for j in range(i, len(source_lines)):
                bl = source_lines[j].strip()
                block.append(bl)
                if bl.endswith("*/") and j > i:
                    break
                if bl.endswith("*/") and j == i and len(bl) > 2:
                    break
            for bl in block:
                cleaned = bl.lstrip("/*").rstrip("*/").lstrip("* ").strip()
                if cleaned:
                    doc_lines.append(cleaned)
            break
        else:
            break
    return "\n".join(doc_lines) if doc_lines else None


# ---------------------------------------------------------------------------
# Language-specific capture processors
# ---------------------------------------------------------------------------

def _process_ts_captures(
    captures: dict[str, list[Node]],
    source_bytes: bytes,
    source_lines: list[str],
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    seen: set[str],
) -> None:
    """Process TypeScript/TSX tree-sitter captures."""

    # Functions
    for node in captures.get("func.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="function",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_ts(name, node),
        ))

    # Exported functions (may duplicate — dedup via seen set)
    for node in captures.get("export_func.def", []):
        # The actual func is inside the export_statement
        func_node = None
        for child in node.children:
            if child.type == "function_declaration":
                func_node = child
                break
        if not func_node:
            continue
        name_node = func_node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="function",
            signature_line=_first_line_text(func_node, source_lines),
            start_line=func_node.start_point[0] + 1,
            end_line=func_node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=True,
        ))

    # Classes
    for node in captures.get("class.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        methods = _extract_methods_from_class(node, source_bytes)
        symbols.append(ParsedSymbol(
            name=name,
            kind="class",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_ts(name, node),
            methods=methods,
        ))

    # Exported classes
    for node in captures.get("export_class.def", []):
        class_node = None
        for child in node.children:
            if child.type == "class_declaration":
                class_node = child
                break
        if not class_node:
            continue
        name_node = class_node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        methods = _extract_methods_from_class(class_node, source_bytes)
        symbols.append(ParsedSymbol(
            name=name,
            kind="class",
            signature_line=_first_line_text(class_node, source_lines),
            start_line=class_node.start_point[0] + 1,
            end_line=class_node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=True,
            methods=methods,
        ))

    # Interfaces
    for node in captures.get("iface.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="interface",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_ts(name, node),
        ))

    # Imports
    for node in captures.get("import.stmt", []):
        raw = _node_text(node, source_bytes)
        module = _extract_ts_import_module(raw)
        imports.append(ParsedImport(module=module, raw_line=raw.strip()))


def _extract_ts_import_module(raw: str) -> str:
    """Extract the module path from a TS import statement."""
    import re
    match = re.search(r"""from\s+['"]([^'"]+)['"]""", raw)
    if match:
        return match.group(1)
    match = re.search(r"""import\s+['"]([^'"]+)['"]""", raw)
    if match:
        return match.group(1)
    return raw.strip()


def _process_go_captures(
    captures: dict[str, list[Node]],
    source_bytes: bytes,
    source_lines: list[str],
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    seen: set[str],
) -> None:
    """Process Go tree-sitter captures."""

    # Functions
    for node in captures.get("func.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="function",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_go(name),
        ))

    # Methods
    for node in captures.get("method.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="method",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_go(name),
        ))

    # Structs
    for node in captures.get("struct.def", []):
        name_nodes = captures.get("type.name", [])
        # Find the type.name that's a child of this node
        name = None
        for nn in name_nodes:
            if nn.start_byte >= node.start_byte and nn.end_byte <= node.end_byte:
                name = _node_text(nn, source_bytes)
                break
        if not name or name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="struct",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_go(name),
        ))

    # Interfaces
    for node in captures.get("iface.def", []):
        name_nodes = captures.get("iface_type.name", [])
        name = None
        for nn in name_nodes:
            if nn.start_byte >= node.start_byte and nn.end_byte <= node.end_byte:
                name = _node_text(nn, source_bytes)
                break
        if not name or name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="interface",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_go(name),
        ))

    # Imports
    for node in captures.get("import.stmt", []):
        raw = _node_text(node, source_bytes)
        for mod in _extract_go_import_modules(raw):
            imports.append(ParsedImport(module=mod, raw_line=raw.strip()))


def _extract_go_import_modules(raw: str) -> list[str]:
    """Extract all import paths from a Go import declaration."""
    import re
    return re.findall(r'"([^"]+)"', raw)


def _process_rust_captures(
    captures: dict[str, list[Node]],
    source_bytes: bytes,
    source_lines: list[str],
    symbols: list[ParsedSymbol],
    imports: list[ParsedImport],
    seen: set[str],
) -> None:
    """Process Rust tree-sitter captures."""

    # Functions (skip those inside impl blocks — they're captured as impl methods)
    for node in captures.get("func.def", []):
        # Skip if this function is inside an impl_item
        parent = node.parent
        if parent and parent.type == "declaration_list":
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="function",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_rust(name, node),
        ))

    # Structs
    for node in captures.get("struct.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="struct",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_rust(name, node),
        ))

    # Enums
    for node in captures.get("enum.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="enum",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_rust(name, node),
        ))

    # Traits
    for node in captures.get("trait.def", []):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _node_text(name_node, source_bytes)
        if name in seen:
            continue
        seen.add(name)
        symbols.append(ParsedSymbol(
            name=name,
            kind="trait",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_rust(name, node),
        ))

    # Impl blocks
    for node in captures.get("impl.def", []):
        name_nodes = captures.get("impl.name", [])
        name = None
        for nn in name_nodes:
            if nn.start_byte >= node.start_byte and nn.end_byte <= node.end_byte:
                name = _node_text(nn, source_bytes)
                break
        if not name or name in seen:
            continue
        seen.add(name)
        methods = _extract_methods_from_class(node, source_bytes)
        symbols.append(ParsedSymbol(
            name=name,
            kind="impl",
            signature_line=_first_line_text(node, source_lines),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_get_preceding_comment(node, source_lines),
            is_public=_is_public_rust(name, node),
            methods=methods,
        ))

    # Imports (use declarations)
    for node in captures.get("import.stmt", []):
        raw = _node_text(node, source_bytes)
        module = _extract_rust_use_path(raw)
        imports.append(ParsedImport(module=module, raw_line=raw.strip()))


def _extract_rust_use_path(raw: str) -> str:
    """Extract the path from a Rust use declaration."""
    import re
    match = re.search(r"use\s+(.+);", raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


# ---------------------------------------------------------------------------
# Convenience: get source for a specific symbol
# ---------------------------------------------------------------------------

def get_symbol_source(file_path: str, symbol_name: str, language: str | None = None) -> dict:
    """
    Extract the full source code of a named symbol from a non-Python file.

    Returns a dict matching the code_reader.get_code() output format.
    """
    path = Path(file_path)
    if not path.exists():
        return {"found": False, "error": f"File not found: {file_path}"}

    if language is None:
        language = get_language(path.suffix)
        if language is None:
            return {"found": False, "error": f"Unsupported extension: {path.suffix}"}

    parsed = parse_file(file_path, language)
    source_text = path.read_text(encoding="utf-8", errors="replace")
    source_lines = source_text.splitlines()

    for sym in parsed.symbols:
        if sym.name == symbol_name:
            lines = source_lines[sym.start_line - 1 : sym.end_line]
            return {
                "found": True,
                "file_path": file_path,
                "symbol": symbol_name,
                "kind": sym.kind,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "docstring": sym.docstring,
                "source": "\n".join(lines),
            }

    return {
        "found": False,
        "file_path": file_path,
        "symbol": symbol_name,
        "error": f"Symbol '{symbol_name}' not found in {file_path}",
        "available_symbols": [s.name for s in parsed.symbols],
    }
