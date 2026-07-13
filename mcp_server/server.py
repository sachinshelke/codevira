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

    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # older mcp (<1.3): tool annotations are skipped
        ToolAnnotations = None  # type: ignore[assignment,misc]
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
    expand,  # E1 (Phase 19): summary-first expand path
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


@server.list_resource_templates()
async def handle_list_resource_templates():
    """Declare the parameterized decisions resource so MCP clients can
    *discover* that the timeline is query-able. ``read_resource`` already
    serves ``codevira://decisions/{query}`` (substring filter), but
    ``resources/list`` only advertises the static URI — without a template,
    a client has no way to learn it can append a query. SDK-supported since
    the resource-templates capability landed."""
    from mcp.types import ResourceTemplate

    return [
        ResourceTemplate(
            uriTemplate="codevira://decisions/{query}",
            name="Codevira decisions — filtered",
            description=(
                "The decision timeline filtered to decisions matching {query} "
                "(URL-encoded substring). Example: codevira://decisions/retry."
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
    from mcp.server.lowlevel.helper_types import ReadResourceContents

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
        html_doc = render_html(timeline, title=title)
    except Exception as e:  # noqa: BLE001
        # Bug-X-shape defense: never let resource-read crash the MCP
        # client. Return an HTML page with the error so the user knows.
        import html as _html

        html_doc = (
            f"<!DOCTYPE html><html><body>"
            f"<h1>{_html.escape(title)}</h1>"
            f"<p style='color:red'>Codevira couldn't load decisions: "
            f"{_html.escape(str(e))}</p></body></html>"
        )

    # Return ReadResourceContents (the modern SDK API). Returning a bare str
    # is deprecated AND defaults the content mimeType to text/plain, so the
    # text/html declared in list_resources never reached the client renderer
    # (Claude Desktop showed escaped HTML instead of the timeline). This fixes
    # the DeprecationWarning and the declared-vs-served mimeType mismatch.
    return [ReadResourceContents(content=html_doc, mime_type="text/html")]


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
                "Keyword search over an FTS5/BM25 index (Porter-stemmed) — "
                "NO semantic/vector matching, so recall depends on sharing "
                "keywords with the stored decision; for a concept with no "
                "shared words, browse list_decisions or list_tags instead. "
                "Default (E1): summary-first rows — {id, decision (one-line ≤140), "
                "file_path, do_not_revert, tags, score}, dropping per-row "
                "snippet/origin. Pass full=true for untruncated rows, "
                "expand(ids=[...]) to fetch specific decisions in full, or "
                "summary_only=true for a ~70%-smaller {id, summary, score} "
                "payload. Answers 'has anyone decided this before?'"
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
                    "all_projects": {
                        "type": "boolean",
                        "description": (
                            "v3.6.0: search EVERY registered project's decisions, "
                            "not just the current one. Each result gains `project` "
                            "+ `project_path`. Use to recall how you solved "
                            "something in another repo. Default false."
                        ),
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
                "list what it remembers.' Default (E1): compact rows — one-line "
                "decision summary + key fields; full=true (or "
                "CODEVIRA_DECISION_DETAIL=full) for untruncated records, "
                "expand(ids=[...]) to fetch specific decisions in full."
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
                    "summary_only": {
                        "type": "boolean",
                        "description": (
                            "Smallest payload — only {id, summary, "
                            "do_not_revert} per row (parity with "
                            "search_decisions). Takes precedence over full."
                        ),
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
            name="expand",
            description=(
                "E1 (Phase 19): fetch FULL decision records by ID — the expand "
                "path for the summary-first search_decisions / list_decisions "
                "defaults. Scan the cheap compact rows, then pass the IDs you "
                "care about here for complete text + context + origin. "
                "Returns {requested, count, decisions, not_found}; never raises "
                "on unknown IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Decision IDs to fetch in full (e.g. ['D0000Z4']).",
                    },
                },
                "required": ["ids"],
            },
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
            name="set_decision_flag",
            description=(
                "v3.0.0 lightweight flag/tag update for an existing decision. "
                "Use this when you only need to toggle do_not_revert or "
                "correct a tag list — avoids supersede_decision's mandatory "
                "rewrite of the decision text + reason. Writes a single "
                "amendment record to .codevira/decisions.jsonl. For "
                "semantic rewrites use supersede_decision instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision_id": {
                        "type": "string",
                        "description": "Decision id to amend (e.g. 'D000007')",
                    },
                    "do_not_revert": {
                        "type": "boolean",
                        "description": "New flag value (omit to leave unchanged)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (omit to leave unchanged)",
                    },
                    "is_outdated": {
                        "type": "boolean",
                        "description": (
                            "v3.7.0: set/clear the outdated tombstone "
                            "(omit to leave unchanged; False un-retires a "
                            "decision marked via mark_decision_outdated)"
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Required to set is_outdated=true on a "
                            "do_not_revert (protected) decision"
                        ),
                    },
                },
                "required": ["decision_id"],
            },
        ),
        Tool(
            name="mark_decision_outdated",
            description=(
                "v3.7.0 staleness read-side: tombstone a decision as "
                "OUTDATED so it stops surfacing in get_session_context / "
                "search_decisions / list_decisions — without deleting it. "
                "Use when a decision is simply no longer true and has NO "
                "successor (for a replacement, use supersede_decision to "
                "preserve lineage). Reversible via "
                "set_decision_flag(is_outdated=false). Writes one amendment "
                "to .codevira/decisions.jsonl; audit preserved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision_id": {
                        "type": "string",
                        "description": "Decision id to retire (e.g. 'D000007')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional short note on why it's outdated",
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Required to retire a do_not_revert (protected) "
                            "decision — surface its reasoning to the user first"
                        ),
                    },
                },
                "required": ["decision_id"],
            },
        ),
        Tool(
            name="reaffirm_decision",
            description=(
                "v3.2.0: refresh a do_not_revert decision's soft-expire "
                "clock. Long-lived locked decisions can grow stale; "
                "v3.2.0 surfaces a 'dnr_soft_expired' flag on search/"
                "list output (default 180 days, override via "
                "CODEVIRA_DNR_SOFT_EXPIRE_DAYS). Call this on a "
                "still-load-bearing soft-expired decision to reset the "
                "clock — appends a single 'reaffirmed_at' amendment to "
                ".codevira/decisions.jsonl. For semantic rewrites use "
                "supersede_decision; for flipping the flag use "
                "set_decision_flag."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decision_id": {
                        "type": "string",
                        "description": "Decision id to reaffirm (e.g. 'D000007')",
                    },
                },
                "required": ["decision_id"],
            },
        ),
        Tool(
            name="check_conflict",
            description=(
                "Check whether a proposed decision contradicts any "
                "do_not_revert=True decision OR duplicates an existing one. "
                "Returns {status: novel|duplicate|conflict, conflicts, "
                "duplicates}. Call BEFORE record_decision to surface conflicts "
                "proactively (record_decision also runs this internally and "
                "surfaces _conflict_warning unless force=true)."
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
                "Record one architectural decision. Set do_not_revert=true to "
                "lock it across sessions and IDEs. Returns {decision_id, "
                "session_id}. To change it later use supersede_decision "
                "(preserves the audit trail) or set_decision_flag (toggle "
                "do_not_revert / tags)."
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
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Optional function/class name within file_path to "
                            'scope the decision to (e.g. "login"). With '
                            "do_not_revert, the lock then blocks only edits "
                            "INSIDE that symbol; edits elsewhere in the file "
                            "warn instead. Requires file_path."
                        ),
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
        # ---- v3.1.0 M2: working memory (intra-session scratchpad) ----
        Tool(
            name="working_add",
            description=(
                "v3.1.0 M2: Append one observation or goal to working memory "
                "(intra-session, bounded, decay-scored scratchpad in "
                ".codevira-cache/working.jsonl). 'observation' = a fact the agent saw "
                "(file edited, error message, command output). 'goal' = what the agent "
                "is currently trying to accomplish. Use working_promote to move an entry "
                "to long-term memory (decision/skill/playbook) when it earns its keep."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Free-text markdown (max 2 KB)",
                    },
                    "kind": {
                        "type": "string",
                        "description": "observation | goal (default: observation)",
                        "enum": ["observation", "goal"],
                        "default": "observation",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "1-10 (default 5). Errors = 7, decisions = 8+",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0.0-1.0, optional. Voyager-style belief strength",
                    },
                    "links": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional D-ids / S-ids this entry references",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session slug; defaults to ad-hoc-XXXXXX",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="working_get",
            description=(
                "v3.1.0 M2: Top-K live working-memory entries by decay score "
                "(importance × exp(-Δt_hours / 6) + 0.5 × access_count). Filters by "
                "kind / session_id. Tombstoned (evicted or promoted) entries are excluded."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {
                        "type": "integer",
                        "description": "Max entries to return (default 10)",
                        "default": 10,
                    },
                    "kind": {
                        "type": "string",
                        "description": "Filter to observation | goal (default: both)",
                        "enum": ["observation", "goal"],
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Filter to one session slug",
                    },
                },
            },
        ),
        Tool(
            name="working_promote",
            description=(
                "v3.1.0 M2: Promote a working-memory entry to long-term memory "
                "and tombstone the source. to='decision' is the fully wired path "
                "(calls check_conflict first; force=true overrides). to='skill' and "
                "to='playbook' are reserved for M3+; the call returns "
                "{deferred: true, milestone: ...} until those stores ship."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "The W-id from working_add / working_get",
                    },
                    "to": {
                        "type": "string",
                        "description": "Target LTM store",
                        "enum": ["decision", "skill", "playbook"],
                        "default": "decision",
                    },
                    "file_path": {"type": "string"},
                    "context": {"type": "string"},
                    "do_not_revert": {"type": "boolean", "default": False},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "force": {
                        "type": "boolean",
                        "description": "Skip check_conflict warning (e.g., on second-pass promote)",
                        "default": False,
                    },
                },
                "required": ["entry_id"],
            },
        ),
        Tool(
            name="get_working_context",
            description=(
                "v3.1.0 M2: Compact markdown rendering of the top working-memory "
                "entries for ReAct-loop injection. Returns {markdown, entries, count}. "
                "Capped at ~150 tokens of output (entries truncated at 120 chars each)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {
                        "type": "integer",
                        "description": "Max entries to include (default 5)",
                        "default": 5,
                    },
                },
            },
        ),
        # ---- v3.1.0 M3: skill library (procedural memory) ----
        Tool(
            name="record_skill",
            description=(
                "v3.1.0 M3: Author a new skill in the canonical store "
                "(.codevira/skills.jsonl). Skills encode 'how to do X in "
                "this project' as markdown procedures. Calls check_conflict "
                "against the SKILLS corpus before writing; near-duplicate "
                "warnings can be overridden via force=True. Use supersede_skill "
                "to version an existing skill, or promote_skill_to_playbook to "
                "promote a skill into the existing playbook system."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short identifier (e.g., 'git-rebase-workflow')",
                    },
                    "procedure": {
                        "type": "string",
                        "description": "Markdown body of how to do this thing (max 2 KB)",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional one-liner (max 256 B)",
                    },
                    "triggers": {
                        "type": "object",
                        "properties": {
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "file_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "description": (
                            "Discovery hints: tags (lowercased, set-membership "
                            "for jaccard ranking) + file_patterns (fnmatch globs "
                            "for file-scoped retrieval)"
                        ),
                    },
                    "source": {
                        "type": "string",
                        "enum": ["explicit", "induced"],
                        "default": "explicit",
                    },
                    "do_not_revert": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Exempt from auto-archive sweep; flag canonical doctrine."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip duplicate-check warning",
                    },
                },
                "required": ["name", "procedure"],
            },
        ),
        Tool(
            name="get_skill",
            description=(
                "v3.1.0 M3: Composite-ranked search over active skills. "
                "score = 0.5 × BM25_norm + 0.3 × tag_jaccard + 0.2 × recency_decay "
                "(τ=30d, never-used skills score 0 recency). Returns hits with "
                "score_breakdown for debuggability. Pass file_path to filter "
                "skills whose trigger file_patterns don't match."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords (e.g., 'rebase main')",
                    },
                    "top_k": {"type": "integer", "default": 5},
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Optional file path to filter skills by their "
                            "trigger file_patterns (fnmatch). Skills with no "
                            "patterns match anything (not filtered)."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="apply_skill_outcome",
            description=(
                "v3.1.0 M3: Manually record one outcome for a skill — success "
                "or failure. Reinforces the reinforcement loop (resets "
                "consecutive_failures on success; auto-archives at 5 consecutive "
                "failures unless do_not_revert=True). The canonical signal in "
                "M5+ comes from outcomes_writer.py (git-derived, not "
                "agent-self-reported); this tool is the manual override."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "success": {"type": "boolean"},
                },
                "required": ["skill_id", "success"],
            },
        ),
        Tool(
            name="list_skills",
            description=(
                "v3.1.0 M3: Filtered list of skills. status='active' (default) "
                "returns the daily-driver set; 'all' returns every state; any "
                "other value filters to that one state. tags filter is set "
                "intersection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "active | archived | superseded | all",
                        "default": "active",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["explicit", "induced"],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="supersede_skill",
            description=(
                "v3.1.0 M3: Version a skill. Writes a new skill that supersedes "
                "old_id; amendment-marks the old as 'superseded' with a backref. "
                "Triggers inherit from the old skill when not supplied. The old "
                "skill no longer surfaces in search after this; it's still "
                "retrievable via list_skills(status='superseded') for audit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "old_id": {"type": "string"},
                    "name": {"type": "string"},
                    "procedure": {"type": "string"},
                    "summary": {"type": "string"},
                    "triggers": {
                        "type": "object",
                        "properties": {
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "file_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                    "reason": {"type": "string"},
                    "do_not_revert": {"type": "boolean", "default": False},
                },
                "required": ["old_id", "name", "procedure"],
            },
        ),
        Tool(
            name="promote_skill_to_playbook",
            description=(
                "v3.1.0 M3: Write the skill's procedure as a playbook markdown "
                "file at .codevira/playbooks/<task_type>/<name>.md. Refuses on "
                "existing file unless force=True so hand-written playbooks "
                "aren't clobbered. After promotion the procedure is also "
                "discoverable via get_playbook(task_type)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        "description": (
                            "Playbook directory name (e.g., 'commit', "
                            "'add_tool', 'debug_pipeline')"
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Optional filename slug; defaults to slugified(skill.name)"
                        ),
                    },
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["skill_id", "task_type"],
            },
        ),
        # ---- v3.1.0 M4: spatial memory ----
        Tool(
            name="spatial_nearby",
            description=(
                "v3.1.0 M4: Files topologically near a given file, ranked by "
                "recent activity. Candidate set = BFS distance ≤ 2 over the "
                "indexer graph (imports + call edges) ∪ same-neighborhood "
                "files. Ranking: (1 / (1 + bfs_dist)) × log(1 + visit_count_30d). "
                "Falls back to neighborhood-only if the indexer graph isn't built."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Project-relative file path",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Max neighbors to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="spatial_heat",
            description=(
                "v3.1.0 M4: Top-K most-touched files in a time window by "
                "weighted activity (edits + decision_refs). Useful for "
                "'where has attention been this week?' queries. Pass "
                "since_days to limit the window; omit for all-time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "default": 20},
                    "since_days": {
                        "type": "integer",
                        "description": (
                            "Only count activity within the trailing N days "
                            "(omit for all-time)"
                        ),
                    },
                },
            },
        ),
        Tool(
            name="spatial_neighborhood",
            description=(
                "v3.1.0 M4: Return the neighborhood id + members for a file. "
                "Folder-tree default (top-2 dir components, e.g., "
                "'mcp_server/storage'); overridable via "
                ".codevira/neighborhoods.yaml."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="spatial_affordances",
            description=(
                "v3.1.0 M4: Return the affordance keys (task_types) applicable "
                "to a file based on the bundled + project affordances.yaml. "
                "E.g., a file under mcp_server/tools/ typically affords "
                "{add_tool, write_test}. Use the returned keys with "
                "get_playbook(task_type) for relevant rules."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                },
                "required": ["file_path"],
            },
        ),
        # ---- v3.1.0 M6 Phase B: consensus (read-only) ----
        Tool(
            name="consensus_check",
            description=(
                "v3.1.0 M6 Phase B: Scan decisions written since this IDE's "
                "checkpoint, surface cross-IDE conflicts to "
                ".codevira/pending_conflicts.jsonl, advance the checkpoint. "
                "Read-only — no automatic resolution. The Phase C handshake "
                "protocol (one IDE proposing supersession to another) is "
                "M7 and ships disabled by default."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="consensus_status",
            description=(
                "v3.1.0 M6: Return the count of pending cross-IDE conflicts + "
                "top-K rows (default 3). Useful as a status check from inside "
                "the agent loop; the get_session_context payload also "
                "carries a 'consensus' panel based on this data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "default": 3},
                },
            },
        ),
        Tool(
            name="consensus_propose_supersession",
            description=(
                "v3.1.0 M7 Phase C: Open a cross-IDE supersession proposal. "
                "Writes a 'proposed_supersession' row to pending_conflicts.jsonl "
                "with expires_at = ts + handshake_timeout_days (default 14). "
                "Opt-in: returns {disabled: True} unless "
                "memory.consensus.handshake_enabled is set in "
                ".codevira/config.yaml. Same-author fast-path returns "
                "{fast_path: True} so the caller can route to "
                "supersede_decision directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_decision_id": {"type": "string"},
                    "new_decision": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["target_decision_id", "new_decision", "reason"],
            },
        ),
        Tool(
            name="consensus_resolve",
            description=(
                "v3.1.0 M7 Phase C: Approve, reject, or withdraw a pending "
                "supersession proposal. Opt-in via "
                "memory.consensus.handshake_enabled. The approving IDE should "
                "match the target decision's origin IDE (or be 'unknown') "
                "for cross-IDE proposals; withdrawals come from the "
                "proposing IDE."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["approved", "rejected", "withdrawn"],
                    },
                    "comment": {"type": "string"},
                },
                "required": ["proposal_id", "action"],
            },
        ),
        Tool(
            name="origin_of",
            description=(
                "v3.1.0 M7: Return the M1 origin block attached to a decision "
                "({ide, agent_model, host_hash, ts}) + protection / supersession "
                "metadata. Always available regardless of the handshake flag."
            ),
            inputSchema={
                "type": "object",
                "properties": {"decision_id": {"type": "string"}},
                "required": ["decision_id"],
            },
        ),
        # ---- v3.3.0 Phase 4: preference capture (D0000LU) ----
        Tool(
            name="distill_preferences",
            description=(
                "v3.3.0: Distill captured user prompts into durable "
                "preferences (communication style, workflow habits) via the "
                "host LLM (sampling/createMessage). Call at SESSION END when "
                "the Stop-hook nudge fires, with dry_run=false to persist "
                "into cross-project memory (~/.codevira/global.db) and clear "
                "the capture file. Degrades to {rendered_prompt} when the "
                "host doesn't support sampling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="search_preferences",
            description=(
                "v3.3.0: Search learned user preferences (cross-project, "
                "LLM-distilled). Filter by category: 'communication', "
                "'workflow', 'formatting'. Use before adopting a tone or "
                "workflow the user may have expressed opinions about. "
                "Highest-frequency first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "top_k": {"type": "integer", "default": 10},
                },
            },
        ),
        # ---- v3.1.0 M8: reflections (episodic abstraction) ----
        Tool(
            name="reflect",
            description=(
                "v3.1.0 M8: Build the source context + rendered prompt for an "
                "LLM abstraction over recent decisions + sessions. v3.1.0 "
                "returns {sampling_supported: False, rendered_prompt, "
                "source_context} so callers can feed the prompt to a locally-"
                "available LLM. The MCP sampling/createMessage RPC integration "
                "is the v3.2 deliverable; until then, use `codevira reflect "
                "--from-file` to commit an LLM-supplied abstraction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "period_days": {"type": "integer", "default": 7},
                    "dry_run": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="get_reflections",
            description=("v3.1.0 M8: Top-K most recent reflections (newest first)."),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "default": 5},
                },
            },
        ),
        Tool(
            name="list_reflections",
            description=(
                "v3.1.0 M8: Filtered reflection list. 'since' is an ISO 8601 "
                "timestamp cutoff; 'tags' is set intersection (every requested "
                "tag must appear)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
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

    # v3.0.0 (D000018): opt-in lean tool surface. The default advertises
    # every tool; CODEVIRA_TOOL_PROFILE=lean trims the ~4.1K-token
    # tools/list to the daily-driver set. Hidden tools still work when
    # called explicitly via call_tool — they're just not advertised.
    import os

    if os.environ.get("CODEVIRA_TOOL_PROFILE", "").strip().lower() == "lean":
        _lean_surface = {
            "get_session_context",
            "get_impact",
            "get_node",
            "get_roadmap",
            "search_decisions",
            "list_decisions",
            "expand",  # E1 (Phase 19): essential complement to summary-first search
            "record_decision",
            "update_phase_status",
            "complete_phase",
            "update_next_action",
            "write_session_log",
        }
        tools = [t for t in tools if t.name in _lean_surface]

    # MCP tool annotations (spec 2025-03-26): tell the client which tools are
    # safe reads vs state mutations, so a host can run read tools without a
    # confirmation prompt and reason about side effects. Codevira's reads
    # (search / get / list / query / spatial / status) never mutate; everything
    # else appends to the JSONL stores. Nothing here is destructive — the
    # destructive ops (reset / uninstall) are CLI-only, not MCP tools.
    _READ_ONLY = {
        "get_session_context", "get_roadmap", "get_phase", "get_playbook",
        "search_decisions", "list_decisions", "expand", "get_history",
        "list_tags", "check_conflict", "get_node", "get_impact", "get_code",
        "get_signature", "query_graph", "get_reflections", "list_reflections",
        "get_skill", "list_skills", "get_working_context", "working_get",
        "spatial_nearby", "spatial_heat", "spatial_neighborhood",
        "spatial_affordances", "consensus_status", "origin_of",
        "search_preferences",
    }  # fmt: skip
    if ToolAnnotations is not None:
        for t in tools:
            if t.annotations is None:
                _is_read = t.name in _READ_ONLY
                t.annotations = ToolAnnotations(
                    readOnlyHint=_is_read,
                    destructiveHint=False,
                    # Reads are idempotent (repeat call → same result); writes
                    # vary (record_decision appends), so leave theirs unset.
                    idempotentHint=True if _is_read else None,
                )

    return tools


# Run the client-roots project binding once per process (the first tool
# call). Subsequent calls skip it — roots don't change within a session.
_roots_bind_attempted = False
# Set True by http_server before serving. Roots-binding pins ONE
# process-global project — correct for stdio (one server per workspace),
# but would cross-contaminate the shared HTTP server (one process, many
# requests/sessions), so it is skipped under HTTP (H1).
_is_http_transport = False


async def _bind_project_from_client_roots() -> None:
    """Bind to the active project from the MCP client's workspace roots.

    Fixes the user-scope-server misbinding: a shared codevira server with
    no ``cwd`` / ``--project-dir`` / ``CODEVIRA_PROJECT_DIR`` would resolve
    to whatever directory the process inherited (the wrong project). When
    no explicit pin is set, we ask the client for its workspace roots and
    choose a binding via :func:`project_binding.choose_binding` (re-binds
    to an established project, or to a fresh ``.git`` workspace root when
    cwd isn't already a codevira project — so a brand-new project doesn't
    auto-init in the wrong inherited cwd). Runs once, best-effort, never
    raises, never blocks dispatch (bounded by a timeout). Skipped under
    HTTP transport (shared process — see ``_is_http_transport``).
    """
    global _roots_bind_attempted
    if _roots_bind_attempted:
        return
    _roots_bind_attempted = True

    if _is_http_transport:
        return

    import os

    from mcp_server import paths

    # Respect an explicit pin — never override --project-dir / the env var.
    if paths._project_dir_override is not None or os.environ.get(
        "CODEVIRA_PROJECT_DIR"
    ):
        return
    try:
        from mcp_server.project_binding import (
            choose_binding,
            resolve_project_root_from_roots,
        )

        session = server.request_context.session
        workspace = await resolve_project_root_from_roots(session)
        cwd_root = paths.get_project_root()
        target = choose_binding(workspace, cwd_root)
        if target is not None and target != cwd_root:
            paths.set_project_dir(target)
            paths.invalidate_data_dir_cache()
    except Exception:
        pass  # best-effort: fall back to cwd discovery


def _maybe_bind_from_tool_path(arguments: dict) -> None:
    """Per-call project resolution for GLOBAL clients (Claude Desktop).

    Claude Desktop is one shared, project-less server — it gives no
    per-conversation workspace signal, so the ``file_path`` in a tool call
    is the only project signal available. Resolve the enclosing codevira
    project from it and switch the active binding (``set_project_dir``
    invalidates the per-root data-dir cache, so subsequent reads come from
    the right project's ``.codevira/``). Sticky: a path-less follow-up tool
    keeps the last resolved project. Gated to ``CODEVIRA_IDE=claude_desktop``
    so strictly workspace-bound IDEs (Claude Code / Cursor / Windsurf) are
    untouched. Best-effort, never raises, never blocks dispatch.
    """
    import os

    if os.environ.get("CODEVIRA_IDE") != "claude_desktop":
        return
    try:
        from mcp_server import paths
        from mcp_server.project_binding import resolve_project_from_file_path

        fp = arguments.get("file_path") or arguments.get("path")
        target = resolve_project_from_file_path(fp)
        if target is not None and target != paths.get_project_root():
            paths.set_project_dir(target)
    except Exception:
        pass


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Bind to the correct project from the client's workspace roots before
    # any path resolution happens (fixes user-scope-server misbinding).
    await _bind_project_from_client_roots()
    # Global clients (Claude Desktop) have no workspace signal — let the
    # tool call's file_path drive which project's memory we use.
    _maybe_bind_from_tool_path(arguments)

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
                all_projects=arguments.get("all_projects", False),  # v3.6.0
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
                summary_only=arguments.get("summary_only", False),
            )
        elif name == "list_tags":
            result = list_tags()
        elif name == "expand":
            # E1 (Phase 19): expand path for the summary-first defaults.
            result = expand(arguments.get("ids") or [])
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
        elif name == "set_decision_flag":
            # v3.0.0 (2026-05-23 RC-audit follow-up): lightweight in-place
            # toggle for do_not_revert / tags. Less surgery than supersede.
            from mcp_server.tools.learning import set_decision_flag

            result = set_decision_flag(
                decision_id=arguments["decision_id"],
                do_not_revert=arguments.get("do_not_revert"),
                tags=arguments.get("tags"),
                is_outdated=arguments.get("is_outdated"),
                force=arguments.get("force", False),
            )
        elif name == "mark_decision_outdated":
            # v3.7.0 staleness read-side: tombstone a stale decision so it
            # stops surfacing (reversible via set_decision_flag).
            from mcp_server.tools.learning import mark_decision_outdated

            result = mark_decision_outdated(
                decision_id=arguments["decision_id"],
                reason=arguments.get("reason"),
                force=arguments.get("force", False),
            )
        elif name == "reaffirm_decision":
            # v3.2.0 do_not_revert soft-expire: append a reaffirmed_at
            # amendment so the decision's age clock resets. See
            # decisions_store.compute_dnr_soft_expire for the threshold.
            from mcp_server.tools.learning import reaffirm_decision

            result = reaffirm_decision(
                decision_id=arguments["decision_id"],
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
                symbol=arguments.get("symbol"),
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
        # ---- v3.1.0 M2: working memory dispatch ----
        elif name == "working_add":
            from mcp_server.tools.working import working_add

            result = working_add(
                content=arguments["content"],
                kind=arguments.get("kind", "observation"),
                importance=arguments.get("importance", 5),
                confidence=arguments.get("confidence"),
                links=arguments.get("links"),
                session_id=arguments.get("session_id"),
            )
        elif name == "working_get":
            from mcp_server.tools.working import working_get

            result = working_get(
                top_k=arguments.get("top_k", 10),
                kind=arguments.get("kind"),
                session_id=arguments.get("session_id"),
            )
        elif name == "working_promote":
            from mcp_server.tools.working import working_promote

            result = working_promote(
                entry_id=arguments["entry_id"],
                to=arguments.get("to", "decision"),
                file_path=arguments.get("file_path"),
                context=arguments.get("context"),
                do_not_revert=arguments.get("do_not_revert", False),
                tags=arguments.get("tags"),
                force=arguments.get("force", False),
            )
        elif name == "get_working_context":
            from mcp_server.tools.working import get_working_context

            result = get_working_context(top_k=arguments.get("top_k", 5))
        # ---- v3.1.0 M3: skill library dispatch ----
        elif name == "record_skill":
            from mcp_server.tools.skills import record_skill

            result = record_skill(
                name=arguments["name"],
                procedure=arguments["procedure"],
                summary=arguments.get("summary"),
                triggers=arguments.get("triggers"),
                source=arguments.get("source", "explicit"),
                do_not_revert=arguments.get("do_not_revert", False),
                force=arguments.get("force", False),
            )
        elif name == "get_skill":
            from mcp_server.tools.skills import get_skill

            result = get_skill(
                query=arguments["query"],
                top_k=arguments.get("top_k", 5),
                file_path=arguments.get("file_path"),
            )
        elif name == "apply_skill_outcome":
            from mcp_server.tools.skills import apply_skill_outcome

            result = apply_skill_outcome(
                skill_id=arguments["skill_id"],
                success=arguments["success"],
            )
        elif name == "list_skills":
            from mcp_server.tools.skills import list_skills

            result = list_skills(
                status=arguments.get("status", "active"),
                source=arguments.get("source"),
                tags=arguments.get("tags"),
                limit=arguments.get("limit", 50),
            )
        elif name == "supersede_skill":
            from mcp_server.tools.skills import supersede_skill

            result = supersede_skill(
                old_id=arguments["old_id"],
                name=arguments["name"],
                procedure=arguments["procedure"],
                summary=arguments.get("summary"),
                triggers=arguments.get("triggers"),
                reason=arguments.get("reason", ""),
                do_not_revert=arguments.get("do_not_revert", False),
            )
        elif name == "promote_skill_to_playbook":
            from mcp_server.tools.skills import promote_skill_to_playbook

            result = promote_skill_to_playbook(
                skill_id=arguments["skill_id"],
                task_type=arguments["task_type"],
                name=arguments.get("name"),
                force=arguments.get("force", False),
            )
        # ---- v3.1.0 M4: spatial memory dispatch ----
        elif name == "spatial_nearby":
            from mcp_server.tools.spatial import spatial_nearby

            result = spatial_nearby(
                file_path=arguments["file_path"],
                k=arguments.get("k", 5),
            )
        elif name == "spatial_heat":
            from mcp_server.tools.spatial import spatial_heat

            result = spatial_heat(
                top_k=arguments.get("top_k", 20),
                since_days=arguments.get("since_days"),
            )
        elif name == "spatial_neighborhood":
            from mcp_server.tools.spatial import spatial_neighborhood

            result = spatial_neighborhood(file_path=arguments["file_path"])
        elif name == "spatial_affordances":
            from mcp_server.tools.spatial import spatial_affordances

            result = spatial_affordances(file_path=arguments["file_path"])
        # ---- v3.1.0 M6 Phase B: consensus dispatch ----
        elif name == "consensus_check":
            from mcp_server.tools.consensus import consensus_check

            result = consensus_check()
        elif name == "consensus_status":
            from mcp_server.tools.consensus import consensus_status

            result = consensus_status(top_k=arguments.get("top_k", 3))
        # ---- v3.1.0 M7 Phase C: handshake dispatch ----
        elif name == "consensus_propose_supersession":
            from mcp_server.tools.consensus import consensus_propose_supersession

            result = consensus_propose_supersession(
                target_decision_id=arguments["target_decision_id"],
                new_decision=arguments["new_decision"],
                reason=arguments["reason"],
            )
        elif name == "consensus_resolve":
            from mcp_server.tools.consensus import consensus_resolve

            result = consensus_resolve(
                proposal_id=arguments["proposal_id"],
                action=arguments["action"],
                comment=arguments.get("comment"),
            )
        elif name == "origin_of":
            from mcp_server.tools.consensus import origin_of

            result = origin_of(decision_id=arguments["decision_id"])
        # ---- v3.3.0 Phase 4: preference capture dispatch ----
        elif name == "distill_preferences":
            from mcp_server.tools.preferences import distill_preferences_async

            mcp_session = None
            try:
                mcp_session = server.request_context.session
            except LookupError:
                pass

            result = await distill_preferences_async(
                dry_run=arguments.get("dry_run", True),
                server_session=mcp_session,
            )
        elif name == "search_preferences":
            from mcp_server.tools.preferences import search_preferences

            result = search_preferences(
                category=arguments.get("category"),
                top_k=arguments.get("top_k", 10),
            )
        # ---- v3.1.0 M8: reflections dispatch ----
        elif name == "reflect":
            from mcp_server.tools.reflections import reflect_async

            # v3.2.0: try the host LLM via sampling/createMessage when
            # this tool runs inside an active MCP request context.
            # reflect_async degrades to the v3.1.0 stub on any failure,
            # so this is safe even when the host doesn't support sampling.
            mcp_session = None
            try:
                mcp_session = server.request_context.session
            except LookupError:
                pass

            result = await reflect_async(
                period_days=arguments.get("period_days", 7),
                dry_run=arguments.get("dry_run", True),
                server_session=mcp_session,
            )
        elif name == "get_reflections":
            from mcp_server.tools.reflections import get_reflections

            result = get_reflections(top_k=arguments.get("top_k", 5))
        elif name == "list_reflections":
            from mcp_server.tools.reflections import list_reflections

            result = list_reflections(
                since=arguments.get("since"),
                tags=arguments.get("tags"),
                limit=arguments.get("limit", 50),
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

    # v3.7.0 (M1): automatic, non-breaking DATA self-heal. Idempotent +
    # ledger-gated + lock-protected + backup-first; failure-isolated so it can
    # never block startup. Heals pre-3.7 id collisions and self-installs the
    # decision-log merge driver. Touches codevira-owned data only — IDE
    # registration is handled surgically by init/setup, never here.
    try:
        from mcp_server.migrate import run_startup_migrations

        _healed = run_startup_migrations()
        if _healed.get("applied"):
            logger.info("Codevira self-heal applied: %s", ", ".join(_healed["applied"]))
    except Exception as e:
        logger.warning("Startup self-heal skipped (non-fatal): %s", e)

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

        # v3.0.0 audit (§4.1): wire AntiRegression git populator
        import os as _os

        if _os.environ.get("CODEVIRA_NO_GIT_SCAN") != "1":
            try:
                from indexer.fix_history import scan_git_log
                from mcp_server.paths import get_project_root

                project_root = get_project_root()
                summary = scan_git_log(project_root)
                if "error" in summary:
                    logger.warning("Git fix-history scan skipped: %s", summary["error"])
                else:
                    logger.info(
                        "Git fix-history scan complete (background): %d recorded, %d skipped",
                        summary.get("fixes_recorded", 0),
                        summary.get("skipped_already_recorded", 0),
                    )
            except Exception as e:
                logger.warning("Could not run startup git fix-history scan: %s", e)
                from mcp_server._safe_crash import safe_log_crash

                safe_log_crash(e, context="startup git fix-history scan")

    import threading

    threading.Thread(
        target=_run_startup_outcome_analysis,
        name="codevira-startup-outcome-analysis",
        daemon=True,
    ).start()

    # v3.0 (2026-05-23 RC-audit follow-up): register this MCP process in
    # the running-MCP registry so `codevira doctor` can detect when our
    # in-memory code is stale relative to the installed wheel — surfaces
    # the "restart Claude Code after pipx --force" failure mode that
    # otherwise bites users silently.
    try:
        from mcp_server._mcp_registry import register, unregister
        from mcp_server.paths import get_project_root
        import atexit as _atexit
        import os as _os

        try:
            _project_root_for_registry = get_project_root()
        except Exception:
            _project_root_for_registry = None
        register(transport="stdio", project_root=_project_root_for_registry)
        _atexit.register(unregister)
        logger.info(
            "Codevira MCP server v%s starting (pid %d, stdio)",
            _codevira_version,
            _os.getpid(),
        )
    except Exception as _reg_err:
        logger.warning("MCP registry write skipped: %s", _reg_err)

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
