import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from indexer.treesitter_parser import (
    parse_file as ts_parse_file,
    get_language as ts_get_language,
    EXTENSION_MAP as TS_EXTENSION_MAP,
)
from indexer.sqlite_graph import SQLiteGraph
from indexer.chunker import extract_imports

def _infer_layer(file_path: str) -> str:
    path = file_path.lower()
    if any(x in path for x in ["/api/", "/controllers/", "/routers/", "/routes/"]):
        return "api"
    if any(x in path for x in ["/models/", "/db/", "/schemas/", "/orm/"]):
        return "database"
    if any(x in path for x in ["/services/", "/core/", "/logic/", "/usecases/"]):
        return "service"
    if any(x in path for x in ["/utils/", "/helpers/", "/common/"]):
        return "utility"
    if any(x in path for x in ["/frontend/", "/ui/", "/components/", "/views/"]):
        return "frontend"
    if "test" in path:
        return "test"
    return "core"

def _get_python_docstring(file_path: str) -> str | None:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
            doc = ast.get_docstring(tree)
            if doc:
                return doc.splitlines()[0]
    except Exception:
        pass
    return None

def _get_python_public_symbols(file_path: str) -> list[str]:
    symbols = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    symbols.append(node.name)
                elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    symbols.append(node.name)
    except Exception:
        pass
    return symbols

def _get_python_symbols_detailed(file_path: str) -> list:
    """Extract Python symbols with call information for the call graph."""
    from indexer.treesitter_parser import ParsedSymbol
    symbols = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        source_lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue

                # Extract function calls within the body
                calls = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            calls.append(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            calls.append(child.func.attr)

                # Extract parameters
                params = []
                for arg in node.args.args:
                    param = {"name": arg.arg}
                    if arg.annotation:
                        try:
                            param["type"] = ast.unparse(arg.annotation)
                        except Exception:
                            pass
                    params.append(param)

                # Extract return type
                ret_type = None
                if node.returns:
                    try:
                        ret_type = ast.unparse(node.returns)
                    except Exception:
                        pass

                sig_line = source_lines[node.lineno - 1].strip() if node.lineno <= len(source_lines) else ""
                doc = ast.get_docstring(node)

                sym = ParsedSymbol(
                    name=node.name,
                    kind="function",
                    signature_line=sig_line,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    docstring=doc.splitlines()[0] if doc else None,
                    is_public=not node.name.startswith("_"),
                )
                # Attach extra fields
                sym.calls = calls  # type: ignore[attr-defined]
                sym.parameters = params  # type: ignore[attr-defined]
                sym.return_type = ret_type  # type: ignore[attr-defined]
                symbols.append(sym)

            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                sig_line = source_lines[node.lineno - 1].strip() if node.lineno <= len(source_lines) else ""
                doc = ast.get_docstring(node)
                methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
                sym = ParsedSymbol(
                    name=node.name,
                    kind="class",
                    signature_line=sig_line,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    docstring=doc.splitlines()[0] if doc else None,
                    is_public=not node.name.startswith("_"),
                    methods=methods,
                )
                sym.calls = []  # type: ignore[attr-defined]
                sym.parameters = []  # type: ignore[attr-defined]
                sym.return_type = None  # type: ignore[attr-defined]
                symbols.append(sym)
    except Exception:
        pass
    return symbols


def generate_graph_node(file_path: str, project_root: str) -> dict[str, Any]:
    abs_path = os.path.join(project_root, file_path)
    if not os.path.exists(abs_path):
        return {}

    layer = _infer_layer(file_path)
    role = f"Handles {layer} logic."
    key_funcs = []
    
    ext = os.path.splitext(abs_path)[1].lower()
    
    lang = ts_get_language(ext)
    if lang:
        try:
            parsed = ts_parse_file(abs_path, lang)
            if parsed.module_docstring:
                role = parsed.module_docstring
            key_funcs = [s.name for s in parsed.symbols if s.is_public]
        except Exception:
            pass
    elif ext == ".py":
        doc = _get_python_docstring(abs_path)
        if doc:
            role = doc
        key_funcs = _get_python_public_symbols(abs_path)

    if not role.endswith("."):
        role += "."

    return {
        "file_path": file_path,
        "role": role,
        "type": "component" if layer != "utility" else "utility",
        "layer": layer,
        "stability": "high" if layer == "database" else "medium",
        "key_functions": key_funcs,
        "connects_to": [],
        "rules": [],
        "tests": [],
        "do_not_revert": False,
        "auto_generated": True,
    }

def generate_graph_sqlite(project_root: str, db_path: str | None = None) -> dict[str, Any]:
    if not db_path:
        from mcp_server.paths import get_data_dir
        db_path = str(get_data_dir() / "graph" / "graph.db")
        
    db = SQLiteGraph(db_path)
    
    file_paths = []
    for ext in TS_EXTENSION_MAP.keys():
        file_paths.extend([str(p.relative_to(project_root)) for p in Path(project_root).rglob(f"*{ext}")])
    file_paths.extend([str(p.relative_to(project_root)) for p in Path(project_root).rglob("*.py")])
    
    added = 0
    skipped = 0
    files_added = []
    
    import json
    for fp in file_paths:
        if "node_modules" in fp or ".venv" in fp:
            continue
            
        node_id = f"file:{fp}"
        existing = db.get_node(node_id)
        if existing:
            skipped += 1
            continue
            
        node_data = generate_graph_node(fp, project_root)
        if not node_data:
            continue
            
        db.add_node(
            node_id=node_id,
            kind="file",
            name=Path(fp).name,
            file_path=fp,
            role=node_data["role"],
            layer=node_data["layer"],
            stability=node_data["stability"],
            type=node_data["type"],
            key_functions=json.dumps(node_data["key_functions"]),
            dependencies="[]",
            rules="[]",
            do_not_revert=node_data.get("do_not_revert", False)
        )
        added += 1
        files_added.append(fp)
        
    # Build the full set of project file paths (used by Phases 2 and 4)
    all_node_paths = {fp for fp in file_paths if "node_modules" not in fp and ".venv" not in fp}

    # ---- Phase 2: Populate function-level symbols ----
    symbols_added = 0
    for fp in all_node_paths:
        file_node_id = f"file:{fp}"
        abs_path = os.path.join(project_root, fp)
        if not os.path.exists(abs_path):
            continue

        # Clear old symbols for this file (call_edges cascade via FK)
        db.remove_symbols_for_file(file_node_id)

        ext = os.path.splitext(abs_path)[1].lower()
        symbols_for_file = []

        lang = ts_get_language(ext)
        if lang:
            try:
                parsed = ts_parse_file(abs_path, lang)
                symbols_for_file = parsed.symbols
            except Exception:
                continue
        elif ext == ".py":
            symbols_for_file = _get_python_symbols_detailed(abs_path)

        for sym in symbols_for_file:
            sym_id = f"file:{fp}::{sym.name}"
            calls_json = json.dumps(getattr(sym, 'calls', []) if hasattr(sym, 'calls') else [])
            params_json = json.dumps(getattr(sym, 'parameters', []) if hasattr(sym, 'parameters') else [])
            ret_type = getattr(sym, 'return_type', None) if hasattr(sym, 'return_type') else None

            db.add_symbol(
                symbol_id=sym_id,
                file_node_id=file_node_id,
                name=sym.name,
                kind=sym.kind,
                signature=sym.signature_line,
                parameters=params_json,
                return_type=ret_type,
                start_line=sym.start_line,
                end_line=sym.end_line,
                docstring=sym.docstring,
                is_public=sym.is_public,
                calls=calls_json,
            )
            symbols_added += 1

    # ---- Phase 3: Resolve call edges between symbols ----
    call_edges_added = 0
    # Build a name→symbol_id lookup for the whole project
    all_symbols = {}
    for row in db.conn.execute("SELECT id, name FROM symbols").fetchall():
        name = row["name"]
        if name not in all_symbols:
            all_symbols[name] = row["id"]

    for row in db.conn.execute("SELECT id, calls FROM symbols WHERE calls IS NOT NULL AND calls != '[]'").fetchall():
        caller_id = row["id"]
        try:
            calls = json.loads(row["calls"])
        except (json.JSONDecodeError, TypeError):
            continue
        for callee_name in calls:
            if callee_name in all_symbols:
                callee_id = all_symbols[callee_name]
                if caller_id != callee_id:  # avoid self-edges
                    db.add_call_edge(caller_id, callee_id)
                    call_edges_added += 1

    # ---- Phase 4: Populate dependency edges from imports ----
    edges_added = 0
    for fp in all_node_paths:
        source_id = f"file:{fp}"
        abs_path = os.path.join(project_root, fp)
        if not os.path.exists(abs_path):
            continue

        # Clear old edges and re-derive from current imports
        db.remove_edges_for_node(source_id)

        try:
            imported_paths = extract_imports(abs_path, project_root)
        except Exception:
            continue

        for imp_path in imported_paths:
            target_id = f"file:{imp_path}"
            # Only create edges to files that exist in the graph
            if imp_path in all_node_paths or db.get_node(target_id):
                db.add_edge(source_id, target_id, kind="imports")
                edges_added += 1

    db.close()
    return {
        "files_processed": added + skipped,
        "nodes_added": added,
        "nodes_skipped": skipped,
        "edges_added": edges_added,
        "symbols_added": symbols_added,
        "call_edges_added": call_edges_added,
        "files_added": files_added,
    }

def generate_roadmap_stub(project_root: str, output_path: str):
    if os.path.exists(output_path):
        return

    phase_name = "Phase 1: Initial Development"
    desc = "Bootstrap project and core architecture."
    
    try:
        out = subprocess.check_output(
            ["git", "-C", project_root, "log", "-1", "--pretty=format:%s"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if out:
            desc = f"Latest context: {out}"
    except Exception:
        pass

    stub = {
        "project": Path(project_root).name,
        "version": "1.0",
        "current_phase": {
            "number": 1,
            "name": phase_name,
            "status": "in_progress",
            "next_action": "Review architecture and implement core components.",
            "open_changesets": [],
            "description": desc,
            "goal": desc,
        },
        "upcoming_phases": [],
        "deferred": [],
        "completed_phases": [],
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(stub, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"Created initial roadmap: {output_path}")
