"""
Codevira MCP Server

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
        "command": "codevira",
        "args": [],
        "cwd": "/path/to/your-project"
      }
    }
  }

Usage (Cursor / Windsurf): configure via their MCP settings UI with same command.
"""

from __future__ import annotations

import sys

try:
    import mcp.server.stdio
    from mcp.server import Server
    from mcp.types import Tool, TextContent
except ImportError:
    # Use stderr — stdout is the MCP protocol channel in stdio mode.
    # Printing to stdout here would corrupt the MCP handshake.
    print(
        "ERROR: mcp package not installed. Run: pip install 'mcp>=1.0.0'",
        file=sys.stderr,
    )
    raise

import json
from mcp_server.tools.graph import (
    get_node,
    get_impact,
    list_nodes,
    add_node,
    update_node,
    refresh_graph,
    export_graph,
    get_graph_diff,
    query_graph as query_graph_tool,
    analyze_changes as analyze_changes_tool,
    find_hotspots as find_hotspots_tool,
)
from mcp_server.tools.roadmap import (
    get_roadmap,
    get_full_roadmap,
    get_phase,
    add_phase,
    update_phase_status,
    defer_phase,
    complete_phase,
    update_next_action,
)
from mcp_server.tools.search import (
    search_codebase,
    refresh_index,
    search_decisions,
    get_history,
    write_session_log,
    write_session_logs,  # v2.1.2 Item 24
    list_decisions,
    list_tags,  # v2.1.2 Items 11 + 27
)
from mcp_server.tools.changesets import (
    start_changeset,
    update_changeset_progress,
    complete_changeset,
    list_open_changesets,
)
from mcp_server.tools.playbook import get_playbook
from mcp_server.tools.code_reader import get_signature, get_code
from mcp_server.tools.learning import (
    get_decision_confidence as learning_get_decision_confidence,
    get_preferences as learning_get_preferences,
    get_learned_rules as learning_get_learned_rules,
    get_project_maturity as learning_get_project_maturity,
    get_session_context as learning_get_session_context,
)

from mcp_server import __version__ as _codevira_version

# Pass version so MCP's serverInfo handshake response reports the codevira
# version (e.g. "1.8.0"). Without this, clients see serverInfo.version ==
# the mcp framework library's own version (currently 1.27.0), which is
# misleading — clients use serverInfo.version for telemetry and version
# gating.
server = Server("codevira", version=_codevira_version)


# ---- MCP Prompts (workflow templates) ----
@server.list_prompts()
async def handle_list_prompts():
    from mcp_server.prompts import list_prompts as _list_prompts
    from mcp.types import Prompt, PromptArgument

    prompts = _list_prompts()
    return [
        Prompt(
            name=p["name"],
            description=p.get("description"),
            arguments=[
                PromptArgument(
                    name=a["name"],
                    description=a.get("description"),
                    required=a.get("required", False),
                )
                for a in p.get("arguments", [])
            ],
        )
        for p in prompts
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict | None = None):
    from mcp_server.prompts import get_prompt as _get_prompt
    from mcp.types import (
        GetPromptResult,
        PromptMessage,
        TextContent as PromptTextContent,
    )

    result = _get_prompt(name, arguments)
    if not result:
        raise ValueError(f"Unknown prompt: {name}")
    return GetPromptResult(
        description=result["description"],
        messages=[
            PromptMessage(
                role=m["role"],
                content=PromptTextContent(type="text", text=m["content"]["text"]),
            )
            for m in result["messages"]
        ],
    )


# ---- MCP Resources (Hero 8 — Decision Replay) ----
#
# We expose two URIs:
#   - codevira://decisions          — full timeline (last 30 days)
#   - codevira://decisions/<query>  — filtered by query substring
#
# Clients that render HTML in resource content (Claude Desktop) display
# the rich timeline. Plain-text clients fall back to the raw HTML string
# (acceptable degradation; the user can pipe through a viewer).
#
# v2.1 may upgrade to MCP Apps SEP-1865 ``ui://`` URIs for proper iframe
# rendering once the SDK exposes that scheme.


@server.list_resources()
async def handle_list_resources():
    from mcp.types import Resource
    from pydantic import AnyUrl

    return [
        Resource(
            uri=AnyUrl("codevira://decisions"),
            name="Codevira decisions timeline",
            description=(
                "Browse this project's decision history with outcomes "
                "and session context. Use codevira://decisions/<query> "
                "to filter by substring."
            ),
            mimeType="text/html",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri):
    """Render a decision-replay timeline at the requested URI.

    Defensive: any read error returns a friendly HTML message rather
    than raising — Hero 8 is a browse surface; data flakiness must
    yield a degraded result, not a broken client experience.
    """
    from mcp_server.decision_replay import build_timeline, render_html
    from mcp_server.paths import get_data_dir

    uri_str = str(uri)
    query: str | None = None

    if uri_str == "codevira://decisions":
        title = "Codevira Replay"
    elif uri_str.startswith("codevira://decisions/"):
        # Everything after the prefix is the query, URL-decoded if needed.
        from urllib.parse import unquote

        raw_query = uri_str[len("codevira://decisions/") :]
        query = unquote(raw_query) or None
        title = f"Codevira Replay — query: {query!r}"
    else:
        # Unknown URI — let the SDK report not-found via ValueError.
        raise ValueError(f"Unknown codevira resource: {uri_str!r}")

    try:
        from indexer.sqlite_graph import SQLiteGraph

        graph_db = get_data_dir() / "graph" / "graph.db"
        if not graph_db.exists():
            return render_html([], title=title)
        g = SQLiteGraph(graph_db)
        try:
            timeline = build_timeline(g.conn, query=query, since_days=30, limit=20)
        finally:
            g.close()
        return render_html(timeline, title=title)
    except Exception as e:  # noqa: BLE001
        # Bug-X-shape defense: never let resource-read crash the MCP
        # client. Return an HTML page with the error so the user knows.
        import html as _html

        return (
            f"<!DOCTYPE html><html><body>"
            f"<h1>{_html.escape(title)}</h1>"
            f"<p style='color:red'>Codevira couldn't load decisions: "
            f"{_html.escape(str(e))}</p></body></html>"
        )


@server.list_tools()
async def list_tools() -> list[Tool]:
    # Hide tools whose deps aren't installed — AI agents only see what works.
    from indexer.index_codebase import _check_search_deps

    _has_search = _check_search_deps()

    tools = [
        Tool(
            name="get_node",
            description=(
                "Get the context graph node for a file. Returns a SUMMARY by default "
                "(role, layer, stability, rules_count, deps_count, stale flag) — ~100 tokens. "
                "Pass full=true for the complete rules/dependencies/key_functions arrays. "
                "Call this INSTEAD of reading the source file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'src/services/generator.py')",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "Include full rules + dependencies arrays (default false — summary only)",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_impact",
            description=(
                "Get the blast radius for a file before modifying it. "
                "Default: returns up to 10 affected files + counts (~400 tokens). "
                "Pass summary_only=true for just counts (blast_radius, protected_count, "
                "high_stability_count) — ~80 tokens, perfect for gate checks. "
                "ALWAYS call before modifying any file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File you are about to modify",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max affected files to return (default 10, max 100)",
                    },
                    "summary_only": {
                        "type": "boolean",
                        "description": "Return only counts, not the file list (default false)",
                    },
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
                "Semantic search over the codebase. Returns file+symbol pointers "
                "by default (~300 tokens for 5 matches). Call get_code(file_path, symbol) "
                "to read source for a specific match. Pass include_content=true to inline "
                "source code in results (500-3000 tokens per match — use sparingly)."
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
                        "description": "Number of results (default 5, max 20)",
                        "default": 5,
                    },
                    "include_content": {
                        "type": "boolean",
                        "description": "Inline chunk source code in results (default false)",
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
                "Get the roadmap with current phase, upcoming, deferred, and a summary "
                "of completed phases. By default completed phases are summarized "
                "(name + number) to keep response small. Pass include_decisions=true "
                "for full history, or use get_phase(number) for one specific phase."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_decisions": {
                        "type": "boolean",
                        "description": "Include full key_decisions from all completed phases (default false)",
                    },
                },
            },
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
                    "phase": {
                        "type": ["integer", "string"],
                        "description": "Phase number or label",
                    },
                    "name": {"type": "string", "description": "Short phase name"},
                    "description": {
                        "type": "string",
                        "description": "What this phase does and why",
                    },
                    "priority": {
                        "type": "string",
                        "description": "high | medium | low",
                        "default": "medium",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": ["integer", "string"]},
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key files that will be touched",
                    },
                    "effort": {
                        "type": "string",
                        "description": "Rough estimate e.g. '~2 hours'",
                    },
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
                    "status": {
                        "type": "string",
                        "description": "pending | in_progress | blocked",
                    },
                    "blocker": {
                        "type": "string",
                        "description": "Required when status=blocked",
                    },
                    "started": {
                        "type": "string",
                        "description": "ISO date override (defaults to today)",
                    },
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
                    "reason": {
                        "type": "string",
                        "description": "Why this is being deferred",
                    },
                },
                "required": ["phase_number", "reason"],
            },
        ),
        Tool(
            name="complete_phase",
            description=(
                "Mark the current phase as complete and advance to the next upcoming phase. "
                "Records key_decisions permanently. Requires phase_number to match current phase (safety check). "
                "v2.1.2 Item 10: pass backfill=True + completed_at='YYYY-MM-DD' to retroactively "
                "mark a historical phase done without advancing the queue. "
                "v2.1.2 Item 12: pass git_ref to link a commit sha or PR ref to the completion."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phase_number": {
                        "type": ["integer", "string"],
                        "description": "Must match current phase (unless backfill=True)",
                    },
                    "key_decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Decisions made — preserved for all future agents",
                    },
                    "backfill": {
                        "type": "boolean",
                        "description": "v2.1.2: allow marking any phase done without advancing the queue",
                    },
                    "completed_at": {
                        "type": "string",
                        "description": "v2.1.2: ISO date for backfill (defaults to today)",
                    },
                    "git_ref": {
                        "type": "string",
                        "description": "v2.1.2: optional commit sha / PR ref the phase shipped at",
                    },
                },
                "required": ["phase_number", "key_decisions"],
            },
        ),
        Tool(
            name="bulk_import_phases",
            description=(
                "v2.1.2 Item 29: backfill multiple historical phases at once. "
                "Each item: {number, name, status?='done', completed_at?, "
                "key_decisions?, git_ref?, description?}. Idempotent. Useful "
                "for adopting codevira on a project that already shipped N "
                "phases in git."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phases": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of phase dicts to import",
                    },
                },
                "required": ["phases"],
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
                    "file_done": {
                        "type": "string",
                        "description": "File path that was completed",
                    },
                    "blocker": {
                        "type": "string",
                        "description": "Optional blocker note if session ending early",
                    },
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
                "Use changes={'do_not_revert': true} to PROTECT a FILE from "
                "future AI edits that would undo architectural decisions "
                "(Hero 1 / Decision Lock enforces it). "
                "For DECISION-LEVEL protection (a specific decision rather "
                "than the whole file), use record_decision(do_not_revert=true) "
                "instead — that's lighter and lets one file hold multiple "
                "independently-protected decisions. "
                "Call at session end for each file you changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "changes": {
                        "type": "object",
                        "description": (
                            "Fields to update: do_not_revert (bool — "
                            "protect file from AI reverts), "
                            "last_changed_by (str), new_rules (list)"
                        ),
                    },
                },
                "required": ["file_path", "changes"],
            },
        ),
        Tool(
            name="list_nodes",
            description=(
                "List nodes in the context graph (PAGINATED: 50 per call by default). "
                "Returns total count, layer distribution, and the requested page of nodes. "
                "Use filters (layer, stability, do_not_revert) to narrow results. "
                "For a specific file's full details, call get_node(file_path) instead."
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
                    "limit": {
                        "type": "integer",
                        "description": "Max nodes to return per page (default 50, max 500)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of nodes to skip (for pagination)",
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
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path",
                    },
                    "role": {
                        "type": "string",
                        "description": "One-line description of what the file does",
                    },
                    "layer": {"type": "string", "description": "Architectural layer"},
                    "stability": {
                        "type": "string",
                        "description": "low | medium | high",
                        "default": "medium",
                    },
                    "node_type": {
                        "type": "string",
                        "description": "file | service | schema | event",
                        "default": "file",
                    },
                    "key_functions": {"type": "array", "items": {"type": "string"}},
                    "connects_to": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Edge list: [{target, edge, via}]",
                    },
                    "rules": {"type": "array", "items": {"type": "string"}},
                    "do_not_revert": {"type": "boolean", "default": False},
                    "tests": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["file_path", "role", "layer"],
            },
        ),
        Tool(
            name="search_decisions",
            description=(
                "Search past decisions across sessions, changesets, and roadmap phases. "
                "v2.1.1: hybrid BM25+semantic with RRF. v2.1.2 Item 1: applies a "
                "self-calibrating similarity threshold so gibberish queries return "
                "zero results (not 'least bad' matches). v2.1.2 Item 28: pass "
                "summary_only=true for a ~70% smaller payload (triage queries). "
                "Default: 5 matches with truncated context (~500 tokens). "
                "Pass full=true for untruncated text. Answers 'has anyone decided this before?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search (e.g. 'threshold', 'uuid', 'retry')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, max 20)",
                        "default": 5,
                    },
                    "full": {
                        "type": "boolean",
                        "description": "Return untruncated decision text (default false)",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional — filter to a specific session",
                    },
                    "summary_only": {
                        "type": "boolean",
                        "description": "v2.1.2 Item 28: return id+summary+score only (smallest payload)",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_decisions",
            description=(
                "v2.1.2 Item 11: enumerate decisions with filters (since_date, "
                "file_pattern, protected_only, session_id, tags). Closes the gap "
                "that 'codevira can remember things across sessions, but can't "
                "list what it remembers.' Returns ~50 tokens per row by default."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max rows (default 20, max 200)",
                    },
                    "since_date": {
                        "type": "string",
                        "description": "ISO 8601 timestamp or YYYY-MM-DD",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "SQL LIKE pattern on file_path",
                    },
                    "protected_only": {
                        "type": "boolean",
                        "description": "Only do_not_revert=true rows",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Filter to one session",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter to rows matching ALL these tags (v2.1.2 Item 27)",
                    },
                    "include_superseded": {
                        "type": "boolean",
                        "description": "Include soft-deleted rows (v2.1.2 Item 26)",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "Untruncated decision text",
                    },
                },
            },
        ),
        Tool(
            name="list_tags",
            description=(
                "v2.1.2 Item 27: enumerate all tags in the project with decision "
                "counts. Useful for discovery — 'what categories of decisions do "
                "we track?'"
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="record_decisions",
            description=(
                "v2.1.2 Item 23: batch variant of record_decision. Cuts ~26 "
                "round trips on memory-dump sessions to ONE. Each item accepts "
                "the same fields as record_decision (decision, file_path, "
                "context, do_not_revert, session_id, tags, force). Returns "
                "{count, recorded:[ids], errors:[...]}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of decision dicts",
                    },
                },
                "required": ["decisions"],
            },
        ),
        Tool(
            name="write_session_logs",
            description=(
                "v2.1.2 Item 24: batch variant of write_session_log. Each item: "
                "{session_id, task, phase, files_changed?, decisions?, "
                "next_steps?}. Returns {count, session_ids, errors}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "logs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of session-log dicts",
                    },
                },
                "required": ["logs"],
            },
        ),
        Tool(
            name="supersede_decision",
            description=(
                "v2.1.2 Item 26: retire ``old_id`` and link to a replacement. "
                "Writes the new decision with `[supersedes #<old_id>: <reason>]` "
                "prefix, sets the old row as superseded. Default-hidden in "
                "search / list (pass include_superseded=true to opt back in)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "old_id": {"type": "integer", "description": "Decision to retire"},
                    "new_decision": {
                        "type": "string",
                        "description": "Replacement decision text",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the prior decision changed",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional file path",
                    },
                    "context": {"type": "string", "description": "Optional context"},
                    "do_not_revert": {
                        "type": "boolean",
                        "description": "Lock the replacement",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["old_id", "new_decision", "reason"],
            },
        ),
        Tool(
            name="check_conflict",
            description=(
                "v2.1.2 Item 20: check whether a proposed decision contradicts "
                "any existing do_not_revert=True decision OR duplicates an "
                "existing one. Returns {status: novel|duplicate|conflict, "
                "conflicts: [...], duplicates: [...]}. Call this BEFORE "
                "record_decision when you want to surface conflicts proactively. "
                "(record_decision also runs this internally and surfaces a "
                "_conflict_warning unless force=true.)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision_text": {
                        "type": "string",
                        "description": "The decision text you'd pass to record_decision",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional — prefer hits on the same file",
                    },
                },
                "required": ["decision_text"],
            },
        ),
        Tool(
            name="get_history",
            description=(
                "Get recent decisions touching a file. Default: 5 with truncated context "
                "(~500 tokens). Pass full=true for untruncated text. "
                "Ordered by most recent first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max decisions (default 5, max 50)",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "Untruncated decision text (default false)",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="record_decision",
            description=(
                "Record a single architectural decision and (optionally) mark "
                "it as protected with do_not_revert=true so future AI sessions "
                "treat it as an architectural constraint. Use this for "
                "decisions you want to LOCK across sessions and across IDEs "
                "(e.g. 'use Postgres for the cortex metadata store, not "
                "SQLite — we need multi-host operator access'). Lighter-"
                "weight than write_session_log, ideal for ad-hoc captures. "
                "Returns {decision_id, session_id} for follow-up edits via "
                "mark_decision_protected. The flag is surfaced in subsequent "
                "search_decisions() calls so other AI sessions see it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "description": "The decision itself (1 sentence is fine)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional file/path the decision pertains to",
                    },
                    "context": {
                        "type": "string",
                        "description": "Why this won (alternatives, what would force re-examination)",
                    },
                    "do_not_revert": {
                        "type": "boolean",
                        "description": (
                            "If true, mark the decision as protected — future "
                            "sessions will see do_not_revert=true and must NOT "
                            "propose changes that conflict without surfacing "
                            "this decision to the user first. Default false."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session id to attach to (auto-generated if omitted)",
                    },
                },
                "required": ["decision"],
            },
        ),
        Tool(
            name="mark_decision_protected",
            description=(
                "Flip the do_not_revert flag on an existing decision by id. "
                "Use to retroactively protect a decision that was originally "
                "logged without do_not_revert, or to UNprotect one that no "
                "longer applies (pass do_not_revert=false). Find the id via "
                "search_decisions()."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision_id": {
                        "type": "integer",
                        "description": "Decision id from search_decisions output",
                    },
                    "do_not_revert": {
                        "type": "boolean",
                        "description": "true to protect, false to unprotect",
                    },
                },
                "required": ["decision_id", "do_not_revert"],
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
                    "session_id": {
                        "type": "string",
                        "description": "Short ID (8-char slug)",
                    },
                    "task": {
                        "type": "string",
                        "description": "Original developer prompt",
                    },
                    "phase": {"type": "string", "description": "phase"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "decision": {"type": "string"},
                                "context": {"type": "string"},
                            },
                        },
                    },
                    "next_steps": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "session_id",
                    "task",
                    "phase",
                    "files_changed",
                    "decisions",
                    "next_steps",
                ],
            },
        ),
        Tool(
            name="refresh_index",
            description=(
                "Trigger an incremental reindex of changed files. "
                "Always refreshes the context graph (no extra deps needed). "
                "Also updates the semantic search index if chromadb is installed. "
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
                "Valid task types: add_tool | add_service | add_schema | "
                "debug_pipeline | commit | write_test"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "description": "Task type (e.g. 'add_tool', 'debug_pipeline', 'commit')",
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
        # ---- v1.4: Graph Visualization & Diff ----
        Tool(
            name="export_graph",
            description=(
                "Export the dependency graph as a Mermaid or DOT diagram. "
                "Use for documentation, PR descriptions, or onboarding. "
                "Pass scope to limit to a directory (e.g. 'src/services/')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["mermaid", "dot"],
                        "description": "Output format: 'mermaid' or 'dot'",
                        "default": "mermaid",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Filter to files under this directory prefix",
                    },
                },
            },
        ),
        Tool(
            name="get_graph_diff",
            description=(
                "Show which graph nodes changed between two git refs and their blast radius. "
                "Use before opening a PR to understand the impact of your changes. "
                "Defaults to comparing main...HEAD."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_ref": {
                        "type": "string",
                        "description": "Base git ref (default: 'main')",
                        "default": "main",
                    },
                    "head_ref": {
                        "type": "string",
                        "description": "Head git ref (default: 'HEAD')",
                        "default": "HEAD",
                    },
                },
            },
        ),
        # ---- v1.4: Learning & Adaptive Memory ----
        Tool(
            name="get_decision_confidence",
            description=(
                "Get confidence scores for a file or pattern based on outcome history. "
                "Returns how often past decisions were kept, modified, or reverted. "
                "Call this before making decisions in an area to gauge reliability."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Specific file to check confidence for",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Directory or file pattern to check (e.g. 'src/api/')",
                    },
                },
            },
        ),
        Tool(
            name="get_preferences",
            description=(
                "Get learned developer preferences from past correction patterns. "
                "Returns coding style signals: naming conventions, structural preferences, patterns. "
                "Call this before writing code to match the developer's style."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category: 'naming' | 'structure' | 'patterns' | 'formatting'",
                    },
                },
            },
        ),
        Tool(
            name="get_learned_rules",
            description=(
                "Get auto-generated rules from observed patterns across sessions. "
                "These rules are learned from what works — test pairing patterns, import rules, "
                "co-change patterns. Higher confidence = more reliable. "
                "Use alongside get_playbook() for comprehensive guidance. "
                "Each rule comes with a numeric `id` — pass that to retire_rule() "
                "if a rule has gone stale (e.g. pinned to a deleted directory)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path to get rules for (matches by pattern)",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter: 'testing' | 'imports' | 'structure' | 'patterns' | 'naming'",
                    },
                    "include_retired": {
                        "type": "boolean",
                        "description": "Include rules previously retired (default false — audit only)",
                    },
                },
            },
        ),
        Tool(
            name="retire_rule",
            description=(
                "Retire a stale learned rule by its numeric id. The rule is "
                "marked retired (kept in the table for audit) and stops "
                "appearing in get_learned_rules() / get_session_context() "
                "and stops firing as a high-confidence signal in policies. "
                "Call this when get_learned_rules surfaces a rule pinned to "
                "a directory or pattern that no longer exists in the codebase. "
                "Provide a short reason so future sessions understand why it was retired."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "integer",
                        "description": "Numeric id from get_learned_rules() output",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the rule is being retired (e.g. 'src/control/cli/ deleted in Plan 1 Week 2')",
                    },
                },
                "required": ["rule_id"],
            },
        ),
        Tool(
            name="get_project_maturity",
            description=(
                "Get overall project intelligence and maturity metrics. "
                "Shows session count, file coverage, confidence score, learned rules, "
                "and preference signals. The higher the score, the less ambiguous agent decisions are."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_session_context",
            description=(
                "Single 'catch me up' call for cross-tool continuity. "
                "Returns current roadmap phase, open changesets, recent decisions with confidence, "
                "learned preferences, and active rules — everything a new session needs. "
                "Call this at the START of every session instead of multiple separate calls. "
                "Works seamlessly across AI tools: Cursor, Claude Code, Windsurf, Antigravity."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # ---- v1.5: Deep Graph Intelligence Tools ----
        Tool(
            name="query_graph",
            description=(
                "Query the function-level call graph. Find callers, callees, tests, or dependents "
                "for a specific symbol. Use query_type='symbols' to list all functions in a file."
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
                        "description": "Function or class name to query",
                    },
                    "query_type": {
                        "type": "string",
                        "description": "callers | callees | tests | dependents | symbols",
                        "default": "callees",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="analyze_changes",
            description=(
                "Function-level risk-scored change analysis. Maps git diff to affected functions, "
                "counts callers, flags test coverage gaps, assigns risk scores (high/medium/low). "
                "Use before code review or pre-commit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_ref": {
                        "type": "string",
                        "description": "Base git ref (default: main)",
                        "default": "main",
                    },
                    "head_ref": {
                        "type": "string",
                        "description": "Head git ref (default: HEAD)",
                        "default": "HEAD",
                    },
                },
            },
        ),
        Tool(
            name="find_hotspots",
            description=(
                "Find complexity and risk hotspots: large functions (exceeding line threshold), "
                "high fan-in symbols (many callers = risky to change), and high fan-out files "
                "(many dependencies = fragile)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "integer",
                        "description": "Min lines for large function (default: 50)",
                        "default": 50,
                    },
                },
            },
        ),
    ]

    # Filter out tools whose optional dependencies aren't installed.
    # AI agents only see tools that will actually work.
    if not _has_search:
        tools = [t for t in tools if t.name != "search_codebase"]

    # Hide admin / reporting / dashboard tools from the AI-facing MCP tool list.
    # These still work if called explicitly (via call_tool dispatch) but are
    # not advertised — they dump too many tokens or serve human workflows only.
    # Humans access them via the CLI (codevira index, codevira status) or
    # dedicated MCP prompts (architecture_overview, pre_commit_check).
    _ADMIN_TOOLS = {
        "list_nodes",  # replaced by get_node(path) targeted queries
        "add_node",  # auto-generated by refresh_graph
        "refresh_graph",  # background/automatic
        "refresh_index",  # background/automatic
        "export_graph",  # 5k-50k token Mermaid/DOT dump
        "get_graph_diff",  # PR review — use prompt instead
        "analyze_changes",  # PR review — use prompt instead
        "find_hotspots",  # dashboard metric — use prompt instead
        "get_project_maturity",  # dashboard metric
        "get_preferences",  # included in get_session_context
        "get_learned_rules",  # included in get_session_context
        "get_full_roadmap",  # rarely needed by agents — use get_phase(n)
    }
    tools = [t for t in tools if t.name not in _ADMIN_TOOLS]

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # v1.6: Auto-init check — triggers background init on first call if needed.
    # This is a no-op (<1ms) on every subsequent call after initialization.
    try:
        from mcp_server.auto_init import ensure_project_initialized

        ensure_project_initialized()
    except Exception:
        pass  # auto-init must never block tool dispatch

    # v2.0 (engine sprint, week 1, round-3 QA): run pre_tool_use through
    # the engine. Most tool calls return action="allow" (no policies
    # registered yet, or none claim PRE_TOOL_USE). When a hero policy
    # IS registered (Hero 1 onward), this is the integration point that
    # lets it block/warn/inject before the tool dispatches.
    #
    # Failures here NEVER break tool dispatch — the wiring layer
    # swallows engine errors and returns allow.
    try:
        # Lazy: register Hero policies on first call. Idempotent — safe
        # to call from any tool dispatch (Week-4 R2 #5 — without this
        # the MCP server would have ZERO policies registered and
        # silently allow every edit).
        from mcp_server.engine import register_default_policies

        register_default_policies()
        from mcp_server.engine.wiring.mcp_dispatch import pre_call

        _engine_verdict = pre_call(name, arguments)
        if _engine_verdict.is_blocking():
            # Block: return the verdict's message as the tool result so
            # the AI sees why it was blocked.
            msg = _engine_verdict.message or "Codevira policy blocked this action."
            if _engine_verdict.policy:
                msg = f"[codevira:{_engine_verdict.policy}] {msg}"
            return [TextContent(type="text", text=msg)]
        # warn / inject / allow: log and continue. (Warn/inject surface
        # via logs for now; Hero 6 will surface them in tool output.)
    except Exception:
        pass  # engine wiring must never block tool dispatch

    try:
        if name == "get_node":
            result = get_node(
                arguments["file_path"],
                full=arguments.get("full", False),
            )
        elif name == "get_impact":
            result = get_impact(
                arguments["file_path"],
                limit=arguments.get("limit", 10),
                summary_only=arguments.get("summary_only", False),
            )
        elif name == "get_roadmap":
            result = get_roadmap()
        elif name == "get_full_roadmap":
            result = get_full_roadmap(
                include_decisions=arguments.get("include_decisions", False),
            )
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
                backfill=arguments.get("backfill", False),
                completed_at=arguments.get("completed_at"),
                git_ref=arguments.get("git_ref"),
            )
        elif name == "bulk_import_phases":
            # v2.1.2 Item 29.
            from mcp_server.tools.roadmap import bulk_import_phases

            result = bulk_import_phases(phases=arguments["phases"])
        elif name == "search_codebase":
            result = search_codebase(
                arguments["query"],
                top_k=arguments.get("limit", 5),
                include_content=arguments.get("include_content", False),
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
            result = update_node(
                arguments["file_path"],
                arguments["changes"],
            )
        elif name == "list_nodes":
            result = list_nodes(
                layer=arguments.get("layer"),
                do_not_revert=arguments.get("do_not_revert"),
                stability=arguments.get("stability"),
                limit=arguments.get("limit", 50),
                offset=arguments.get("offset", 0),
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
            )
        elif name == "search_decisions":
            result = search_decisions(
                arguments["query"],
                limit=arguments.get("limit", 5),
                session_id=arguments.get("session_id"),
                full=arguments.get("full", False),
                summary_only=arguments.get("summary_only", False),
                since=arguments.get("since"),  # v2.1.2 Item 25
            )
        elif name == "list_decisions":
            # v2.1.2 Item 11.
            result = list_decisions(
                limit=arguments.get("limit", 20),
                since_date=arguments.get("since_date"),
                file_pattern=arguments.get("file_pattern"),
                protected_only=arguments.get("protected_only", False),
                session_id=arguments.get("session_id"),
                tags=arguments.get("tags"),
                include_superseded=arguments.get("include_superseded", False),
                full=arguments.get("full", False),
            )
        elif name == "list_tags":
            result = list_tags()
        elif name == "record_decisions":
            # v2.1.2 Item 23.
            from mcp_server.tools.learning import record_decisions

            result = record_decisions(decisions=arguments["decisions"])
        elif name == "write_session_logs":
            # v2.1.2 Item 24.
            result = write_session_logs(logs=arguments["logs"])
        elif name == "supersede_decision":
            # v2.1.2 Item 26.
            from mcp_server.tools.learning import supersede_decision

            result = supersede_decision(
                old_id=arguments["old_id"],
                new_decision=arguments["new_decision"],
                reason=arguments["reason"],
                file_path=arguments.get("file_path"),
                context=arguments.get("context"),
                do_not_revert=arguments.get("do_not_revert", False),
                tags=arguments.get("tags"),
            )
        elif name == "check_conflict":
            # v2.1.2 Item 20.
            from mcp_server.tools.check_conflict import check_conflict

            result = check_conflict(
                decision_text=arguments["decision_text"],
                file_path=arguments.get("file_path"),
            )
        elif name == "get_history":
            result = get_history(
                arguments["file_path"],
                limit=arguments.get("limit", 5),
                full=arguments.get("full", False),
                since=arguments.get("since"),  # v2.1.2 Item 25
            )
        elif name == "write_session_log":
            result = write_session_log(
                session_id=arguments["session_id"],
                task=arguments["task"],
                phase=arguments["phase"],
                files_changed=arguments["files_changed"],
                decisions=arguments["decisions"],
                next_steps=arguments["next_steps"],
            )
        elif name == "record_decision":
            from mcp_server.tools.learning import (
                record_decision as learning_record_decision,
            )

            result = learning_record_decision(
                decision=arguments["decision"],
                file_path=arguments.get("file_path"),
                context=arguments.get("context"),
                do_not_revert=arguments.get("do_not_revert", False),
                session_id=arguments.get("session_id"),
            )
        elif name == "mark_decision_protected":
            from mcp_server.tools.learning import (
                mark_decision_protected as learning_mark_decision_protected,
            )

            result = learning_mark_decision_protected(
                decision_id=arguments["decision_id"],
                do_not_revert=arguments["do_not_revert"],
            )
        elif name == "refresh_index":
            result = refresh_index(file_paths=arguments.get("file_paths") or [])
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
        # ---- v1.4: Graph Visualization & Diff ----
        elif name == "export_graph":
            result = export_graph(
                format=arguments.get("format", "mermaid"),
                scope=arguments.get("scope"),
            )
        elif name == "get_graph_diff":
            result = get_graph_diff(
                base_ref=arguments.get("base_ref", "main"),
                head_ref=arguments.get("head_ref", "HEAD"),
            )
        # ---- v1.4: Learning & Adaptive Memory ----
        elif name == "get_decision_confidence":
            result = learning_get_decision_confidence(
                file_path=arguments.get("file_path"),
                pattern=arguments.get("pattern"),
            )
        elif name == "get_preferences":
            result = learning_get_preferences(category=arguments.get("category"))
        elif name == "get_learned_rules":
            result = learning_get_learned_rules(
                file_path=arguments.get("file_path"),
                category=arguments.get("category"),
                include_retired=arguments.get("include_retired", False),
            )
        elif name == "retire_rule":
            from mcp_server.tools.learning import retire_rule as learning_retire_rule

            result = learning_retire_rule(
                rule_id=arguments["rule_id"],
                reason=arguments.get("reason"),
            )
        elif name == "get_project_maturity":
            result = learning_get_project_maturity()
        elif name == "get_session_context":
            # v2.1.2 Item 25: pass through optional since= cutoff.
            result = learning_get_session_context(since=arguments.get("since"))
        # ---- v1.5: Deep Graph Intelligence ----
        elif name == "query_graph":
            result = query_graph_tool(
                file_path=arguments["file_path"],
                symbol=arguments.get("symbol"),
                query_type=arguments.get("query_type", "callees"),
            )
        elif name == "analyze_changes":
            result = analyze_changes_tool(
                base_ref=arguments.get("base_ref", "main"),
                head_ref=arguments.get("head_ref", "HEAD"),
            )
        elif name == "find_hotspots":
            result = find_hotspots_tool(
                threshold=arguments.get("threshold", 50),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as e:
        result = {"error": str(e), "tool": name}
        # Log crashes with full traceback (sanitized, no secrets)
        try:
            from mcp_server.crash_logger import log_crash
            from mcp_server.paths import get_project_root

            log_crash(
                e,
                context="tool dispatch",
                tool_name=name,
                project_path=str(get_project_root()),
            )
        except Exception:
            pass  # crash logger must never break the server

    # v2.0 engine: run post_tool_use hook for telemetry (Hero 6 token
    # accounting, Hero 7 style checks, Hero 10 outcome scoring will
    # all hook here). Failures must not change the response we return
    # to the AI — wiring layer swallows.
    try:
        from mcp_server.engine.wiring.mcp_dispatch import post_call

        post_call(name, arguments, result)
    except Exception:
        pass

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def main():
    import asyncio
    import logging
    import sys

    logger = logging.getLogger("codevira.server")

    # Install global crash handler — catches unhandled exceptions
    try:
        from mcp_server.crash_logger import install_global_handler

        install_global_handler()
    except Exception as e:
        logger.warning("Could not install crash handler: %s", e)

    # v1.8.1: refuse to start the MCP server when launched from $HOME or a
    # system top-level. This is the LAST-mile guard — without it, a user
    # who upgrades from v1.8.0 WITHOUT running `clean --orphans` would still
    # hit the crash mode: their leftover rogue project's config.yaml drives
    # `start_background_watcher` (called below) into walking
    # ~/Library/Group Containers/... — which is where the original 41
    # InterruptedError crashes were logged. cmd_configure / cmd_init /
    # auto_init guards prevent NEW rogues; this guard prevents the
    # existing-rogue path from spinning up the watcher.
    try:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        _early_root = get_project_root()
        _rejection = is_invalid_project_root(_early_root)
        if _rejection:
            print(f"Error: {_rejection}", file=sys.stderr)
            print(
                "  The MCP server cannot start with this project root.\n"
                "  Move into a real project directory and relaunch your IDE,\n"
                "  or run `codevira clean --orphans` to remove leftover\n"
                "  rogue project data from a previous version.",
                file=sys.stderr,
            )
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        # Never let the guard itself crash the server — log and continue.
        logger.warning("Project-root validation failed (continuing): %s", e)

    # v1.6: Auto-migrate legacy .codevira/ → ~/.codevira/projects/<key>/
    try:
        from mcp_server.migrate import detect_migration_needed, migrate_to_centralized
        from mcp_server.paths import get_project_root

        _proj_root = get_project_root()
        if detect_migration_needed(_proj_root):
            logger.info("Migrating legacy .codevira/ to centralized storage...")
            result = migrate_to_centralized(_proj_root)
            if result.get("migrated"):
                logger.info(
                    "Migration complete: %d files moved to %s",
                    result.get("files_copied", 0),
                    result.get("new_path", ""),
                )
    except Exception as e:
        logger.warning("Could not run storage migration: %s", e)

    # Auto-start background file watcher so the index stays fresh
    # on every file save — no manual trigger or git commit needed.
    watcher = None
    try:
        from indexer.index_codebase import start_background_watcher

        watcher = start_background_watcher(quiet=True)
        logger.info("Live file watcher active — index updates on save")
    except Exception as e:
        # Watcher is best-effort; don't block server startup
        logger.warning("Could not start background watcher: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="background watcher startup")

    # v1.4: Run outcome analysis and rule inference on startup
    # This processes any sessions that haven't been analyzed yet.
    try:
        from indexer.outcome_tracker import analyze_session_outcomes
        from indexer.rule_learner import run_rule_inference

        analyze_session_outcomes()
        run_rule_inference()
        logger.info("Outcome analysis and rule inference complete")
    except Exception as e:
        logger.warning("Could not run startup learning: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="startup learning pipeline")

    # v1.5: Import global intelligence from cross-project memory
    try:
        from mcp_server.global_sync import import_global_to_project

        stats = import_global_to_project()
        if stats.get("preferences_imported") or stats.get("rules_imported"):
            logger.info(
                "Global memory: imported %d preferences, %d rules",
                stats["preferences_imported"],
                stats["rules_imported"],
            )
    except Exception as e:
        logger.warning("Could not sync global memory: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="global memory import")

    # v1.7: Enforce logs.retention_days from config (opt-in, default 0 = keep forever)
    try:
        from mcp_server.log_retention import enforce_retention

        enforce_retention()
    except Exception as e:
        logger.warning("Log retention cleanup failed: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="log retention cleanup")

    # v1.7: Pre-warm the embedding model in a background thread so the first
    # search_codebase() call doesn't hit the MCP client's ~30s timeout
    # while waiting for PyTorch init / model download.
    try:
        from mcp_server.tools.search import prewarm_embedding_model

        prewarm_embedding_model()
    except Exception as e:
        logger.warning("Embedding prewarm failed: %s", e)

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    try:
        asyncio.run(_run())
    finally:
        if watcher is not None:
            watcher.stop()


if __name__ == "__main__":
    main()
