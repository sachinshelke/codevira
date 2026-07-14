<!-- codevira:begin (auto-generated; do not edit) -->

## Codevira-tracked project memory: codevira

> **Codevira** — cross-IDE persistent memory. Read it with the codevira MCP tools (`get_session_context`, `search_decisions`); do **not** open `.codevira/*.jsonl` directly — those files are large and token-heavy.

### Locked decisions (do_not_revert)

- **D000001** All disk writes in product surface MUST go through mcp_server/storage/atomic.py — never use open(..., 'w'), write_text(…  ·  _atomic, concurrency, storage, v3.0.0-rc-audit_
- **D000002** All read paths for decisions / sessions / phases MUST go through the v3.0.0 JSONL canonical store (storage/decisions_st…  ·  _jsonl, read-path, storage, v3.0.0-rc-audit_
- **D000006** analyze_session_outcomes() MUST run in a daemon thread at MCP server startup, never inline. The function spawns N git s…  ·  `mcp_server/server.py`  ·  _mcp, outcome-tracker, perf, startup, v3.0_
- **D000007** Hook script fast-path uses ~/.codevira/engine.disabled sentinel as a persistent alternative to CODEVIRA_ENGINE=0 env va…  ·  `mcp_server/data/hooks/user_prompt_submit.sh`  ·  _cli, engine, hooks, perf, v3.0_
- **D000008** repair_incomplete_init() uses a per-project .registered sentinel file to skip the global.db open once registration is c…  ·  `mcp_server/_repair_init.py`  ·  _concurrency, global-db, perf, startup, v3.0_
- **D000009** CODEVIRA_NO_WATCHER=1 env var skips start_background_watcher() in both stdio and HTTP MCP servers. Each codevira proces…  ·  `mcp_server/server.py`  ·  _env-var, fsevents, perf, v3.0, watcher_
- **D000010** Any change to a hero policy (mcp_server/engine/policies/*.py) — especially relevance_inject and decision_lock — MUST ru…  ·  `mcp_server/engine/policies/relevance_inject.py`  ·  _e2e, hero-policy, regression, relevance-inject, testing, v3.0, wedge_
- **D000012** The v3.0.0 JSONL store WRITE path now validates the resolved project root via is_invalid_project_root() inside storage/…  ·  `mcp_server/storage/paths.py`  ·  _claude-desktop, forbidden-root, g5, ship-blocker, storage, v3.0.0, write-path_

### Active conventions

- **D000003** get_project_root() honors CODEVIRA_PROJECT_DIR env var as priority-2 (after --project-dir CLI flag, before cwd discover…  ·  _ide-integration, mcp-config, paths, v3.0.0-rc-audit_
- **D000004** check_conflict uses TWO similarity regimes: (1) symmetric Jaccard ≥ 0.60 for duplicates, (2) asymmetric overlap_coeffic…  ·  _check_conflict, similarity, v3.0.0-rc-audit_
- **D000005** v3.0.0 is NOT yet published to PyPI. .release-evidence/3.0.0.json::G5_human_confirmed=false. The PreToolUse hook blocks…
- **D000011** v3.0.1 candidate fix list (a/b/c from the v3-rc-dogfood session) verified EMPTY on 2026-05-25 — no code work needed. (a…  ·  _decisions-store, no-op, v3.0.1, verification_
- **D000013** Antigravity 2.0 BROKE codevira's IDE integration: the hardcoded Antigravity MCP-config path '~/.gemini/in/mcp_config.js…  ·  _antigravity, broken-integration, cross-tool, ide-inject, setup-wizard, v3.0.1_
- **D000014** [supersedes D000013: Original D000013 dropped its file_path + context (malformed parameters in the record_decision call…  ·  `mcp_server/ide_inject.py`  ·  _antigravity, broken-integration, cross-tool, ide-inject, setup-wizard, v3.0.1_
- **D000015** list_decisions already DEFAULTS to a summary shape (full=False ⇒ slim ~50 tok/row: id, 200-char decision, file_path, do…  ·  _api-consistency, list-decisions, mcp-tools, search-decisions, token-efficiency, v3.0.1_
- **D000016** v3.0.1 will add an interactive, queryable HTML viewer for codevira memory. Design (agreed 2026-05-26): self-contained S…  ·  _cli, cytoscape, export, feature, memory-viewer, v3.0.1, visualization_
- **D000017** [supersedes D000014: D000014 premise was factually wrong: verified the committed code uses ~/.gemini/antigravity/, neve…  ·  `mcp_server/ide_inject.py`  ·  _antigravity, broken-integration, cross-tool, ide-inject, setup-wizard, v3.0.1_
- **D000018** Measured codevira startup token footprint (2026-05-26, chars/4 estimate): MCP tools/list = 16,454 chars ≈ 4,100 tokens …
- **D000019** IMPLEMENTED (commit 7a2bdd4) the D000018 token reduction. New env var CODEVIRA_TOOL_PROFILE=lean trims the advertised M…
- **D00001A** RELEASE SCOPING (per Sachin 2026-05-26): ALL of this session's work ships in the SINGLE 3.0.0 release — there is no sep…
- **D00001B** CORRECTION: the release gauntlet is GREEN, not broken. The 53 'failures' I reported earlier were NOT test rot, NOT test…
- **D00001C** Antigravity 2.0 shared-config support BUILT (commit c83cddf), resolving the open item from D000017. ide_inject.py now d…  ·  _antigravity, cross-tool, ide-inject, v3.0.0_

_+55 more decision(s) — full log in `.codevira/decisions.jsonl`._


For the full decision log, use `search_decisions` / `list_decisions` (or the `codevira` CLI) — don't read `.codevira/*.jsonl` directly.

<!-- codevira:end -->

<!-- codevira:start -->
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

### Before you finish a meaningful unit of work — STRONG RECOMMENDATION

Codevira's promise is "the project remembers what you did." That promise breaks if you don't write to it. Before you respond to the user with the final result of a meaningful change, **call ONE of these**:

- **`update_phase_status(status, ...)`** — if you started or are blocked on the current phase
- **`complete_phase(phase_number, key_decisions=[...])`** — if you finished a phase
- **`write_session_log(...)`** — at minimum, at the end of any session that produced commits or non-trivial output

A session that ships code WITHOUT a codevira write call leaves the project's memory stale for the next AI. That's the most common way the wedge breaks. Treat it as part of the definition-of-done, not optional.

### When you see "Roadmap drift detected" in your SessionStart context

That warning fires when codevira's claimed phase hasn't been updated for several days OR many commits have landed since the last update. Before relying on `get_roadmap` state:

1. Read recent commits: `git log --oneline -20`
2. Compare against codevira's `current_phase` from `get_session_context()`
3. If reality has moved on, call `update_phase_status` or `complete_phase` with the actual state, then proceed
4. If reality matches the claimed phase, the warning is informational — note it and continue

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
<!-- codevira:end -->
