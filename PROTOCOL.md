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
If open changesets exist → pick up unfinished work BEFORE starting anything new.

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
Examples: `search_decisions("caching")`, `search_decisions("threshold")`, `search_decisions("schema")`.

### Step 4 — Classify the task
Determine task type using `agents/orchestrator.md`:
- `small_fix` → 1 file, low impact
- `medium_change` → 2–4 files
- `large_change` → 5+ files or phase-level → invoke Planner first
- `parallel_review` → audit tasks
- `research` → no code changes

### Step 5 — Load context for relevant files
```
get_node(file_path)      # for each file you will touch
get_impact(file_path)    # for each file you will MODIFY
```
Read rules and `do_not_revert` flags BEFORE writing a line.

If `get_node()` returns `index_status.stale: true` → call `refresh_index([file_path])` first.

### Step 6 — Find patterns
```
search_codebase(task_description)
```
Find existing implementations. Never rewrite what exists.

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
| 3 | `get_code(file_path, symbol)` | Full body of one function or class | ~200–500 tokens |
| 4 | Read full file | Everything | 3,000–8,000 tokens — **LAST RESORT only** |

Read the full file only when:
- The file is non-Python (config, YAML, SQL, Markdown, etc.) — `get_signature` is Python-only
- You need broad structural understanding of a file with no graph node
- `get_signature()` shows more than 5 symbols you all need to understand together

- Follow the agent definition for your role: `.agents/agents/developer.md`
- Invoke Reviewer if the file has `stability: high` or `do_not_revert: true`
- Run tests via Tester after each file change
- Run Builder checks (linter, type checker) before declaring done
- If blast radius grows beyond expected → re-check with `get_impact()`
- If index reports stale files → call `refresh_index()` before `search_codebase`

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
  "last_changed_by": "Phase N — brief description",
  "new_rules": ["any new invariant discovered"],
  "new_connections": [{"target": "other/file.py", "edge": "depends_on"}],
  "key_functions": ["new_public_function"],
  "stability": "high",       # only if stability changed
  "new_tests": ["tests/unit/test_new.py"],
})
```
Do this for EVERY file modified.

### Step 3 — Update roadmap
```
update_next_action("Exact description of what needs to happen next")

# If new work was discovered during this session:
add_phase(phase=N, name="...", description="...", priority="medium")

# If current phase is now complete:
complete_phase(phase_number=N, key_decisions=["decision 1", ...])
```

### Step 4 — Write session log via MCP
```
write_session_log(
  session_id="<first 8 chars of UUID>",
  task="<developer's original prompt>",
  task_type="small_fix | medium_change | large_change",
  files_changed=["src/services/generator.py"],
  decisions=["key decision 1", "key decision 2"],
  phase=<current phase number>,
  next_action="<what was set in roadmap>",
  agents_invoked=["developer", "tester", "documenter"],
  tests_run=["tests/unit/test_feature.py"],
  tests_passed=True,
  build_clean=True,
  changeset_id="<id or None>"
)
```

---

## MULTI-FILE FIX RULES

1. NEVER start modifying files without calling `start_changeset` first
2. NEVER end a session with a changeset `in_progress` without documenting the blocker
3. If picking up an existing changeset: call `get_changeset(id)` to see what's pending

---

## RULES THAT ARE NEVER NEGOTIABLE

These apply in ALL sessions regardless of task:

| Rule | Source |
|---|---|
| `do_not_revert: true` files require explicit justification to change | graph nodes |
| `stability: high` files require Reviewer approval before merge | graph nodes |
| All multi-file changes require an open changeset | `PROTOCOL.md` |
| Import layer violations are blocked (if your project defines layers) | project rules |
| Session log must be written at session end | `agents/documenter.md` |

---

## MCP SERVER SETUP

The MCP server must be running for this protocol to work.

**Claude Code** (`.claude/settings.json`), **Cursor / Windsurf** (Settings → MCP):
```json
{
  "mcpServers": {
    "codevira": {
      "command": "/path/to/your-project/.venv/bin/python",
      "args": ["-m", "mcp_server", "--project-dir", "/path/to/your-project"]
    }
  }
}
```

**Google Antigravity** — add to `~/.gemini/antigravity/mcp_config.json`:
```json
{
  "mcpServers": {
    "codevira": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "/path/to/your-project/.venv/bin/python",
      "args": ["-m", "mcp_server", "--project-dir", "/path/to/your-project"]
    }
  }
}
```

Verify it works: ask your agent call `get_roadmap()` — it should return the current active phase.

---

## FIRST-TIME SETUP (any project)

```bash
# Install the package
pip install codevira-mcp

# Initialize Codevira in your project
codevira init
```

This single command:
- Creates `.codevira/` with config, graph, and log directories.
- Adds `.codevira/` to `.gitignore`.
- Builds the full code index.
- Auto-generates graph stubs for all source files.
- Bootstraps `.codevira/roadmap.yaml` from git history.
- Installs a `post-commit` git hook for automatic reindexing.
- Prints the MCP config block to paste into your AI tool.

After this, `get_node()`, `get_impact()`, `search_codebase()`, and `get_roadmap()` all work immediately.

## CODE INDEX SETUP (first time or after major changes)

```bash
# First time
pip install -r .agents/requirements.txt
python .agents/indexer/index_codebase.py --full

# Check index health
python .agents/indexer/index_codebase.py --status

# Real-time sync during active development
python .agents/indexer/index_codebase.py --watch

# Auto-reindex on every commit (install once)
bash .agents/hooks/install-hooks.sh
```

The index is git-ignored — it lives in `.agents/codeindex/` (binary files, auto-regenerated).
Agents self-heal via `refresh_index()` when `get_node()` reports `index_status.stale: true`.

## GRAPH SELF-HEAL (after creating a new file)

When you create a new file and need its graph node immediately (without the CLI):

```
refresh_graph(["path/to/new_file.py"])
```

Or to scan all unregistered Python files:

```
refresh_graph()
```

Safe merge: existing enriched nodes are never overwritten.
`refresh_graph` is the MCP equivalent of `--generate-graph` — use it mid-session.
