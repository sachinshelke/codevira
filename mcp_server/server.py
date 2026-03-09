"""
Agent MCP Server

Exposes the project context graph, roadmap, code index, and changeset tracker
as MCP tools — usable by any MCP-compatible AI coding tool.

Tools:
  get_node(file_path)                    → graph node: role, connections, rules
  get_impact(file_path)                  → blast radius before touching a file
  get_roadmap()                          → current phase, next action, open changesets
  search_codebase(query, limit, layer)   → semantic search via local ChromaDB
  list_open_changesets()                 → check for unfinished multi-file work
  start_changeset(id, desc, files)       → begin tracking a multi-file fix
  update_changeset_progress(id, file)    → mark a file done within a changeset
  complete_changeset(id, decisions)      → mark changeset done, record decisions
  update_node(file_path, changes)        → update graph node after session
  update_next_action(next_action)        → update roadmap next action
  refresh_graph(file_paths?)             → auto-generate graph nodes for new files
  get_signature(file_path)               → skeleton: public symbols, signatures, line ranges
  get_code(file_path, symbol?)           → full source of one function or class from disk

Usage (Claude Code .claude/settings.json):
  {
    "mcpServers": {
      "codevira": {
        "command": "codevira-mcp",
        "args": [],
        "cwd": "/path/to/your-project"
      }
    }
  }

Usage (Cursor / Windsurf): configure via their MCP settings UI with same command.
"""
import sys

try:
    import mcp.server.stdio
    from mcp.server import Server
    from mcp.types import Tool, TextContent
except ImportError:
    print("ERROR: mcp package not installed.")
    print("Run: pip install 'mcp>=1.0.0'")
    sys.exit(1)

import json
from mcp_server.tools.graph import get_node, get_impact, list_nodes, add_node, refresh_graph
from mcp_server.tools.roadmap import (
    get_roadmap, get_full_roadmap, get_phase,
    add_phase, update_phase_status, defer_phase,
    complete_phase, update_next_action,
    add_open_changeset, remove_open_changeset,
)
from mcp_server.tools.search import search_codebase, refresh_index, search_decisions, get_history, write_session_log
from mcp_server.tools.changesets import (
    start_changeset,
    update_changeset_progress,
    complete_changeset,
    get_changeset,
    list_open_changesets,
    update_node_after_change,
)
from mcp_server.tools.playbook import get_playbook
from mcp_server.tools.code_reader import get_signature, get_code

server = Server("codevira")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_node",
            description=(
                "Get the context graph node for a file. Returns role, connections, rules, "
                "stability, tests, and do_not_revert flags. "
                "Call this INSTEAD of reading the source file to understand what it does."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'src/services/generator.py')",
                    }
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_impact",
            description=(
                "Get the full blast radius for a file before touching it. "
                "BFS traversal of graph edges to find all downstream affected files. "
                "ALWAYS call this before modifying any file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File you are about to modify",
                    }
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_roadmap",
            description=(
                "Get current project state: phase number, name, status, next action, "
                "open changesets, and upcoming phases. Call at the START of every session."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search_codebase",
            description=(
                "Semantic search over the codebase. Returns relevant functions, "
                "classes, or module docs. Use to find implementation patterns before writing code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language or code query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results (default 5, max 10)",
                        "default": 5,
                    },
                    "layer": {
                        "type": "string",
                        "description": "Filter by architectural layer",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_full_roadmap",
            description=(
                "Get the complete roadmap: all completed phases with key decisions, "
                "current phase, all upcoming phases, and deferred items. "
                "Use for planning sessions. More expensive than get_roadmap() — "
                "only call when you need full project history."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_phase",
            description="Get full details of any phase by number — completed, current, or upcoming.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phase_number": {
                        "type": ["integer", "string"],
                        "description": "Phase number (e.g. 19, '8R', '12A')",
                    }
                },
                "required": ["phase_number"],
            },
        ),
        Tool(
            name="add_phase",
            description=(
                "Add a new upcoming phase to the roadmap. "
                "Call when you identify new work during a session — gaps, refactors, follow-ups. "
                "High-priority phases are inserted at the front of the queue."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phase": {"type": ["integer", "string"], "description": "Phase number or label"},
                    "name": {"type": "string", "description": "Short phase name"},
                    "description": {"type": "string", "description": "What this phase does and why"},
                    "priority": {"type": "string", "description": "high | medium | low", "default": "medium"},
                    "depends_on": {"type": "array", "items": {"type": ["integer", "string"]}},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "Key files that will be touched"},
                    "effort": {"type": "string", "description": "Rough estimate e.g. '~2 hours'"},
                },
                "required": ["phase", "name", "description"],
            },
        ),
        Tool(
            name="update_phase_status",
            description=(
                "Update the current phase status: pending | in_progress | blocked. "
                "Call when starting work on a phase (in_progress) or when blocked."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "pending | in_progress | blocked"},
                    "blocker": {"type": "string", "description": "Required when status=blocked"},
                    "started": {"type": "string", "description": "ISO date override (defaults to today)"},
                },
                "required": ["status"],
            },
        ),
        Tool(
            name="defer_phase",
            description=(
                "Move an upcoming phase to the deferred list. "
                "Use when priorities shift or a phase depends on unavailable work."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phase_number": {"type": ["integer", "string"]},
                    "reason": {"type": "string", "description": "Why this is being deferred"},
                },
                "required": ["phase_number", "reason"],
            },
        ),
        Tool(
            name="complete_phase",
            description=(
                "Mark the current phase as complete and advance to the next upcoming phase. "
                "Records key_decisions permanently. Requires phase_number to match current phase (safety check)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phase_number": {"type": ["integer", "string"], "description": "Must match current phase"},
                    "key_decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Decisions made — preserved for all future agents",
                    },
                },
                "required": ["phase_number", "key_decisions"],
            },
        ),
        Tool(
            name="list_open_changesets",
            description=(
                "List all in-progress multi-file changesets. "
                "Call at session start to check for unfinished work from previous sessions."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="start_changeset",
            description=(
                "Begin tracking a multi-file fix. Call BEFORE touching any files. "
                "Creates a changeset record for cross-session continuity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "changeset_id": {
                        "type": "string",
                        "description": "Short slug (e.g. 'auth-refactor')",
                    },
                    "description": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "All files that will be modified",
                    },
                    "trigger": {
                        "type": "string",
                        "enum": ["small_fix", "medium_change", "large_change"],
                        "default": "medium_change",
                    },
                },
                "required": ["changeset_id", "description", "files"],
            },
        ),
        Tool(
            name="update_changeset_progress",
            description="Mark a file as done within an active changeset.",
            inputSchema={
                "type": "object",
                "properties": {
                    "changeset_id": {"type": "string"},
                    "file_done": {"type": "string", "description": "File path that was completed"},
                    "blocker": {"type": "string", "description": "Optional blocker note if session ending early"},
                },
                "required": ["changeset_id", "file_done"],
            },
        ),
        Tool(
            name="complete_changeset",
            description=(
                "Mark a changeset as complete and record key decisions. "
                "Call at session end after all files in the changeset are done."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "changeset_id": {"type": "string"},
                    "decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key decisions made (preserved for future agents)",
                    },
                },
                "required": ["changeset_id", "decisions"],
            },
        ),
        Tool(
            name="update_node",
            description=(
                "Update a graph node after modifying a file. "
                "Call at session end for each file you changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "changes": {
                        "type": "object",
                        "description": "Fields to update: last_changed_by (str), new_rules (list), do_not_revert (bool)",
                    },
                },
                "required": ["file_path", "changes"],
            },
        ),
        Tool(
            name="list_nodes",
            description=(
                "List all nodes in the context graph with brief summaries. "
                "Use at session start to discover what's in the graph and spot stale files. "
                "Filter by layer, do_not_revert, or stability."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "layer": {
                        "type": "string",
                        "description": "Filter by architectural layer",
                    },
                    "do_not_revert": {
                        "type": "boolean",
                        "description": "If true, return only protected nodes",
                    },
                    "stability": {
                        "type": "string",
                        "description": "Filter by stability: low | medium | high",
                    },
                },
            },
        ),
        Tool(
            name="add_node",
            description=(
                "Add a new node to the context graph for a newly created file. "
                "Call this after creating a new file. Graph file is auto-inferred from path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "role": {"type": "string", "description": "One-line description of what the file does"},
                    "layer": {"type": "string", "description": "Architectural layer"},
                    "stability": {"type": "string", "description": "low | medium | high", "default": "medium"},
                    "node_type": {"type": "string", "description": "file | service | schema | event", "default": "file"},
                    "key_functions": {"type": "array", "items": {"type": "string"}},
                    "connects_to": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Edge list: [{target, edge, via}]",
                    },
                    "rules": {"type": "array", "items": {"type": "string"}},
                    "do_not_revert": {"type": "boolean", "default": False},
                    "tests": {"type": "array", "items": {"type": "string"}},
                    "graph_file": {"type": "string", "description": "Target YAML filename (auto-inferred if omitted)"},
                },
                "required": ["file_path", "role", "layer"],
            },
        ),
        Tool(
            name="search_decisions",
            description=(
                "Search past decisions across all completed changesets, roadmap phases, and session logs. "
                "Answers 'has anyone decided this before?' — gives agents institutional memory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search (e.g. 'threshold', 'uuid', 'retry')"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                    "session_id": {"type": "string", "description": "Optional — filter results to a specific session only"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_history",
            description=(
                "Get the last N git commits that touched a file. "
                "Links graph node last_changed_by to actual commits."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "n": {"type": "integer", "description": "Number of commits (default 5, max 20)", "default": 5},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="write_session_log",
            description=(
                "Write a structured session log to .agents/logs/YYYY-MM-DD/. "
                "Called by the Documenter at the end of every session. "
                "Feeds search_decisions() with institutional memory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Short ID (8-char slug)"},
                    "task": {"type": "string", "description": "Original developer prompt"},
                    "task_type": {"type": "string", "description": "small_fix | medium_change | large_change"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "phase": {"type": ["integer", "string"]},
                    "next_action": {"type": "string"},
                    "agents_invoked": {"type": "array", "items": {"type": "string"}},
                    "tests_run": {"type": "array", "items": {"type": "string"}},
                    "tests_passed": {"type": "boolean"},
                    "build_clean": {"type": "boolean"},
                    "changeset_id": {"type": "string"},
                },
                "required": ["session_id", "task", "task_type", "files_changed", "decisions", "phase", "next_action"],
            },
        ),
        Tool(
            name="refresh_index",
            description=(
                "Trigger an incremental reindex of changed files. "
                "Call when get_node() returns index_status.stale=true, or before searching "
                "files you know have changed. Pass file_paths to reindex specific files only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific files to reindex. Omit to reindex all changed files.",
                    }
                },
            },
        ),
        Tool(
            name="get_playbook",
            description=(
                "Get curated architectural rules for a specific task type. "
                "Returns only the 2-3 relevant rule files — not all of them. "
                "Use for: add_route | add_service | add_schema | "
                "debug_pipeline | commit | write_test"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "description": "Task type (e.g. 'add_route', 'debug_pipeline', 'commit')",
                    }
                },
                "required": ["task_type"],
            },
        ),
        Tool(
            name="update_next_action",
            description="Update the roadmap's next_action field. Call at session end.",
            inputSchema={
                "type": "object",
                "properties": {
                    "next_action": {
                        "type": "string",
                        "description": "Exact description of what the next agent should do",
                    }
                },
                "required": ["next_action"],
            },
        ),
        Tool(
            name="refresh_graph",
            description=(
                "Auto-generate context graph nodes for new or unregistered files. "
                "Call after creating a new file to get a graph stub immediately — "
                "no CLI needed. Safe merge: existing enriched nodes are never overwritten. "
                "Pass file_paths to target specific files, or omit to scan all new files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Relative file paths to generate nodes for. "
                            "Omit to auto-detect all Python files not yet in the graph."
                        ),
                    }
                },
            },
        ),
        Tool(
            name="get_signature",
            description=(
                "Get the skeleton of a Python file — all public function and class names, "
                "their signatures, docstrings, and line ranges. "
                "Call this after get_node() to understand file structure before deciding "
                "which symbol to read with get_code(). Much cheaper than reading the full file. "
                "Note: Python files only. For other languages, read the file directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'src/services/generator.py')",
                    }
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_code",
            description=(
                "Get the full source of a single function or class by name. "
                "Always reads from disk — always current, never stale. "
                "Call get_signature() first to discover available symbol names and line ranges. "
                "Omit symbol to get module-level constants and assignments only. "
                "Note: Python files only. For other languages, read the file directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Function or class name to retrieve. Omit for module-level constants.",
                    },
                },
                "required": ["file_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "get_node":
            result = get_node(arguments["file_path"])
        elif name == "get_impact":
            result = get_impact(arguments["file_path"])
        elif name == "get_roadmap":
            result = get_roadmap()
        elif name == "get_full_roadmap":
            result = get_full_roadmap()
        elif name == "get_phase":
            result = get_phase(arguments["phase_number"])
        elif name == "add_phase":
            result = add_phase(
                phase=arguments["phase"],
                name=arguments["name"],
                description=arguments["description"],
                priority=arguments.get("priority", "medium"),
                depends_on=arguments.get("depends_on"),
                files=arguments.get("files"),
                effort=arguments.get("effort"),
            )
        elif name == "update_phase_status":
            result = update_phase_status(
                status=arguments["status"],
                blocker=arguments.get("blocker"),
                started=arguments.get("started"),
            )
        elif name == "defer_phase":
            result = defer_phase(
                phase_number=arguments["phase_number"],
                reason=arguments["reason"],
            )
        elif name == "complete_phase":
            result = complete_phase(
                phase_number=arguments["phase_number"],
                key_decisions=arguments["key_decisions"],
            )
        elif name == "search_codebase":
            result = search_codebase(
                arguments["query"],
                limit=arguments.get("limit", 5),
                layer=arguments.get("layer"),
            )
        elif name == "list_open_changesets":
            result = list_open_changesets()
        elif name == "start_changeset":
            result = start_changeset(
                arguments["changeset_id"],
                arguments["description"],
                arguments["files"],
                trigger=arguments.get("trigger", "medium_change"),
            )
        elif name == "update_changeset_progress":
            result = update_changeset_progress(
                arguments["changeset_id"],
                arguments["file_done"],
                blocker=arguments.get("blocker"),
            )
        elif name == "complete_changeset":
            result = complete_changeset(
                arguments["changeset_id"],
                arguments["decisions"],
            )
        elif name == "update_node":
            result = update_node_after_change(
                arguments["file_path"],
                arguments["changes"],
            )
        elif name == "list_nodes":
            result = list_nodes(
                layer=arguments.get("layer"),
                do_not_revert=arguments.get("do_not_revert"),
                stability=arguments.get("stability"),
            )
        elif name == "add_node":
            result = add_node(
                file_path=arguments["file_path"],
                role=arguments["role"],
                layer=arguments["layer"],
                stability=arguments.get("stability", "medium"),
                node_type=arguments.get("node_type", "file"),
                key_functions=arguments.get("key_functions"),
                connects_to=arguments.get("connects_to"),
                rules=arguments.get("rules"),
                do_not_revert=arguments.get("do_not_revert", False),
                tests=arguments.get("tests"),
                graph_file=arguments.get("graph_file"),
            )
        elif name == "search_decisions":
            result = search_decisions(
                arguments["query"],
                limit=arguments.get("limit", 10),
                session_id=arguments.get("session_id"),
            )
        elif name == "get_history":
            result = get_history(arguments["file_path"], n=arguments.get("n", 5))
        elif name == "write_session_log":
            result = write_session_log(
                session_id=arguments["session_id"],
                task=arguments["task"],
                task_type=arguments["task_type"],
                files_changed=arguments["files_changed"],
                decisions=arguments["decisions"],
                phase=arguments["phase"],
                next_action=arguments["next_action"],
                agents_invoked=arguments.get("agents_invoked"),
                tests_run=arguments.get("tests_run"),
                tests_passed=arguments.get("tests_passed"),
                build_clean=arguments.get("build_clean"),
                changeset_id=arguments.get("changeset_id"),
            )
        elif name == "refresh_index":
            result = refresh_index(file_paths=arguments.get("file_paths"))
        elif name == "get_playbook":
            result = get_playbook(arguments["task_type"])
        elif name == "update_next_action":
            result = update_next_action(arguments["next_action"])
        elif name == "refresh_graph":
            result = refresh_graph(file_paths=arguments.get("file_paths"))
        elif name == "get_signature":
            result = get_signature(arguments["file_path"])
        elif name == "get_code":
            result = get_code(arguments["file_path"], symbol=arguments.get("symbol"))
        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as e:
        result = {"error": str(e), "tool": name}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def main():
    import asyncio

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
