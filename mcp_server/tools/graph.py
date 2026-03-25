import os
import time
from pathlib import Path
from typing import Any, Optional
import logging

from mcp_server.paths import get_data_dir, get_project_root
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)

def _graph_dir() -> Path:
    return get_data_dir() / "graph"

def _index_dir() -> Path:
    return get_data_dir() / "codeindex"

def _get_db() -> SQLiteGraph:
    db_path = _graph_dir() / "graph.db"
    return SQLiteGraph(db_path)

def _last_indexed_file() -> Path:
    return _index_dir() / ".last_indexed"

def _get_index_timestamp() -> float | None:
    lif = _last_indexed_file()
    if lif.exists():
        try:
            return float(lif.read_text().strip())
        except ValueError:
            return None
    return None

def _get_file_mtime(file_path: str) -> float | None:
    abs_path = get_project_root() / file_path
    if abs_path.exists():
        return abs_path.stat().st_mtime
    return None

def _check_staleness(file_path: str) -> dict[str, Any]:
    from datetime import datetime
    index_ts = _get_index_timestamp()
    file_mtime = _get_file_mtime(file_path)

    stale = False
    reason = "File is tracked and up-to-date with index."

    if file_mtime is None:
        reason = "File does not exist on disk."
        stale = True
    elif index_ts is None:
        reason = "Project index missing (.last_indexed not found)."
        stale = True
    elif file_mtime > index_ts:
        reason = "File modified AFTER last index build."
        stale = True

    return {
        "stale": stale,
        "reason": reason,
        "last_indexed": datetime.fromtimestamp(index_ts).isoformat() if index_ts else None,
        "file_mtime": datetime.fromtimestamp(file_mtime).isoformat() if file_mtime else None,
    }

def list_nodes(layer: str | None = None, do_not_revert: bool | None = None, stability: str | None = None) -> dict[str, Any]:
    db = _get_db()
    nodes = db.list_file_nodes(layer=layer, stability=stability, do_not_revert=do_not_revert)
    db.close()
    
    index_ts = _get_index_timestamp()
    result = []
    
    for n in nodes:
        fp = n['file_path']
        stale = None
        if index_ts is not None:
            file_mtime = _get_file_mtime(fp)
            if file_mtime is not None:
                stale = file_mtime > index_ts
                
        result.append({
            "file_path": fp,
            "role": n.get('role'),
            "layer": n.get('layer'),
            "stability": n.get('stability'),
            "do_not_revert": bool(n.get('do_not_revert')),
            "stale": stale
        })
        
    return {
        "count": len(result),
        "nodes": result,
        "hint": "Use get_node(file_path) to read the rules and dependencies for a specific file."
    }

def add_node(file_path: str, role: str, layer: str, stability: str = "medium", node_type: str = "file", key_functions: list[str] | None = None, connects_to: list[dict] | None = None, rules: list[str] | None = None, do_not_revert: bool = False, tests: list[str] | None = None) -> dict[str, str]:
    db = _get_db()
    import json
    
    node_id = f"file:{file_path}"
    
    db.add_node(
        node_id=node_id,
        kind="file",
        name=Path(file_path).name,
        file_path=file_path,
        role=role,
        layer=layer,
        stability=stability,
        type=node_type,
        key_functions=json.dumps(key_functions) if key_functions else None,
        dependencies=json.dumps(connects_to) if connects_to else None,
        rules=json.dumps(rules) if rules else None,
        do_not_revert=do_not_revert
    )
    db.close()
    return {"status": f"Graph node added for '{file_path}'"}

def update_node(file_path: str, changes: dict[str, Any]) -> dict[str, str]:
    db = _get_db()
    node = db.get_node_by_path(file_path)
    if not node:
        db.close()
        return {"error": f"Node '{file_path}' not found."}
        
    import json
    updates = {}
    for key, val in changes.items():
        if key in ["rules", "key_functions"]:
            existing = json.loads(node.get(key) or "[]")
            if isinstance(existing, list) and isinstance(val, list):
                updates[key] = json.dumps(list(set(existing + val)))
            else:
                updates[key] = json.dumps(val)
        elif key == "connects_to":
            existing = json.loads(node.get("dependencies") or "[]")
            if isinstance(existing, list) and isinstance(val, list):
                updates["dependencies"] = json.dumps(val)
        else:
            updates[key] = val
            
    db.update_node_metadata(node["id"], **updates)
    db.close()
    return {"status": f"Updated node '{file_path}'"}

def get_node(file_path: str) -> dict[str, Any]:
    db = _get_db()
    node = db.get_node_by_path(file_path)
    if not node:
        nodes = db.list_file_nodes()
        matches = [n for n in nodes if file_path in n['file_path']]
        if len(matches) == 1:
            node = matches[0]
            file_path = node['file_path']
            
    db.close()
    
    if not node:
        return {
            "found": False,
            "message": f"File '{file_path}' not found in the context graph.",
            "hint": "Use refresh_graph(['path/to/file']) to auto-generate a graph node for new files.",
        }
        
    staleness = _check_staleness(file_path)
    import json
    
    res_node = dict(node)
    for k in ["rules", "key_functions", "dependencies"]:
        if res_node.get(k):
            try:
                res_node[k] = json.loads(res_node[k])
            except:
                pass

    return {
        "found": True,
        "file_path": file_path,
        "node": res_node,
        "index_status": staleness,
        "hint": "Next: call get_signature(file_path) to see all public symbols and line ranges.",
    }

def get_impact(file_path: str) -> dict[str, Any]:
    db = _get_db()
    
    node = db.get_node_by_path(file_path)
    if not node:
        nodes = db.list_file_nodes()
        matches = [n for n in nodes if file_path in n['file_path']]
        if len(matches) == 1:
            node = matches[0]
            
    if not node:
        db.close()
        return {
            "found": False,
            "message": f"No graph node for '{file_path}'. Impact analysis unavailable.",
        }
        
    file_path = node['file_path']
    blast_radius = db.get_blast_radius(node['id'], max_depth=3)
    db.close()
    
    affected = []
    for r in blast_radius:
        path = r['file_path']
        if not any(a['file'] == path for a in affected) and path != file_path:
            affected.append({
                "file": path,
                "role": r.get('role', 'Unknown'),
                "stability": r.get('stability', 'medium'),
                "do_not_revert": bool(r.get('do_not_revert'))
            })

    return {
        "found": True,
        "target_file": file_path,
        "blast_radius": len(affected),
        "affected_files": affected,
    }

def refresh_graph(file_paths: list[str] | None = None) -> dict[str, Any]:
    from indexer.graph_generator import generate_graph_sqlite
    from mcp_server.paths import get_project_root
    from indexer.treesitter_parser import get_language
    
    root = get_project_root()
    if not file_paths:
        file_paths = []
        for p in root.rglob("*.*"):
            if get_language(p.suffix) is not None or p.suffix == ".py":
                if "node_modules" not in p.parts and ".venv" not in p.parts:
                    file_paths.append(str(p.relative_to(root)))
                    
    generated = 0
    db_path = str(_graph_dir() / "graph.db")
    # For a list of specific files, we can just call it (though the generator scans all files, 
    # it only adds missing ones).
    generate_graph_sqlite(str(root), db_path)
            
    return {
        "status": f"Generated graph nodes in SQLite DB.",
        "hint": "Call get_node(file_path) to read the new graph stub."
    }
