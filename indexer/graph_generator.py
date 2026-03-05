"""
Auto-generates context graph YAML nodes and roadmap stubs from codebase AST analysis.

Reduces first-time setup from 2–4 hours of manual YAML to one command:
  python index_codebase.py --full --generate-graph --bootstrap-roadmap

All auto-generated nodes are marked `auto_generated: true` so developers know
which nodes need human enrichment (rules, do_not_revert, semantic edge types).

Merge behavior: existing enriched nodes are NEVER overwritten — only new files
get auto-generated stubs.
"""
import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml


def _load_config() -> dict:
    """Load .agents/config.yaml if present."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


_config = _load_config()
_project_cfg = _config.get("project", {})

# Graph YAML files location (relative to agent framework root)
GRAPH_DIR_RELATIVE = ".agents/graph"

# Default watched dirs from config
DEFAULT_TARGET_DIRS: list[str] = _project_cfg.get("watched_dirs", ["src"])

# Rule-extraction patterns: lines/docstrings containing these signal a rule
RULE_PATTERNS = [
    re.compile(r"\bRULE\s*:", re.IGNORECASE),
    re.compile(r"\bNEVER\b"),
    re.compile(r"\bMUST NOT\b", re.IGNORECASE),
    re.compile(r"\bCRITICAL\s*:", re.IGNORECASE),
    re.compile(r"\bINVARIANT\s*:", re.IGNORECASE),
]

# Event detection patterns
PUBLISH_PATTERN = re.compile(r'\.publish\s*\(')
ON_EVENT_PATTERN = re.compile(r'@on_event|\.subscribe\s*\(|\.consume\s*\(')

# Stability heuristics
_HIGH_STABILITY_PATTERNS = {"core/", "schemas/", "api/routes/", "contracts/"}
_MEDIUM_STABILITY_DEFAULT = "medium"


def _infer_graph_file(file_path: str, graph_dir: Path) -> Path:
    """
    Infer which graph YAML file a node should go into based on file path.
    All nodes go to graph.yaml by default — override with graph_file parameter in add_node().
    """
    return graph_dir / "graph.yaml"


def _infer_type(file_path: str) -> str:
    """Infer node type from path patterns."""
    fp = file_path.lower()
    name = Path(file_path).name
    if "api/" in fp or "routes/" in fp:
        return "file"
    if "schemas/" in fp or "schema" in name:
        return "schema"
    if name in {"consumer.py", "worker.py", "handler.py"}:
        return "service"
    return "file"


def _infer_stability(file_path: str) -> str:
    """Infer stability from path heuristics."""
    fp = file_path.lower()
    for pattern in _HIGH_STABILITY_PATTERNS:
        if pattern in fp:
            return "high"
    return _MEDIUM_STABILITY_DEFAULT


def _infer_layer(file_path: str) -> str:
    """Infer architectural layer from path."""
    parts = Path(file_path).parts
    for part in parts:
        if part in {"generator", "assembler", "indexer", "scanner", "drift", "graph", "context"}:
            return part
        if part in {"api", "routes"}:
            return "api"
        if part in {"core", "datastore", "schemas"}:
            return part
        if part in {"contexts", "application", "providers", "control", "services", "handlers"}:
            return part
    return "unknown"


def _get_module_docstring(file_path: str) -> str:
    """Extract module-level docstring from a Python file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        return ast.get_docstring(tree) or ""
    except Exception:
        return ""


def _get_public_symbols(file_path: str) -> list[str]:
    """Extract all public function and class names from a Python file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return []

    symbols = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                symbols.append(node.name)
    seen = set()
    result = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _extract_rules_from_source(file_path: str) -> list[str]:
    """
    Scan source text for lines/comments that look like architectural rules.
    Extracts 50-char context around each match as the rule string.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    rules = []
    for line in lines:
        stripped = line.strip()
        if not (stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'")):
            continue
        for pattern in RULE_PATTERNS:
            if pattern.search(stripped):
                rule_text = stripped.lstrip("#").strip().strip('"""').strip("'").strip()
                if rule_text and rule_text not in rules and len(rule_text) > 5:
                    rules.append(rule_text[:200])
                break

    return rules[:5]  # Cap at 5 auto-extracted rules


def _extract_events(file_path: str) -> tuple[list[str], list[str]]:
    """
    Scan source for event publish/consume patterns.
    Returns (events_published, events_consumed).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception:
        return [], []

    published = []
    consumed = []

    for match in re.finditer(r'\.publish\s*\(\s*["\']([^"\']+)["\']', source):
        event = match.group(1)
        if event not in published:
            published.append(event)

    for match in re.finditer(r'(?:subscribe|consume|on_event)\s*\(\s*["\']([^"\']+)["\']', source):
        event = match.group(1)
        if event not in consumed:
            consumed.append(event)

    return published, consumed


def _find_tests(file_path: str, project_root: str) -> list[str]:
    """
    Find test files that likely cover a given source file.
    Searches tests/ directory for files containing the module stem name.
    """
    stem = Path(file_path).stem
    tests_dir = Path(project_root) / "tests"
    if not tests_dir.exists():
        return []

    matches = []
    for test_file in tests_dir.rglob("test_*.py"):
        if stem in test_file.stem or stem in test_file.read_text(errors="ignore"):
            rel = str(test_file.relative_to(project_root))
            if rel not in matches:
                matches.append(rel)
                if len(matches) >= 3:
                    break
    return matches


def _get_last_changed_by(file_path: str, project_root: str) -> str:
    """Get the last git commit subject line for a file."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%s", "-1", "--", file_path],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or ""
    except Exception:
        return ""


def generate_graph_node(file_path: str, project_root: str) -> dict[str, Any]:
    """
    Auto-generate a context graph node for a Python file.

    Args:
        file_path: Relative path to the Python file (from project root)
        project_root: Absolute path to project root

    Returns:
        A dict representing the graph node (ready to write to YAML).
    """
    abs_path = str(Path(project_root) / file_path)

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from chunker import extract_imports

    # Step 1: module docstring → role
    docstring = _get_module_docstring(abs_path)
    role = docstring.split("\n")[0].strip() if docstring else f"Auto-generated stub for {Path(file_path).name}"
    if len(role) > 120:
        role = role[:117] + "..."

    # Step 2: public symbols → key_functions
    key_functions = _get_public_symbols(abs_path)

    # Step 3: imports → connects_to edges
    imports = extract_imports(abs_path, project_root)
    connects_to = [
        {"target": imp, "edge": "depends_on", "via": "import"}
        for imp in imports
        if imp != file_path
    ]

    # Step 4: infer type, layer, stability
    node_type = _infer_type(file_path)
    layer = _infer_layer(file_path)
    stability = _infer_stability(file_path)

    # Step 5: find tests
    tests = _find_tests(file_path, project_root)

    # Step 6: last_changed_by from git
    last_changed_by = _get_last_changed_by(file_path, project_root)

    # Step 7: extract rules from source
    rules = _extract_rules_from_source(abs_path)

    # Step 8: detect event patterns
    events_published, events_consumed = _extract_events(abs_path)

    node = {
        "role": role,
        "type": node_type,
        "layer": layer,
        "stability": stability,
        "key_functions": key_functions,
        "connects_to": connects_to,
        "events_published": events_published,
        "events_consumed": events_consumed,
        "rules": rules,
        "tests": tests,
        "auto_generated": True,
    }

    if last_changed_by:
        node["last_changed_by"] = last_changed_by

    return node


def generate_graph_yaml(project_root: str, graph_dir: str | None = None) -> dict[str, Any]:
    """
    Generate/update graph YAML files for all Python files in the project.

    Merge behavior:
    - New files → auto-generated stub added
    - Existing nodes → SKIPPED (human enrichment preserved)

    Args:
        project_root: Absolute path to project root
        graph_dir: Absolute path to graph directory (defaults to .agents/graph/)

    Returns:
        dict with nodes_added, nodes_skipped, files_processed stats.
    """
    project_root_path = Path(project_root)
    graph_dir_path = Path(graph_dir) if graph_dir else project_root_path / GRAPH_DIR_RELATIVE

    # Load all existing nodes to detect what already exists
    existing_nodes: set[str] = set()
    for yaml_file in graph_dir_path.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f) or {}
            if "nodes" in data:
                existing_nodes.update(data["nodes"].keys())
        except Exception:
            pass

    # Use configured or default target dirs
    target_dirs = DEFAULT_TARGET_DIRS
    skip_dirs = {"__pycache__", ".venv", "venv", ".git", "node_modules", "migrations"}
    skip_files = {"__init__.py"}

    nodes_added = 0
    nodes_skipped = 0
    files_processed = 0
    files_added: list[str] = []

    yaml_buffers: dict[Path, dict] = {}

    for target_dir in target_dirs:
        target_path = project_root_path / target_dir
        if not target_path.exists():
            continue

        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                if not fname.endswith(".py") or fname in skip_files:
                    continue

                abs_path = Path(root) / fname
                rel_path = str(abs_path.relative_to(project_root_path))
                files_processed += 1

                if rel_path in existing_nodes:
                    nodes_skipped += 1
                    continue

                try:
                    node = generate_graph_node(rel_path, project_root)
                except Exception as e:
                    print(f"  Warning: failed to generate node for {rel_path}: {e}")
                    nodes_skipped += 1
                    continue

                target_yaml = _infer_graph_file(rel_path, graph_dir_path)

                if target_yaml not in yaml_buffers:
                    if target_yaml.exists():
                        with open(target_yaml) as f:
                            yaml_buffers[target_yaml] = yaml.safe_load(f) or {}
                    else:
                        yaml_buffers[target_yaml] = {}
                    if "nodes" not in yaml_buffers[target_yaml]:
                        yaml_buffers[target_yaml]["nodes"] = {}

                yaml_buffers[target_yaml]["nodes"][rel_path] = node
                nodes_added += 1
                files_added.append(rel_path)

    for yaml_path, data in yaml_buffers.items():
        with open(yaml_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {
        "nodes_added": nodes_added,
        "nodes_skipped": nodes_skipped,
        "files_processed": files_processed,
        "files_added": files_added,
        "graph_dir": str(graph_dir_path),
    }


def generate_graph_nodes_for_files(
    file_paths: list[str],
    project_root: str,
    graph_dir: str | None = None,
) -> dict[str, Any]:
    """
    Generate/update graph nodes for a specific list of files only.
    Used by refresh_graph() MCP tool for targeted updates.

    Merge behavior: existing nodes are SKIPPED (same as generate_graph_yaml).

    Returns dict with nodes_added, nodes_skipped, files_processed.
    """
    project_root_path = Path(project_root)
    graph_dir_path = Path(graph_dir) if graph_dir else project_root_path / GRAPH_DIR_RELATIVE

    existing_nodes: set[str] = set()
    for yaml_file in graph_dir_path.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f) or {}
            if "nodes" in data:
                existing_nodes.update(data["nodes"].keys())
        except Exception:
            pass

    nodes_added = 0
    nodes_skipped = 0
    yaml_buffers: dict[Path, dict] = {}
    files_added: list[str] = []

    for rel_path in file_paths:
        if not rel_path.endswith(".py"):
            nodes_skipped += 1
            continue

        if rel_path in existing_nodes:
            nodes_skipped += 1
            continue

        abs_path = project_root_path / rel_path
        if not abs_path.exists():
            nodes_skipped += 1
            continue

        try:
            node = generate_graph_node(rel_path, project_root)
        except Exception as e:
            print(f"  Warning: failed to generate node for {rel_path}: {e}")
            nodes_skipped += 1
            continue

        target_yaml = _infer_graph_file(rel_path, graph_dir_path)
        if target_yaml not in yaml_buffers:
            if target_yaml.exists():
                with open(target_yaml) as f:
                    yaml_buffers[target_yaml] = yaml.safe_load(f) or {}
            else:
                yaml_buffers[target_yaml] = {}
            if "nodes" not in yaml_buffers[target_yaml]:
                yaml_buffers[target_yaml]["nodes"] = {}

        yaml_buffers[target_yaml]["nodes"][rel_path] = node
        nodes_added += 1
        files_added.append(rel_path)

    for yaml_path, data in yaml_buffers.items():
        with open(yaml_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {
        "nodes_added": nodes_added,
        "nodes_skipped": nodes_skipped,
        "files_processed": len(file_paths),
        "files_added": files_added,
    }


def generate_roadmap_stub(project_root: str, roadmap_path: str | None = None) -> dict[str, Any]:
    """
    Generate a roadmap.yaml stub from git history.
    Only runs if the target file does NOT already exist — never overwrites.

    Args:
        project_root: Absolute path to project root
        roadmap_path: Absolute path to roadmap YAML (defaults to .agents/roadmap.yaml)

    Returns:
        dict with created (bool), path, and reason if skipped.
    """
    # Default: .agents/roadmap.yaml (framework-owned path)
    roadmap_file = Path(roadmap_path) if roadmap_path else Path(__file__).parent.parent / "roadmap.yaml"

    if roadmap_file.exists():
        return {
            "created": False,
            "path": str(roadmap_file),
            "reason": "roadmap.yaml already exists — not overwritten. Delete it to regenerate.",
        }

    # Gather git tags as phase milestones
    completed_phases = []
    try:
        tags_result = subprocess.run(
            ["git", "tag", "--sort=creatordate"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        tags = [t.strip() for t in tags_result.stdout.splitlines() if t.strip()]
    except Exception:
        tags = []

    for i, tag in enumerate(tags[:10], start=1):
        completed_phases.append({
            "phase": i,
            "name": tag,
            "status": "complete",
            "key_decisions": [],
            "auto_generated": True,
        })

    if not completed_phases:
        try:
            log_result = subprocess.run(
                ["git", "log", "--format=%s", "--merges", "-5"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            commits = [c.strip() for c in log_result.stdout.splitlines() if c.strip()]
            for i, commit in enumerate(commits, start=1):
                completed_phases.append({
                    "phase": i,
                    "name": commit[:60],
                    "status": "complete",
                    "key_decisions": [],
                    "auto_generated": True,
                })
        except Exception:
            pass

    next_phase_num = len(completed_phases) + 1

    roadmap = {
        "project": Path(project_root).name,
        "version": "1.0",
        "completed_phases": completed_phases,
        "current_phase": {
            "number": next_phase_num,
            "name": "Getting Started",
            "description": "Auto-generated stub — update with your actual phase name and goals.",
            "status": "pending",
            "next_action": "Review auto-generated graph nodes and enrich with rules and do_not_revert flags.",
            "open_changesets": [],
            "key_decisions": [],
            "auto_generated": True,
        },
        "upcoming_phases": [],
        "deferred": [],
    }

    roadmap_file.parent.mkdir(parents=True, exist_ok=True)
    with open(roadmap_file, "w") as f:
        yaml.dump(roadmap, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return {
        "created": True,
        "path": str(roadmap_file),
        "completed_phases_from_git": len(completed_phases),
        "current_phase": next_phase_num,
    }
