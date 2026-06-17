# Agent Session Protocol

Every AI coding agent working on this project MUST follow this protocol.
This applies to Claude Code, Cursor, Windsurf, and any other AI tool.

---

## WHY THIS EXISTS

Without this protocol, agents spend 15,000+ tokens re-discovering what's already known.
With it: ~1,400 tokens overhead, full context, zero re-discovery.

---

## SESSION START (mandatory, every session)

### Step 1 — Catch up on project state
```
get_session_context()
```
The ~500-token "catch me up" orientation call. Returns current focus, recent decisions, top open items, and working-memory scratchpad. Call this FIRST — without it you're blind to the project's history.

### Step 2 — Orient to the roadmap
```
get_roadmap()
```
Read: current phase, next_action.
Confirm the task aligns with the current phase. If not, flag it.

### Step 3 — Check institutional memory (for non-trivial tasks)
```
search_decisions(query)
```
FTS5 keyword search of past decisions before making a choice. Prevents re-litigating settled issues.

### Step 4 — Classify the task
Determine task type:
- `small_fix` — 1 file, low impact
- `medium_change` — 2-4 files
- `large_change` — 5+ files or phase-level
- `research` — no code changes

### Step 5 — Load context for relevant files
```
get_node(file_path)      # for each file you will touch
get_impact(file_path)    # for each file you will MODIFY
```
Read rules and `do_not_revert` flags BEFORE writing a line.

### Step 6 — Find patterns
```
get_signature(file_path)         # all public symbols in a candidate file
get_code(file_path, symbol)      # full body of a function or class
query_graph(file_path, symbol, "callers")    # who already uses this?
```
Find existing implementations. Never rewrite what exists.

### Step 7 — Ensure the roadmap phase is current (if multi-file)
```
get_phase(phase_number)                       # read the phase you're working in
update_phase_status(status)                   # mark it in_progress if not already
```
Multi-file work is tracked via roadmap phases + working memory, not a separate changeset. If your change is a phase of work, make sure that phase reflects reality before you start.

---

## DURING THE SESSION

### CODE READING HIERARCHY

Never read a full source file when a targeted tool works. Use in order:

| Step | Tool | Returns | Cost |
|---|---|---|---|
| 1 | `get_node(file_path)` | Role, rules, key_functions, connections | ~50 tokens |
| 2 | `get_signature(file_path)` | All public symbols, signatures, line ranges | ~150 tokens |
| 3 | `get_code(file_path, symbol)` | Full body of one function or class | ~200-500 tokens |
| 4 | Read full file | Everything | 3,000-8,000 tokens — **LAST RESORT** |

`get_signature` works for Python, TypeScript, JavaScript, JSX, Go, and Rust files.

Read the full file only when:
- The file is a config, YAML, SQL, Markdown, or other non-code file
- You need broad structural understanding of a file with no graph node
- `get_signature()` shows many symbols you all need to understand together

### Function-level intelligence
```
query_graph(file_path, symbol, "callers")    # who calls this function?
query_graph(file_path, symbol, "callees")    # what does it call?
query_graph(file_path, symbol, "tests")      # what tests cover it?
```

### During coding
- Invoke Reviewer if the file has `stability: high` or `do_not_revert: true`
- Run tests after each file change
- If blast radius grows beyond expected -> re-check with `get_impact()`

---

## SESSION END (mandatory, every session)

### Step 1 — Update roadmap
```
update_next_action("Exact description of what needs to happen next")
```

### Step 2 — Close or advance the phase
```
# If you finished a phase:
complete_phase(phase_number, key_decisions=["key decision 1", "key decision 2"])

# If ending mid-phase (session limit or blocker):
update_phase_status(status="in_progress")    # or "blocked", with the reason
```

### Step 3 — Write session log
```
write_session_log(
  session_id="<8-char slug>",
  task="<developer's original prompt>",
  phase="<current phase number>",
  files_changed=["src/services/generator.py"],
  decisions=[
    {"file_path": "src/service.py", "decision": "Used retry pattern", "context": "API calls timeout"}
  ],
  next_steps=["Write integration tests", "Update docs"]
)
```

---

## MULTI-FILE FIX RULES

1. If the work is a phase, ensure its roadmap phase is current before you start (`get_phase` / `update_phase_status`)
2. NEVER end a session with a phase left `in_progress` without documenting the blocker via `update_phase_status`
3. If picking up an existing phase: read it with `get_phase` to see what's pending

---

## RULES THAT ARE NEVER NEGOTIABLE

| Rule | Source |
|---|---|
| `do_not_revert: true` files require explicit justification to change | graph nodes |
| `stability: high` files require Reviewer approval before merge | graph nodes |
| Session log must be written at session end | protocol |

---

## MCP SERVER SETUP

The MCP server must be running for this protocol to work.

**Quick setup (recommended):**
```bash
pip install codevira    # or: pipx install codevira
cd your-project
codevira init               # auto-detects everything, auto-injects IDE configs
```

Restart your AI tool after init. Verify: ask your agent to call `get_roadmap()`.

**Manual config (if auto-inject didn't work):**

Codevira supports two MCP transports:
- **stdio (default + recommended)** — the IDE spawns `codevira` per project. Multi-project out of the box.
- **HTTP/HTTPS (preview)** — single-project only. Use stdio for multi-project work.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`) — stdio only:
```json
{
  "mcpServers": {
    "codevira": {
      "command": "/path/to/codevira",
      "args": ["--project-dir", "/path/to/your-project"]
    }
  }
}
```

**Claude Code CLI, Cursor, Windsurf** — stdio (`.claude/settings.json` / `.cursor/mcp.json` / `.windsurf/mcp.json`):
```json
{
  "mcpServers": {
    "codevira": {
      "command": "codevira",
      "args": [],
      "cwd": "/path/to/your-project"
    }
  }
}
```

**Claude Code CLI** — HTTP transport (start server first, then register URL):
```bash
codevira serve --https --port 7443 --project-dir /path/to/your-project
```
```json
{
  "mcpServers": {
    "codevira": {
      "url": "https://localhost:7443/mcp"
    }
  }
}
```

**Google Antigravity** (`~/.gemini/antigravity/mcp_config.json`):
```json
{
  "mcpServers": {
    "codevira": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "codevira",
      "args": []
    }
  }
}
```

---

## GRAPH SELF-HEAL (after creating a new file)

When you create a new file and need its graph node immediately:

```
refresh_graph(["path/to/new_file.py"])
```

Or to scan all unregistered files:

```
refresh_graph()
```

Safe merge: existing enriched nodes are never overwritten.
