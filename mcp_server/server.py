"""
Codevira MCP Server

Exposes the project context graph, roadmap, and code index as MCP tools —
usable by any MCP-compatible AI coding tool.

Tools:
  get_node(file_path)                    → graph node: role, connections, rules
  get_impact(file_path)                  → blast radius before touching a file
  get_roadmap()                          → current phase, next action, recent decisions
  # search_codebase removed in v2.2.0 — agents grep + Read directly
  # changesets removed in v2.2.0 — never reached real usage
  # update_node / add_node / list_nodes removed in v2.2.0 — graph generator owns mutations
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
    refresh_graph,
    query_graph as query_graph_tool,
)
from mcp_server.tools.roadmap import (
    get_roadmap,
    get_phase,
    add_phase,
    update_phase_status,
    defer_phase,
    complete_phase,
    update_next_action,
)
from mcp_server.tools.search import (
    search_decisions,
    get_history,
    write_session_log,
    list_decisions,
    list_tags,  # v2.1.2 Items 11 + 27
)
# v2.2.0+ (2026-05-22 surface-cut audit batch 6) — dropped imports:
#   - get_full_roadmap (rarely needed by agents; `get_roadmap` is the
#     daily driver; agents wanting full history use `get_phase(n)`)
#   - refresh_index (chromadb-era; v2.2.0 has nothing to refresh —
#     `_check_search_deps` returns False always; the code-graph
#     refresh is the separate `refresh_graph` MCP tool)
#   - write_session_logs (batch endpoint that nobody used)

# v2.2.0: search_codebase removed. AI agents grep + read files; semantic
# code search was the source of 90%+ of v2.1.x disk + bug surface.
from mcp_server.tools.playbook import get_playbook
from mcp_server.tools.code_reader import get_signature, get_code
from mcp_server.tools.learning import (
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
        # v2.2.0+: JSONL is the only storage layer. The legacy graph.db
        # fallback was removed once the v2.1.x carryover user base
        # dropped to zero. If `.codevira/` isn't present, build_timeline
        # returns an empty list and the renderer shows the friendly
        # placeholder.
        timeline = build_timeline(query=query, since_days=30, limit=20)
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
                "and upcoming phases. Call at the START of every session."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # v2.2.0: search_codebase tool removed. AI agents grep + read files
        # natively; semantic code search added 90%+ of v2.1.x's disk footprint
        # + bug surface for a feature usage data showed was near-zero.
        #
        # v2.2.0+ (2026-05-22 surface-cut audit batch 6): get_full_roadmap
        # removed. The audit found near-zero use; `get_roadmap` covers the
        # 95% case, and agents wanting one phase's detail use
        # `get_phase(n)` (which already returns key_decisions when present).
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
        # v2.2.0+: update_node, list_nodes deleted (manual graph mutation
        # was never load-bearing; query_graph covers list use case).
        # v2.2.0+: add_node deleted (graph generator owns node creation).
        Tool(
            name="search_decisions",
            description=(
                "Search past decisions across sessions and roadmap phases. "
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
        # v2.2.0+ (2026-05-22 surface-cut audit batch 6): the batch
        # endpoints `record_decisions` and `write_session_logs` were
        # deleted. They saved network round-trips that the audit data
        # showed were not happening in practice (memory-dump sessions
        # weren't using the batch APIs; agents called single endpoints
        # repeatedly anyway). Use ``record_decision`` /
        # ``write_session_log`` directly.
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
                    "old_id": {
                        "type": "string",
                        "description": (
                            "Decision id to retire (e.g. 'D000001'). v3.0.0 "
                            "uses zero-padded string IDs returned by "
                            "record_decision. v2.x integer IDs are not "
                            "accepted — they live in graph.db which v3.0.0 "
                            "no longer reads."
                        ),
                    },
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
                "Returns {decision_id, session_id}. To change the "
                "do_not_revert flag later or update the decision text, "
                "use supersede_decision(old_id, new_decision, reason, "
                "do_not_revert=...) — it preserves the audit trail."
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
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of tag strings (e.g. "
                            '["security", "auth"]). Surfaces in '
                            "list_decisions / list_tags filters."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "If true, skip the implicit `check_conflict` "
                            "duplicate/conflict warning step. Use when "
                            "you've already reviewed a conflict and want "
                            "to record anyway."
                        ),
                    },
                },
                "required": ["decision"],
            },
        ),
        # v2.2.0+ (2026-05-22 surface-cut audit batch 6):
        # mark_decision_protected was deleted. To toggle do_not_revert
        # on an existing decision, use `supersede_decision(old_id,
        # new_decision, reason, do_not_revert=true)` — that's the
        # canonical "I want to update this decision" path and gives
        # you the audit trail (supersession reason) for free. Setting
        # the flag in isolation without a reason was the use case;
        # the audit found 0 such calls in real data.
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
        # v2.2.0+ (2026-05-22 surface-cut audit batch 6):
        # refresh_index was deleted. It was the chromadb-era
        # "reindex the semantic search" tool. v2.2.0 has no semantic
        # search index to refresh; the code-graph refresh lives at
        # `refresh_graph` (still present below) which is what callers
        # actually want.
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
        # v2.2.0+: export_graph, get_graph_diff, get_decision_confidence,
        # get_project_maturity tools deleted per 2026-05-22 surface-cut
        # audit. Vestigial / never-used / dashboard-only surfaces.
        Tool(
            name="get_session_context",
            description=(
                "Single 'catch me up' call for cross-tool continuity. "
                "Returns current roadmap phase, recent decisions with confidence, "
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
        # v2.2.0+: analyze_changes + find_hotspots deleted (vestigial).
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
        "refresh_graph",  # background/automatic
        # v2.2.0+ (2026-05-22 surface-cut audit batch 6): refresh_index
        # and get_full_roadmap deleted; no longer need hiding because
        # they no longer exist.
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
        # v2.2.0+ batch 6: get_full_roadmap dispatch deleted (tool gone).
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
            # v2.2.0: search_codebase removed. If anyone still calls it,
            # return a friendly explanation.
            result = {
                "error": (
                    "search_codebase was removed in v2.2.0. AI agents grep + "
                    "read files natively. Use the standard Read/Grep tools "
                    "instead. For decisions, use search_decisions(query)."
                ),
                "removed_in": "v2.2.0",
            }
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
        # v2.2.0+ batch 6: record_decisions (batch) and write_session_logs
        # (batch) dispatchers deleted along with the tools.
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

            # v2.2.0+ batch 6: the batch endpoint `record_decisions` was
            # deleted, so single-record `record_decision` now needs to
            # forward EVERY field the batch endpoint used to support
            # (tags, force) — otherwise users loop-calling this endpoint
            # silently drop those fields.
            result = learning_record_decision(
                decision=arguments["decision"],
                file_path=arguments.get("file_path"),
                context=arguments.get("context"),
                do_not_revert=arguments.get("do_not_revert", False),
                session_id=arguments.get("session_id"),
                tags=arguments.get("tags"),
                force=arguments.get("force", False),
            )
        # v2.2.0+ batch 6: mark_decision_protected dispatch deleted
        # (use supersede_decision with do_not_revert=True). refresh_index
        # dispatch deleted (use refresh_graph; semantic index is gone).
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
        # v2.2.0+: export_graph, get_graph_diff, get_decision_confidence,
        # get_project_maturity, analyze_changes, find_hotspots dispatchers
        # deleted per surface-cut audit.
        elif name == "get_session_context":
            # v2.1.2 Item 25: pass through optional since= cutoff.
            result = learning_get_session_context(since=arguments.get("since"))
        elif name == "query_graph":
            result = query_graph_tool(
                file_path=arguments["file_path"],
                symbol=arguments.get("symbol"),
                query_type=arguments.get("query_type", "callees"),
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
    #
    # v3.0 escape hatch: CODEVIRA_NO_WATCHER=1 skips the watcher
    # entirely. For users on huge repos, low-power machines, or
    # multi-MCP-instance setups where N codevira processes each
    # register their own fsevents observers and compound CPU usage.
    # The graph still re-reads files on demand; you just don't get
    # automatic incremental reindex on save.
    import os as _os

    watcher = None
    if _os.environ.get("CODEVIRA_NO_WATCHER", "0") == "1":
        logger.info(
            "Background watcher disabled via CODEVIRA_NO_WATCHER=1 — "
            "graph will not auto-refresh on file save"
        )
    else:
        try:
            from indexer.index_codebase import start_background_watcher

            watcher = start_background_watcher(quiet=True)
            logger.info("Live file watcher active — index updates on save")
        except Exception as e:
            # Watcher is best-effort; don't block server startup
            logger.warning("Could not start background watcher: %s", e)
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(e, context="background watcher startup")

    # v1.4: Run outcome analysis on startup. This processes any sessions
    # that haven't been analyzed yet so AntiRegression + decision-
    # confidence have fresh data.
    #
    # v3.0.0 audit cleanup: the companion ``run_rule_inference()`` call
    # was removed. The rule-learner module was deleted in the
    # 2026-05-22 surface-cut audit because the MCP tools that consumed
    # its output (get_learned_rules, retire_rule) were also deleted.
    #
    # v3.0 perf: moved to a daemon thread so the MCP `initialize`
    # handshake returns immediately. Synchronous startup blocked for
    # seconds when many sessions had unanalyzed decisions — git
    # subprocess fanout in _analyze_single_session with no overall
    # timeout. Surfaced via "looks hanged on first tool call" during
    # Claude Desktop dogfood (2026-05-23).
    def _run_startup_outcome_analysis() -> None:
        try:
            from indexer.outcome_tracker import analyze_session_outcomes

            analyze_session_outcomes()
            logger.info("Outcome analysis complete (background)")
        except Exception as e:
            logger.warning("Could not run startup outcome analysis: %s", e)
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(e, context="startup outcome analysis")

    import threading

    threading.Thread(
        target=_run_startup_outcome_analysis,
        name="codevira-startup-outcome-analysis",
        daemon=True,
    ).start()

    # v1.5 → v3.0.0: register this project in the cross-machine inventory
    # so `codevira projects` can enumerate it. Best-effort; never breaks
    # startup. v3.0.0 simplified the previous "import global intelligence"
    # path — preferences + rules sync was deleted in the audit; project
    # registration is the one piece of cross-project state that survives.
    try:
        from mcp_server.global_sync import register_current_project

        register_current_project()
    except Exception as e:
        logger.warning("Could not register project in global inventory: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="cross-project registration")

    # v1.7: Enforce logs.retention_days from config (opt-in, default 0 = keep forever)
    try:
        from mcp_server.log_retention import enforce_retention

        enforce_retention()
    except Exception as e:
        logger.warning("Log retention cleanup failed: %s", e)
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="log retention cleanup")

    # 2026-05-19 v2.1.2 issue #10: prewarm REMOVED at startup.
    #
    # History: v1.7 added a background daemon thread that pre-loaded the
    # ChromaDB embedding model so the first search_codebase() call didn't
    # block on PyTorch init. v2.1.2 removed this because:
    #
    #   1. Antigravity (Cascade) runs MCP-server children under sandbox
    #      entitlements that block dlopen() of unsigned PyPI dylibs like
    #      torch/lib/libtorch_global_deps.dylib. The prewarm daemon thread
    #      fails during MCP server startup; Antigravity reports it as
    #      "tools/list failed: dlopen ... no such file" even though the
    #      file exists. Issue #10.
    #
    #   2. The MCP server itself + every non-search tool (graph, roadmap,
    #      changesets, decisions read/write, etc.) work fine WITHOUT
    #      torch. Loading torch at startup forced the whole server to
    #      either succeed-on-torch or be partly broken.
    #
    #   3. The existing lazy-load in search.py:_get_chroma_client()
    #      already handles "first call is slow" via the "warming" status
    #      response — the MCP client retries on the next invocation.
    #
    # Net effect: server starts instantly everywhere. First semantic
    # search call pays the ~1-3s PyTorch init cost (sometimes returning
    # status="warming" if the MCP client's per-call timeout is short).
    # Non-search tools unaffected.

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
