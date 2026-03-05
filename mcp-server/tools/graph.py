"""
MCP tools for querying the project context graph.
Reads .agents/graph/*.yaml files — no code files needed.
"""
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

GRAPH_DIR = Path(__file__).parent.parent.parent / "graph"
PROJECT_ROOT = GRAPH_DIR.parent.parent
INDEX_DIR = GRAPH_DIR.parent / "codeindex"
LAST_INDEXED_FILE = INDEX_DIR / ".last_indexed"


def _get_index_timestamp() -> float | None:
    """Return the last index build timestamp, or None."""
    if LAST_INDEXED_FILE.exists():
        try:
            return float(LAST_INDEXED_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _get_file_mtime(file_path: str) -> float | None:
    """Return mtime of a source file, or None if it doesn't exist."""
    abs_path = PROJECT_ROOT / file_path
    if abs_path.exists():
        return abs_path.stat().st_mtime
    return None


def _check_staleness(file_path: str) -> dict[str, Any]:
    """
    Check if a file has been modified since the last index build.

    Returns a dict with:
      - stale: True if the file was modified after the last index build
      - reason: human-readable explanation
      - last_indexed: ISO timestamp of last index build (or None)
      - file_mtime: ISO timestamp of file's last modification (or None)
    """
    from datetime import datetime

    index_ts = _get_index_timestamp()
    file_mtime = _get_file_mtime(file_path)

    if index_ts is None:
        return {
            "stale": None,
            "reason": "No index timestamp found — run: python .agents/indexer/index_codebase.py --full",
        }

    if file_mtime is None:
        return {
            "stale": None,
            "reason": f"File not found on disk: {file_path}",
        }

    is_stale = file_mtime > index_ts
    return {
        "stale": is_stale,
        "reason": (
            "File modified after last index build — run: python .agents/indexer/index_codebase.py"
            if is_stale else "Index is current for this file"
        ),
        "last_indexed": datetime.fromtimestamp(index_ts).strftime("%Y-%m-%d %H:%M:%S"),
        "file_mtime": datetime.fromtimestamp(file_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _load_all_nodes() -> dict[str, Any]:
    """Load all nodes from all graph YAML files into a flat dict keyed by file_path."""
    nodes: dict[str, Any] = {}
    for yaml_file in GRAPH_DIR.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data and "nodes" in data:
                nodes.update(data["nodes"])
        except Exception:
            pass
    return nodes


def _find_graph_file_for_path(file_path: str) -> Path | None:
    """Return the graph YAML file that contains a given file_path node, or None."""
    for yaml_file in GRAPH_DIR.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data and "nodes" in data and file_path in data["nodes"]:
                return yaml_file
        except Exception:
            pass
    return None


def _infer_graph_file(file_path: str) -> Path:
    """
    Infer which graph YAML file a new node should be added to.
    Falls back to graph.yaml for all files. Override by passing graph_file explicitly.
    """
    return GRAPH_DIR / "graph.yaml"


def list_nodes(
    layer: str | None = None,
    do_not_revert: bool | None = None,
    stability: str | None = None,
) -> dict[str, Any]:
    """
    List all nodes in the context graph with brief summaries.
    Supports optional filtering by layer, do_not_revert flag, or stability.

    Args:
        layer: Filter by layer name
        do_not_revert: If True, return only protected nodes; if False, non-protected only
        stability: Filter by stability (low | medium | high)

    Returns:
        List of nodes with file_path, role, layer, stability, do_not_revert, index_stale.
    """
    nodes = _load_all_nodes()
    index_ts = _get_index_timestamp()

    result = []
    for fp, node in nodes.items():
        if layer and node.get("layer") != layer:
            continue
        if do_not_revert is not None and node.get("do_not_revert", False) != do_not_revert:
            continue
        if stability and node.get("stability") != stability:
            continue

        # Quick staleness check (mtime only, no datetime formatting overhead)
        stale = None
        if index_ts is not None:
            file_mtime = _get_file_mtime(fp)
            if file_mtime is not None:
                stale = file_mtime > index_ts

        result.append({
            "file_path": fp,
            "role": node.get("role", ""),
            "layer": node.get("layer", ""),
            "stability": node.get("stability", ""),
            "do_not_revert": node.get("do_not_revert", False),
            "rules_count": len(node.get("rules", [])),
            "index_stale": stale,
            "last_changed_by": node.get("last_changed_by", ""),
        })

    # Sort: do_not_revert first, then by layer, then file_path
    result.sort(key=lambda x: (not x["do_not_revert"], x["layer"], x["file_path"]))

    return {
        "total": len(result),
        "filters_applied": {k: v for k, v in {"layer": layer, "do_not_revert": do_not_revert, "stability": stability}.items() if v is not None},
        "nodes": result,
    }


def add_node(
    file_path: str,
    role: str,
    layer: str,
    stability: str = "medium",
    node_type: str = "file",
    key_functions: list[str] | None = None,
    connects_to: list[dict] | None = None,
    rules: list[str] | None = None,
    do_not_revert: bool = False,
    tests: list[str] | None = None,
    graph_file: str | None = None,
) -> dict[str, Any]:
    """
    Add a new node to the context graph for a newly created file.
    Call this after creating a new file in the codebase.

    Args:
        file_path: Relative file path (e.g. 'src/services/new_service.py')
        role: One-line description of what the file does
        layer: Architectural layer
        stability: low | medium | high
        node_type: file | service | schema | event
        key_functions: List of important public functions/classes
        connects_to: List of edge dicts: [{target, edge, via}]
        rules: List of invariants that must never be violated
        do_not_revert: True if this file contains intentional protected decisions
        tests: List of test files covering this node
        graph_file: Which graph YAML to add to (auto-inferred if not provided)

    Returns:
        success, file_path, added_to (which YAML file)
    """
    # Check if node already exists
    existing = _find_graph_file_for_path(file_path)
    if existing:
        return {
            "success": False,
            "message": f"Node '{file_path}' already exists in {existing.name}. Use update_node() to modify it.",
        }

    # Determine target graph file
    if graph_file:
        target_yaml = GRAPH_DIR / graph_file
        if not target_yaml.exists():
            return {"success": False, "message": f"Graph file '{graph_file}' not found in {GRAPH_DIR}"}
    else:
        target_yaml = _infer_graph_file(file_path)

    # Build the node
    node: dict[str, Any] = {
        "role": role,
        "type": node_type,
        "layer": layer,
        "stability": stability,
        "key_functions": key_functions or [],
        "connects_to": connects_to or [],
        "events_published": [],
        "events_consumed": [],
        "rules": rules or [],
        "tests": tests or [],
    }
    if do_not_revert:
        node["do_not_revert"] = True

    # Load or create the target YAML file
    if target_yaml.exists():
        with open(target_yaml) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    if "nodes" not in data:
        data["nodes"] = {}

    data["nodes"][file_path] = node

    with open(target_yaml, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {
        "success": True,
        "file_path": file_path,
        "added_to": target_yaml.name,
        "node": node,
        "hint": "Call update_node() at session end to add last_changed_by.",
    }


def get_node(file_path: str) -> dict[str, Any]:
    """
    Return the graph node for a given file path.
    Agents call this instead of reading the actual source file.

    Returns node with: role, type, layer, stability, key_functions,
    connects_to, rules, tests, do_not_revert, last_changed_by.
    """
    nodes = _load_all_nodes()

    # Exact match first
    if file_path in nodes:
        staleness = _check_staleness(file_path)
        return {
            "found": True,
            "file_path": file_path,
            "node": nodes[file_path],
            "index_status": staleness,
            "hint": "Next: call get_signature(file_path) to see all public symbols and line ranges, then get_code(file_path, symbol) for a specific function body.",
        }

    # Fuzzy match — check if file_path is a substring of any key
    matches = [k for k in nodes if file_path in k or k in file_path]
    if len(matches) == 1:
        staleness = _check_staleness(matches[0])
        return {
            "found": True,
            "file_path": matches[0],
            "node": nodes[matches[0]],
            "index_status": staleness,
            "hint": "Next: call get_signature(file_path) to see all public symbols and line ranges, then get_code(file_path, symbol) for a specific function body.",
        }
    if len(matches) > 1:
        return {
            "found": False,
            "message": f"Ambiguous match. Did you mean one of: {matches}",
            "candidates": matches,
        }

    return {
        "found": False,
        "message": f"No graph node found for '{file_path}'. The file may not be in the context graph yet.",
        "hint": "Add it to .agents/graph/graph.yaml or call refresh_graph() to auto-generate a stub.",
    }


def refresh_graph(file_paths: list[str] | None = None) -> dict[str, Any]:
    """
    Auto-generate graph nodes for new or specified files without running the CLI.

    Agents call this after creating a new file to get a graph stub immediately.
    Safe merge: existing enriched nodes are NEVER overwritten.

    Args:
        file_paths: Specific relative file paths to process.
                    If None, processes all Python files that are NOT yet in the graph.

    Returns:
        nodes_added, nodes_skipped, files_processed, files_added
    """
    indexer_dir = PROJECT_ROOT / ".agents" / "indexer"
    graph_generator_path = indexer_dir / "graph_generator.py"

    if not graph_generator_path.exists():
        return {
            "success": False,
            "error": "graph_generator.py not found. Run: python .agents/indexer/index_codebase.py --full first.",
        }

    import sys
    sys.path.insert(0, str(indexer_dir))

    try:
        from graph_generator import generate_graph_nodes_for_files, generate_graph_yaml
    except ImportError as e:
        return {"success": False, "error": f"Failed to import graph_generator: {e}"}

    try:
        if file_paths:
            result = generate_graph_nodes_for_files(
                file_paths=file_paths,
                project_root=str(PROJECT_ROOT),
                graph_dir=str(GRAPH_DIR),
            )
        else:
            result = generate_graph_yaml(
                project_root=str(PROJECT_ROOT),
                graph_dir=str(GRAPH_DIR),
            )
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_impact(file_path: str) -> dict[str, Any]:
    """
    Return the full blast radius for a given file — BFS traversal of graph edges.
    Call this BEFORE touching any file to understand what else will be affected.

    Returns ordered list of affected files with their roles, edge types, and risk level.
    """
    nodes = _load_all_nodes()

    # Resolve the starting node
    start_key = None
    if file_path in nodes:
        start_key = file_path
    else:
        matches = [k for k in nodes if file_path in k or k in file_path]
        if len(matches) == 1:
            start_key = matches[0]

    if not start_key:
        return {
            "found": False,
            "message": f"No graph node for '{file_path}'. Impact analysis unavailable.",
            "hint": "Proceed with caution — manually check downstream consumers.",
        }

    # BFS traversal
    visited: set[str] = set()
    queue: list[tuple[str, str, str, int]] = [(start_key, "", "", 0)]  # (node, edge, via, depth)
    impact: list[dict] = []

    while queue:
        current, edge_type, via, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if current != start_key:
            node_data = nodes.get(current, {})
            impact.append({
                "file_path": current,
                "depth": depth,
                "edge_type": edge_type,
                "via": via,
                "role": node_data.get("role", "unknown"),
                "stability": node_data.get("stability", "unknown"),
                "do_not_revert": node_data.get("do_not_revert", False),
                "rules": node_data.get("rules", []),
                "tests": node_data.get("tests", []),
            })

        # Follow outgoing edges from current node
        node_data = nodes.get(current, {})
        for edge in node_data.get("connects_to", []):
            target = edge.get("target", "")
            if target and target not in visited:
                queue.append((
                    target,
                    edge.get("edge", ""),
                    str(edge.get("via", "")),
                    depth + 1,
                ))

    # Sort by depth then stability (low stability = higher risk)
    stability_order = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
    impact.sort(key=lambda x: (x["depth"], stability_order.get(x["stability"], 3)))

    # Collect all tests from blast radius
    all_tests = list({t for item in impact for t in item.get("tests", [])})

    # Check staleness of source file
    source_staleness = _check_staleness(start_key)

    return {
        "source": start_key,
        "source_role": nodes[start_key].get("role", ""),
        "source_stability": nodes[start_key].get("stability", ""),
        "source_index_status": source_staleness,
        "blast_radius": len(impact),
        "affected_files": impact,
        "tests_to_run": all_tests,
        "high_risk_files": [i["file_path"] for i in impact if i.get("do_not_revert")],
    }
