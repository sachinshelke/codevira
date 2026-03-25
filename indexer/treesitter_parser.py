"""
treesitter_parser.py — Unified tree-sitter parser using tree-sitter-language-pack.

Supports parsing AST nodes (classes, functions, methods, imports) across many languages.
Python files are NOT handled here — they use the stdlib `ast` module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_language_pack as tslp
from tree_sitter import Node

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes — unified output across all languages
# ---------------------------------------------------------------------------

@dataclass
class ParsedSymbol:
    name: str
    kind: str             # "function" | "class" | "method" | "interface" | "struct" | "enum" | "trait" | "impl"
    signature_line: str
    start_line: int
    end_line: int
    docstring: str | None = None
    is_public: bool = True
    methods: list[str] = field(default_factory=list)


@dataclass
class ParsedImport:
    module: str
    raw_line: str


@dataclass
class ParsedFile:
    file_path: str
    language: str
    symbols: list[ParsedSymbol]
    imports: list[ParsedImport]
    module_docstring: str | None = None


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

EXTENSION_MAP: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".sol": "solidity",
    ".vue": "vue",
    ".js": "javascript",
    ".jsx": "javascript",
}

def get_language(extension: str) -> str | None:
    return EXTENSION_MAP.get(extension.lower())


# ---------------------------------------------------------------------------
# Node mapping configurations
# ---------------------------------------------------------------------------

_CLASS_TYPES = {
    "javascript": ["class_declaration", "class"],
    "typescript": ["class_declaration", "class", "interface_declaration"],
    "tsx": ["class_declaration", "class", "interface_declaration"],
    "go": ["type_declaration"],
    "rust": ["struct_item", "enum_item", "impl_item", "trait_item"],
    "java": ["class_declaration", "interface_declaration", "enum_declaration"],
    "c": ["struct_specifier", "type_definition"],
    "cpp": ["class_specifier", "struct_specifier"],
    "csharp": ["class_declaration", "interface_declaration", "enum_declaration", "struct_declaration"],
    "ruby": ["class", "module"],
    "kotlin": ["class_declaration", "object_declaration"],
    "swift": ["class_declaration", "struct_declaration", "protocol_declaration"],
    "php": ["class_declaration", "interface_declaration"],
    "solidity": ["contract_declaration", "interface_declaration", "library_declaration"],
}

_FUNCTION_TYPES = {
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "tsx": ["function_declaration", "method_definition", "arrow_function"],
    "go": ["function_declaration", "method_declaration"],
    "rust": ["function_item"],
    "java": ["method_declaration", "constructor_declaration"],
    "c": ["function_definition"],
    "cpp": ["function_definition"],
    "csharp": ["method_declaration", "constructor_declaration"],
    "ruby": ["method", "singleton_method"],
    "kotlin": ["function_declaration"],
    "swift": ["function_declaration"],
    "php": ["function_definition", "method_declaration"],
    "solidity": ["function_definition", "constructor_definition"],
}

_IMPORT_TYPES = {
    "javascript": ["import_statement"],
    "typescript": ["import_statement"],
    "tsx": ["import_statement"],
    "go": ["import_declaration", "import_spec"],
    "rust": ["use_declaration"],
    "java": ["import_declaration"],
    "c": ["preproc_include"],
    "cpp": ["preproc_include"],
    "csharp": ["using_directive"],
    "ruby": ["call"], 
    "kotlin": ["import_header"],
    "swift": ["import_declaration"],
    "php": ["namespace_use_declaration"],
    "solidity": ["import_directive"],
}

# ---------------------------------------------------------------------------
# Parsing core
# ---------------------------------------------------------------------------

def parse_file(file_path: str, language: str | None = None) -> ParsedFile:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if language is None:
        language = get_language(path.suffix)
        if language is None:
            raise ValueError(f"Cannot infer language for extension: {path.suffix}")

    try:
        parser = tslp.get_parser(language)
    except Exception as e:
        raise ValueError(f"Failed to load parser for {language}: {e}")

    source_bytes = path.read_bytes()
    source_text = source_bytes.decode("utf-8", errors="replace")
    source_lines = source_text.splitlines()

    tree = parser.parse(source_bytes)
    root = tree.root_node

    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    seen: set[str] = set()

    class_types = set(_CLASS_TYPES.get(language, []))
    func_types = set(_FUNCTION_TYPES.get(language, []))
    import_types = set(_IMPORT_TYPES.get(language, []))

    def walk(node: Node, parent_kind: str | None = None, is_exported: bool = False):
        if not node:
            return
            
        node_type = node.type
        current_exported = is_exported or "export" in node_type
        
        # 1. Imports
        if node_type in import_types:
            raw = _node_text(node, source_bytes)
            import re
            matches = re.findall(r'["\']([^"\']+)["\']', raw)
            for m in matches:
                imports.append(ParsedImport(module=m, raw_line=raw.strip()))
        
        # 2. Classes / Types
        elif node_type in class_types:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source_bytes) if name_node else None
            
            if not name:
                for child in node.children:
                    if "identifier" in child.type or child.type == "type_identifier":
                        name = _node_text(child, source_bytes)
                        break

            if name and name not in seen:
                seen.add(name)
                methods = []
                body = node.child_by_field_name("body") or node.child_by_field_name("declaration_list") or node
                for child in body.children:
                    if child.type in func_types:
                        m_name_node = child.child_by_field_name("name")
                        if m_name_node:
                            methods.append(_node_text(m_name_node, source_bytes))

                kind = "class"
                if "interface" in node_type: kind = "interface"
                elif "struct" in node_type or "type" in node_type: kind = "struct"
                elif "enum" in node_type: kind = "enum"
                elif "trait" in node_type: kind = "trait"
                elif "impl" in node_type: kind = "impl"

                if language == "go" and node_type == "type_declaration":
                    for c in node.children:
                        if c.type == "type_identifier":
                            name = _node_text(c, source_bytes)
                            break
                            
                if language == "rust" and "impl" in node_type:
                    for c in node.children:
                        if c.type == "type_identifier":
                            name = _node_text(c, source_bytes)
                            break
                            
                public = current_exported or _is_public(name, node, language)

                symbols.append(ParsedSymbol(
                    name=name,
                    kind=kind,
                    signature_line=_first_line_text(node, source_lines),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    docstring=_get_preceding_comment(node, source_lines),
                    is_public=public,
                    methods=methods
                ))
            
            for child in node.children:
                walk(child, "class", current_exported)
                
        # 3. Functions / Methods
        elif node_type in func_types:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source_bytes) if name_node else None
            if name and name not in seen:
                seen.add(name)
                kind = "method" if parent_kind == "class" or "method" in node_type else "function"
                
                if language == "rust" and node.parent and node.parent.type == "declaration_list":
                    pass 
                else:
                    public = current_exported or _is_public(name, node, language)
                    symbols.append(ParsedSymbol(
                        name=name,
                        kind=kind,
                        signature_line=_first_line_text(node, source_lines),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        docstring=_get_preceding_comment(node, source_lines),
                        is_public=public
                    ))
            return
            
        else:
            for child in node.children:
                walk(child, parent_kind, current_exported)

    walk(root)
    mod_doc = _extract_module_docstring(root, source_lines)

    return ParsedFile(
        file_path=file_path,
        language=language,
        symbols=symbols,
        imports=imports,
        module_docstring=mod_doc,
    )

def get_symbol_source(file_path: str, symbol_name: str, language: str | None = None) -> dict:
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

def _first_line_text(node: Node, source_lines: list[str]) -> str:
    start_row = node.start_point[0]
    if start_row < len(source_lines):
        line = source_lines[start_row].strip()
        if len(line) > 120:
            return line[:117] + "..."
        return line
    return ""

def _get_preceding_comment(node: Node, source_lines: list[str]) -> str | None:
    comments = []
    current_line = node.start_point[0] - 1
    
    while current_line >= 0:
        line = source_lines[current_line].strip()
        if line.startswith(("//", "#", "*", "/*", "*/", "///")):
            comments.append(line)
            current_line -= 1
        elif not line:
            current_line -= 1
        else:
            break
            
    if comments:
        comments.reverse()
        return "\n".join(comments)
    return None

def _extract_module_docstring(root: Node, source_lines: list[str]) -> str | None:
    if not root.children: return None
    first = root.children[0]
    for i in range(min(5, len(root.children))):
        n = root.children[i]
        if n.type == "comment" or n.type == "line_comment" or n.type == "block_comment":
            doc = _node_text(n, "\n".join(source_lines).encode('utf-8'))
            return doc.strip()
    return None

def _is_public(name: str, node: Node, language: str) -> bool:
    if language == "go":
        return name[0].isupper()
    if language in ("typescript", "tsx", "javascript", "jsx"):
        if name.startswith("_"): return False
        if node.parent and "export" in node.parent.type:
            return True
        return False
    if language == "rust":
        try:
            return "pub " in _node_text(node, b"pub ")
        except:
            pass
        return False
        
    return not name.startswith("_")

