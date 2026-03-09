# Orchestrator — Agent Routing Logic

## Purpose
Determine which agents to invoke based on the nature of the developer's prompt.
This is not a separate process — it is the decision logic that any agent (Claude Code, Cursor, Windsurf)
should follow at the start of a session before doing any work.

---

## Step 1: Classify the Task

Read the developer prompt and classify it:

| Signal | Task Type |
|---|---|
| "fix", "bug", "broken", "error" | `small_fix` or `medium_change` |
| "add", "implement", "new feature" | `medium_change` or `large_change` |
| "refactor", "restructure", "redesign" | `large_change` |
| "review", "audit", "check" | `parallel_review` |
| "explain", "what does", "how does" | `research` (no code changes) |
| Affects 1 file, stability=high | `small_fix` |
| Affects 2–4 files | `medium_change` |
| Affects 5+ files or crosses service boundaries | `large_change` |

---

## Step 2: Select Agent Pipeline

### `small_fix` (1 file, low blast radius)
```
Developer → Tester → Documenter
```
Token budget: ~900 overhead

### `medium_change` (2–4 files, known blast radius)
```
Developer → Reviewer → Tester → Builder → Documenter
```
Token budget: ~1,400 overhead

### `large_change` (phase-level, cross-service, or uncertain scope)
```
Planner → Developer → Reviewer → Tester → Builder → Documenter
```
Token budget: ~2,000 overhead

### `parallel_review` (audit a module or multiple files)
```
[Agent A: file group 1] + [Agent B: file group 2]  ← parallel
→ Documenter
```
Token budget: ~800 per parallel agent

### `research` (no code changes)
```
No agents — just MCP tool calls:
  get_node() + get_impact() + search_codebase() + search_decisions()
```
Token budget: ~400

---

## Step 3: MCP Tools Per Stage

Every agent in the pipeline calls only the MCP tools it needs:

| Agent | MCP Tools | Shell Commands |
|---|---|---|
| Planner | get_full_roadmap, get_impact, list_nodes, search_codebase, search_decisions, add_phase | — |
| Developer | list_open_changesets, get_roadmap, get_node, get_impact, search_codebase, search_decisions, start_changeset, refresh_index, add_node | — |
| Reviewer | get_node, get_playbook | — |
| Tester | get_node (for tests field), list_nodes | project test command |
| Builder | — | linter, type checker, optional architecture verifier |
| Documenter | complete_changeset, update_node, update_next_action, update_phase_status, add_phase, write_session_log | — |

---

## Step 4: Reviewer Trigger Conditions

Reviewer is NOT always needed. Trigger it when ANY of:
- The file's graph node has `stability: high`
- The file's graph node has `do_not_revert: true`
- The file's graph node has non-empty `rules`
- The change affects a schema or event payload
- The change touches `.agents/graph/` or `.agents/roadmap.yaml`

Skip reviewer for: scaffolding, new test files, config-only changes, docs.

---

## Escalation Rule

If at any point the blast radius (`get_impact` result) returns more files than expected for the task type, escalate:
- `small_fix` → blast_radius > 3 → escalate to `medium_change`
- `medium_change` → blast_radius > 7 → escalate to `large_change`
- `large_change` → blast_radius > 15 → stop, call `get_roadmap` and document the scope before proceeding
