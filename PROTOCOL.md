# Agent Session Protocol

Every AI coding agent working on this project MUST follow this protocol.
This applies to Claude Code, Cursor, Windsurf, and any other AI tool.

---

## WHY THIS EXISTS

Without this protocol, agents spend 15,000+ tokens re-discovering what's already known.
With it: ~1,400 tokens overhead, full context, zero re-discovery.

---

## SESSION START (mandatory, every session)

### Step 1 — Check for unfinished work
```
list_open_changesets()
```
If open changesets exist -> pick up unfinished work BEFORE starting anything new.

### Step 2 — Orient to project state
```
get_roadmap()
```
Read: current phase, next_action, open_changesets.
Confirm the task aligns with the current phase. If not, flag it.

### Step 3 — Check institutional memory (for non-trivial tasks)
```
search_decisions(query)
```
Search past decisions before making a choice. Prevents re-litigating settled issues.

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
search_codebase(task_description)
```
Find existing implementations. Never rewrite what exists.
(Requires `[search]` extras. If not installed, use `get_signature` and `get_code` instead.)

### Step 7 — Start changeset (if multi-file)
```
start_changeset(id, description, files)
```
Required for any change touching 2+ files.

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

`get_signature` works for Python, TypeScript, Go, and Rust files.

Read the full file only when:
- The file is a config, YAML, SQL, Markdown, or other non-code file
- You need broad structural understanding of a file with no graph node
- `get_signature()` shows many symbols you all need to understand together

### Function-level intelligence (v1.5)
```
query_graph(file_path, symbol, "callers")    # who calls this function?
query_graph(file_path, symbol, "callees")    # what does it call?
query_graph(file_path, symbol, "tests")      # what tests cover it?
analyze_changes()                            # risk score for current changes
```

### During coding
- Invoke Reviewer if the file has `stability: high` or `do_not_revert: true`
- Run tests after each file change
- If blast radius grows beyond expected -> re-check with `get_impact()`
- If index reports stale files -> call `refresh_index()` before `search_codebase`

---

## SESSION END (mandatory, every session)

### Step 1 — Update changeset
```
# If all files done:
complete_changeset(id, decisions=["key decision 1", "key decision 2"])

# If ending early (session limit or blocker):
update_changeset_progress(id, last_file, blocker="reason")
```

### Step 2 — Update graph nodes
```
update_node(file_path, {
  "last_changed_by": "Phase N - brief description",
  "new_rules": ["any new invariant discovered"],
})
```
Do this for EVERY file modified.

### Step 3 — Update roadmap
```
update_next_action("Exact description of what needs to happen next")
```

### Step 4 — Write session log
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

1. NEVER start modifying files without calling `start_changeset` first
2. NEVER end a session with a changeset `in_progress` without documenting the blocker
3. If picking up an existing changeset: read it to see what's pending

---

## RULES THAT ARE NEVER NEGOTIABLE

| Rule | Source |
|---|---|
| `do_not_revert: true` files require explicit justification to change | graph nodes |
| `stability: high` files require Reviewer approval before merge | graph nodes |
| All multi-file changes require an open changeset | `PROTOCOL.md` |
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

Codevira supports two MCP transports — stdio (default) and HTTP (via `codevira serve`).

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

**Google Antigravity** (`~/.gemini/settings/mcp_config.json`):
```json
{
  "mcpServers": {
    "codevira": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "codevira",
      "args": ["--project-dir", "/path/to/your-project"]
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
