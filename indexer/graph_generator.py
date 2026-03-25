import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from indexer.treesitter_parser import (
    parse_file as ts_parse_file,
    get_language as ts_get_language,
    EXTENSION_MAP as TS_EXTENSION_MAP,
)
from indexer.sqlite_graph import SQLiteGraph

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

def generate_graph_node(file_path: str, project_root: str) -> dict[str, Any]:
    abs_path = os.path.join(project_root, file_path)
    if not os.path.exists(abs_path):
        return {}

    layer = _infer_layer(file_path)
    role = f"Handles {layer} logic."
    key_funcs = []
    
    ext = os.path.splitext(abs_path)[1].lower()
    
    connects_to = []
    lang = ts_get_language(ext)
    if lang:
        try:
            parsed = ts_parse_file(abs_path, lang)
            if parsed.module_docstring:
                role = parsed.module_docstring
            key_funcs = [s.name for s in parsed.symbols if s.is_public]
            for imp in parsed.imports:
                connects_to.append(imp.module)
        except Exception:
            pass
    elif ext == ".py":
        doc = _get_python_docstring(abs_path)
        if doc:
            role = doc
        key_funcs = _get_python_public_symbols(abs_path)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for name in node.names:
                            connects_to.append(name.name)
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            connects_to.append(node.module)
        except Exception:
            pass

    if not role.endswith("."):
        role += "."

    return {
        "file_path": file_path,
        "role": role,
        "type": "component" if layer != "utility" else "utility",
        "layer": layer,
        "stability": "high" if layer == "database" else "medium",
        "key_functions": key_funcs,
        "connects_to": connects_to,
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
            dependencies=json.dumps(node_data.get("connects_to", [])),
            connects_to=node_data.get("connects_to", []),
            rules="[]",
            do_not_revert=node_data.get("do_not_revert", False)
        )
        added += 1
        files_added.append(fp)
        
    db.close()
    return {
        "files_processed": added + skipped,
        "nodes_added": added,
        "nodes_skipped": skipped,
        "files_added": files_added
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

    stub = f"""# Auto-generated by codevira-mcp init
current_phase: 1
next_action: "Review architecture and implement core components."
open_changesets: []

phases:
  - number: 1
    name: "{phase_name}"
    description: "{desc}"
    status: in_progress
    key_decisions: []

deferred: []
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(stub)
    print(f"Created initial roadmap: {output_path}")
