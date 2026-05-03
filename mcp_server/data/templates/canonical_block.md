# Codevira — persistent project memory

This project uses **Codevira** to give every AI coding tool you use shared memory of the project. Decisions, fix history, the structural code graph, and learned style preferences persist across sessions and across IDEs.

## When to call which Codevira tool

Call these MCP tools at the moments the description matches your action — they are fast (tens of ms), token-efficient (summary-by-default), and what makes the project's memory available to you.

### At the start of every session

- **`get_session_context()`** — Always call this first. Returns a ~500-token brief: current focus, recent decisions, top open items. Without it you're blind to the project's history.

### Before modifying any file

- **`get_impact(file_path)`** — Returns who calls this code (callers, dependents, blast radius). Required before any rename, signature change, or cross-cutting refactor. If callers > a small number, propose a deprecation+migration plan instead of a direct edit.

- **`search_decisions(query)`** — Returns prior architectural decisions touching the topic. Decisions with `do_not_revert: true` MUST be respected; if your proposed change conflicts, surface the decision to the user and wait for approval before proceeding.

### Before adopting a pattern, library, or naming convention

- **`search_preferences(category)`** — Returns this project's coding style (snake_case vs camelCase, error-handling idioms, test layout, etc.). Match these unless the user explicitly asks to change them.

### When you fix a bug or land an architectural choice

- **`record_decision(...)`** — Capture the *why*: what alternatives were considered, why this won, what would force a re-examination. A one-line decision is enough; the value compounds across sessions and across the other AI tools the user opens this project in.

- For commits that fix a bug, prefer commit messages starting with `fix:`, `bug:`, `hotfix:`, or `fixes #N`. Codevira scans these into a fix-history database used by the Anti-Regression hero to block silent re-introduction of fixed bugs.

### When the user asks "what did we decide about X"

- **`search_decisions(query="X")`** is the answer. Don't guess — surface the actual decision log.

## Tool budget discipline

Codevira is **token-efficient by design**:

- Tools return summaries by default. If you need full data, pass `full=true`.
- `get_session_context()` is one ~500-token call — not a stream of round-trips.
- Don't call the same tool five times with slight variations. One precise call beats five exploratory ones.

If a tool returns more than you need, narrow the query. If it returns less than you need, escalate to `full=true` rather than calling it again.

## Decision protection (the `do_not_revert` flag)

Some decisions in this project are protected. If `search_decisions()` returns a decision with `do_not_revert: true`:

- **Treat it as an architectural constraint.** Do not propose changes that conflict.
- **If the user explicitly asks you to revert it,** surface the decision's reasoning, the date, and what would force a re-examination, then ask for confirmation before proceeding.
- **Never silently work around it.** If you find yourself thinking "I'll just rewrite this differently," check whether the rewrite reverts a protected decision.

## Cross-tool memory

The user may open this project in multiple AI tools across the day — Claude Code, Cursor, Windsurf, Antigravity, Gemini, Codex, Copilot. **They all see the same project memory through Codevira.** What you record here is visible to whichever tool the user opens next.

This means:

- A decision you log in Claude Code shows up in Cursor.
- A fix you record will block the same regression in Windsurf the next morning.
- A style preference learned in one session enforces in the next.

Be a good citizen: log decisions, respect existing ones, and assume the next AI to read this graph isn't you.
