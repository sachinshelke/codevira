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

def export_graph(format: str = "mermaid", scope: str | None = None) -> dict[str, Any]:
    """Export the dependency graph as Mermaid or DOT format."""
    db = _get_db()
    try:
        nodes = db.list_file_nodes()
        edges = db.get_all_edges()

        # Filter by scope if provided
        if scope:
            nodes = [n for n in nodes if n["file_path"].startswith(scope)]
            node_ids = {f"file:{n['file_path']}" for n in nodes}
            edges = [e for e in edges if e["source_id"] in node_ids or e["target_id"] in node_ids]

        if format == "mermaid":
            output = _to_mermaid(nodes, edges)
        elif format == "dot":
            output = _to_dot(nodes, edges)
        else:
            return {"error": f"Unknown format '{format}'. Use 'mermaid' or 'dot'."}

        return {
            "format": format,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "output": output,
        }
    finally:
        db.close()


def _to_mermaid(nodes: list[dict], edges: list[dict]) -> str:
    lines = ["graph LR"]
    # Create safe node IDs for Mermaid
    id_map = {}
    for n in nodes:
        safe_id = n["file_path"].replace("/", "_").replace(".", "_").replace("-", "_")
        id_map[f"file:{n['file_path']}"] = safe_id
        label = Path(n["file_path"]).name
        stability = n.get("stability", "medium")
        style = ""
        if stability == "high":
            style = ":::high"
        elif stability == "low":
            style = ":::low"
        lines.append(f"    {safe_id}[\"{label}\"]{style}")

    for e in edges:
        src = id_map.get(e["source_id"])
        tgt = id_map.get(e["target_id"])
        if src and tgt:
            lines.append(f"    {src} --> {tgt}")

    return "\n".join(lines)


def _to_dot(nodes: list[dict], edges: list[dict]) -> str:
    lines = ["digraph codevira {", "    rankdir=LR;", "    node [shape=box, fontsize=10];"]
    id_map = {}
    for n in nodes:
        safe_id = n["file_path"].replace("/", "_").replace(".", "_").replace("-", "_")
        id_map[f"file:{n['file_path']}"] = safe_id
        label = Path(n["file_path"]).name
        color = {"high": "green", "medium": "yellow", "low": "red"}.get(n.get("stability", "medium"), "white")
        lines.append(f'    {safe_id} [label="{label}", fillcolor={color}, style=filled];')

    for e in edges:
        src = id_map.get(e["source_id"])
        tgt = id_map.get(e["target_id"])
        if src and tgt:
            lines.append(f"    {src} -> {tgt};")

    lines.append("}")
    return "\n".join(lines)


def get_graph_diff(base_ref: str = "main", head_ref: str = "HEAD") -> dict[str, Any]:
    """Show which graph nodes changed between two git refs and their blast radius."""
    import subprocess
    root = get_project_root()

    try:
        diff_output = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--name-only", f"{base_ref}...{head_ref}"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        # Fallback for when there's no common ancestor (e.g., same branch)
        try:
            diff_output = subprocess.check_output(
                ["git", "-C", str(root), "diff", "--name-only", base_ref, head_ref],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8").strip()
        except subprocess.CalledProcessError as e:
            return {
                "error": f"Could not compute diff between {base_ref} and {head_ref}",
                "detail": f"git exit code {e.returncode}. Ensure both refs exist.",
            }

    if not diff_output:
        return {"changed_files": [], "total_blast_radius": 0, "hint": "No files changed."}

    changed_files = [f for f in diff_output.split("\n") if f.strip()]

    db = _get_db()
    try:
        result_files = []
        all_affected = set()

        for fp in changed_files:
            node = db.get_node_by_path(fp)
            if node:
                blast = db.get_blast_radius(node["id"], max_depth=3)
                affected_paths = [r["file_path"] for r in blast if r["file_path"] != fp]
                all_affected.update(affected_paths)
                result_files.append({
                    "file_path": fp,
                    "in_graph": True,
                    "stability": node.get("stability", "medium"),
                    "do_not_revert": bool(node.get("do_not_revert")),
                    "blast_radius": len(affected_paths),
                    "affected": affected_paths[:5],  # Top 5 for brevity
                })
            else:
                result_files.append({
                    "file_path": fp,
                    "in_graph": False,
                    "stability": "unknown",
                    "do_not_revert": False,
                    "blast_radius": 0,
                    "affected": [],
                })

        return {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "changed_files": result_files,
            "total_changed": len(changed_files),
            "total_blast_radius": len(all_affected),
            "union_affected": list(all_affected)[:20],  # Top 20
        }
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# v1.5: query_graph — callers/callees/tests/dependents
# ---------------------------------------------------------------------------

def query_graph(file_path: str, symbol: str | None = None,
                query_type: str = "callees") -> dict[str, Any]:
    """
    Query the call graph.
    query_type: 'callers' | 'callees' | 'tests' | 'dependents' | 'symbols'
    """
    db = _get_db()
    try:
        if query_type == "symbols":
            # List all symbols in a file
            node_id = f"file:{file_path}"
            symbols = db.get_symbols_for_file(node_id)
            return {
                "file_path": file_path,
                "query_type": "symbols",
                "results": [
                    {"name": s["name"], "kind": s["kind"], "signature": s["signature"],
                     "start_line": s["start_line"], "end_line": s["end_line"],
                     "is_public": bool(s["is_public"])}
                    for s in symbols
                ],
                "count": len(symbols),
            }

        if symbol:
            sym = db.find_symbol(symbol, file_path)
        else:
            return {"error": "symbol is required for callers/callees/tests queries"}

        if not sym:
            return {"error": f"Symbol '{symbol}' not found in {file_path}",
                    "hint": "Call query_graph with query_type='symbols' to list available symbols."}

        sym_id = sym["id"]

        if query_type == "callers":
            callers = db.get_callers(sym_id)
            return {
                "file_path": file_path, "symbol": symbol, "query_type": "callers",
                "results": [{"name": c["name"], "kind": c["kind"],
                             "file": c["file_node_id"].replace("file:", "")}
                            for c in callers],
                "count": len(callers),
            }

        elif query_type == "callees":
            callees = db.get_callees(sym_id)
            return {
                "file_path": file_path, "symbol": symbol, "query_type": "callees",
                "results": [{"name": c["name"], "kind": c["kind"],
                             "file": c["file_node_id"].replace("file:", "")}
                            for c in callees],
                "count": len(callees),
            }

        elif query_type == "tests":
            # Find test files that import or call this file's functions
            node_id = f"file:{file_path}"
            # Check edges: which test files depend on this file?
            edges = db.conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND kind = 'imports'",
                (node_id,),
            ).fetchall()
            test_files = []
            for e in edges:
                src = e["source_id"].replace("file:", "")
                if "test" in src.lower():
                    test_files.append(src)
            return {
                "file_path": file_path, "symbol": symbol, "query_type": "tests",
                "test_files": test_files,
                "count": len(test_files),
            }

        elif query_type == "dependents":
            # Files that depend on the file containing this symbol
            node_id = f"file:{file_path}"
            blast = db.get_blast_radius(node_id, max_depth=2)
            return {
                "file_path": file_path, "symbol": symbol, "query_type": "dependents",
                "results": [{"file": r["file_path"]} for r in blast],
                "count": len(blast),
            }

        else:
            return {"error": f"Unknown query_type: {query_type}. Use: callers, callees, tests, dependents, symbols"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# v1.5: analyze_changes — function-level risk-scored change analysis
# ---------------------------------------------------------------------------

def analyze_changes(base_ref: str = "main", head_ref: str = "HEAD") -> dict[str, Any]:
    """
    Enhanced change analysis with function-level risk scoring.
    Maps git diff to affected functions, callers, and test coverage gaps.
    """
    import subprocess
    root = get_project_root()

    # Get changed files
    try:
        diff_output = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--name-only", f"{base_ref}...{head_ref}"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        try:
            diff_output = subprocess.check_output(
                ["git", "-C", str(root), "diff", "--name-only", base_ref, head_ref],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8").strip()
        except subprocess.CalledProcessError as e:
            return {"error": f"Could not compute diff: {e}"}

    if not diff_output:
        return {"changes": [], "summary": "No changes detected."}

    changed_files = [f for f in diff_output.split("\n") if f.strip()]

    db = _get_db()
    try:
        results = []
        total_risk = {"high": 0, "medium": 0, "low": 0}
        test_gaps = []

        for fp in changed_files:
            node_id = f"file:{fp}"
            symbols = db.get_symbols_for_file(node_id)

            # Check if any test files cover this file
            test_edges = db.conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND kind = 'imports'",
                (node_id,),
            ).fetchall()
            test_files = [e["source_id"].replace("file:", "") for e in test_edges
                          if "test" in e["source_id"].lower()]
            has_tests = len(test_files) > 0

            for sym in symbols:
                sym_id = sym["id"]
                callers = db.get_callers(sym_id)
                caller_count = len(callers)

                # Risk scoring
                is_public = bool(sym.get("is_public"))
                if is_public and caller_count >= 3 and not has_tests:
                    risk = "high"
                elif is_public and caller_count >= 1:
                    risk = "medium"
                else:
                    risk = "low"

                total_risk[risk] += 1

                if is_public and not has_tests:
                    test_gaps.append({"file": fp, "symbol": sym["name"], "callers": caller_count})

                results.append({
                    "file": fp,
                    "symbol": sym["name"],
                    "kind": sym["kind"],
                    "risk": risk,
                    "caller_count": caller_count,
                    "has_tests": has_tests,
                    "callers": [c["name"] for c in callers[:5]],
                })

        return {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "changed_files": len(changed_files),
            "functions_analyzed": len(results),
            "risk_summary": total_risk,
            "test_gaps": test_gaps[:10],
            "details": results[:30],  # Top 30 for token efficiency
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# v1.5: find_hotspots — complexity and risk hotspots
# ---------------------------------------------------------------------------

def find_hotspots(threshold: int = 50) -> dict[str, Any]:
    """
    Find complexity hotspots: large functions, high fan-in, high fan-out,
    and low confidence areas.
    """
    db = _get_db()
    try:
        # Large functions
        large_funcs = db.find_hotspot_functions(min_lines=threshold)

        # High fan-in (many callers = high risk if changed)
        high_fan_in = db.find_high_fan_in(min_callers=3)

        # High fan-out (files with many dependencies = fragile)
        fan_out = db.conn.execute('''
            SELECT source_id, COUNT(target_id) as dep_count
            FROM edges
            GROUP BY source_id
            HAVING dep_count >= 5
            ORDER BY dep_count DESC
            LIMIT 10
        ''').fetchall()

        return {
            "large_functions": [
                {"file": f.get("full_path", ""), "name": f["name"], "lines": f["line_count"],
                 "kind": f["kind"]}
                for f in large_funcs[:10]
            ],
            "high_fan_in": [
                {"name": h["name"], "kind": h["kind"], "callers": h["caller_count"],
                 "file": h["file_node_id"].replace("file:", "")}
                for h in high_fan_in[:10]
            ],
            "high_fan_out": [
                {"file": f["source_id"].replace("file:", ""), "dependencies": f["dep_count"]}
                for f in fan_out
            ],
            "threshold": threshold,
        }
    finally:
        db.close()
