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

- **`get_session_context()`** — its `style` panel carries the top communication preferences (~30 tokens), so match them without a separate call. For code style, read the surrounding code: naming, error handling and test layout are visible there and do not need a tool.

  *(`search_preferences` was removed in 4.0 — see "Removed in 4.0" below.)*

### When you fix a bug or land an architectural choice

- **`record_decision(...)`** — Capture the *why*: what alternatives were considered, why this won, what would force a re-examination. A one-line decision is enough; the value compounds across sessions and across the other AI tools the user opens this project in.

- For commits that fix a bug, prefer commit messages starting with `fix:`, `bug:`, `hotfix:`, or `fixes #N`. Codevira scans these into a fix-history database used by the Anti-Regression hero to block silent re-introduction of fixed bugs. *(v3.7.0: the scan now self-freshens on read — `fix_history.lookup()` re-scans when git HEAD has advanced — so `fix:` commits made after the server started are picked up without a restart.)*

- **`mark_decision_outdated(decision_id, reason)`** (v3.7.0) — retire a decision that is simply no longer true and has NO successor, so it stops surfacing in `search_decisions` / `list_decisions` / `get_session_context` (reversible; audit preserved). Use `supersede_decision` instead when there IS a replacement. A `do_not_revert` decision needs `force=True` (surface its reasoning first).

### Before you finish a meaningful unit of work — STRONG RECOMMENDATION

Codevira's promise is "the project remembers what you did." That promise breaks if you don't write to it. Before you respond to the user with the final result of a meaningful change, **call ONE of these**:

- **`update_phase_status(status, ...)`** — if you started or are blocked on the current phase
- **`complete_phase(phase_number, key_decisions=[...])`** — if you finished a phase
- **`write_session_log(...)`** — at minimum, at the end of any session that produced commits or non-trivial output

A session that ships code WITHOUT a codevira write call leaves the project's memory stale for the next AI. That's the most common way the wedge breaks. Treat it as part of the definition-of-done.

**Engine enforcement (v3.2.0+, blocking since 4.0):** The `session_log_enforcer` policy fires on `Stop` events. If the session shipped commits AND no `write_session_log` was called between `SESSION_START` and now, it **refuses the Stop** so you can write the log before finishing. Set `CODEVIRA_SESSION_LOG_ENFORCER_MODE=warn` for the pre-4.0 non-blocking nudge, or `off` to disable. The flip was gated on the v3.3.0 instrumentation in `.codevira-cache/enforcer_outcomes.jsonl` (324 Stop evaluations / 7 weeks / 9% warn rate on commit-bearing sessions — a single-machine sample, so expect a higher rate elsewhere). A second consecutive block degrades to a warn, so a session that genuinely cannot write the log still finishes. Logging is still your judgment call for what counts as "meaningful" — if you only answered a question with no commits, the policy stays silent.

### When you see "Roadmap drift detected" in your SessionStart context

That warning fires when codevira's claimed phase hasn't been updated for several days OR many commits have landed since the last update. Before relying on `get_roadmap` state:

1. Read recent commits: `git log --oneline -20`
2. Compare against codevira's `current_phase` from `get_session_context()`
3. If reality has moved on, call `update_phase_status` or `complete_phase` with the actual state, then proceed
4. If reality matches the claimed phase, the warning is informational — note it and continue

### When the user asks "what did we decide about X"

- **`search_decisions(query="X")`** is the answer. Don't guess — surface the actual decision log.

## Memory subsystems (v3.1.0)

v3.1.0 added five memory subsystems on top of the existing decision log. Each has a specific moment to call it; together they cover the gap between "episodic" (decisions) and "the agent's day-to-day state."

### Working memory — intra-session scratchpad

`.codevira-cache/working.jsonl` (per-machine, ephemeral, gitignored). Capacity-bounded, decay-scored.

- **`working_add(content, kind="observation"|"goal", importance=5, links=[])`** — record an observation (something you saw) or a goal (something you're trying). `Edit`/`Write`/`Bash` calls auto-populate this via the post_tool_use hook; explicit calls add narrative + intent the auto path can't see.
- **`working_get(top_k=10, kind=?)`** — top-K live entries by decay score (importance × exp(-Δt/τ=6h) + 0.5 × access_count). Tombstoned entries excluded.
- **`working_promote(entry_id, to="decision"|"skill"|"playbook", ...)`** — move an observation/goal into LTM. Calls `check_conflict` first; tombstones the source on success.
- **`get_working_context(top_k=5)`** — compact markdown for ReAct-loop injection.

Working memory persists into `get_session_context` (top-3 panel) so the next call sees your recent scratchpad.

CLI escape hatch: `codevira working commit <session_id>` archives a session's live entries to `.codevira/working_archived/<session_id>.jsonl` (canonical, team-shareable).

### Skill library — procedural memory

`.codevira/skills.jsonl` (canonical, team-shareable). FTS5-backed retrieval with composite ranking (BM25 + tag-Jaccard + recency).

- **`record_skill(name, procedure, summary, triggers, do_not_revert, force)`** — author a reusable procedure ("how we rebase in this repo", "the project's commit-message convention"). Conflict-checked against existing skills.
- **`get_skill(query, top_k=5, file_path=?)`** — composite-ranked search. Returns `score_breakdown` so you can see WHY each skill surfaced.
- **`apply_skill_outcome(skill_id, success)`** — manual reinforcement. The *canonical* signal comes from git via `outcomes_writer` fan-out (M5) — this tool is the override.
- **`list_skills(status="active"|"archived"|"superseded"|"all", source, tags)`** — daily-driver `active` filter by default.
- **`supersede_skill(old_id, name, procedure, ...)`** — version a skill; amendment chain preserves audit.
- **`promote_skill_to_playbook(skill_id, task_type, name?, force)`** — write a skill's procedure as a playbook markdown so `get_playbook(task_type)` finds it.

Auto-archive at 5 consecutive failures OR `unused_days ≥ 90` (configurable). Skills with `do_not_revert=true` are exempt.

CLI: `codevira induce-skills [--apply] [--yes]` — cluster productive sessions (≥80% kept, tag-Jaccard ≥ 0.5) and propose induced skills. Without `--apply`: writes to `.codevira/induction_proposals.jsonl` for review.

### Provenance — who wrote what

- **`origin_of(decision_id)`** — which IDE, which machine, when. Retained
  when the rest of the consensus subsystem was cut in 4.0, because
  provenance is what `id_repair` uses to attribute an amendment across a
  two-host merge.

## Removed in 4.0 — do not call these

Fifteen tools were cut on measured usage across 4,203 transcripts. If you
have them in memory from an older version of this file, they no longer
exist and calling them will fail:

- `consensus_check`, `consensus_status`, `consensus_propose_supersession`,
  `consensus_resolve` — use `origin_of` for provenance
- `reflect`, `get_reflections`, `list_reflections`
- `spatial_nearby`, `spatial_heat`, `spatial_neighborhood`,
  `spatial_affordances` — use `get_impact(file_path)` for structural
  neighbours
- `distill_preferences`, `search_preferences`
- `get_code`, `get_signature` — Read the file directly; both measured
  **zero** calls in 2.5 months

`get_session_context()` still includes the one-line `style` panel, so
match the user's communication preferences without needing a tool call.

## Enforcement reaches every editor (4.0)

Enforcement used to be an IDE hook, which meant only Claude Code
hard-blocked. `codevira engine install-git-hook` installs a `pre-commit`
hook that runs locked decisions against staged changes through the same
engine — so a `do_not_revert` decision is enforced in any editor, because
they all commit with git.

- `git commit --no-verify` overrides a single commit
- `CODEVIRA_GIT_HOOK_MODE=warn` (or `off`) changes it globally
- merge commits are never blocked

## Rolling back memory (4.0)

`.codevira/` is gitignored, so `git revert` cannot undo a bad migration or
an accidental wipe. Before any risky operation on the store:

```bash
codevira memory snapshot --note "before <whatever>"
```

`codevira memory undo` restores it. The current state is captured first,
so the undo is itself undoable.

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

The user may open this project in multiple AI tools across the day — Claude Code, Cursor, Antigravity, Gemini, Codex, Copilot. **They all see the same project memory through Codevira.** What you record here is visible to whichever tool the user opens next.

This means:

- A decision you log in Claude Code shows up in Cursor.
- A fix you record will block the same regression in Cursor the next morning.
- A style preference learned in one session enforces in the next.

Be a good citizen: log decisions, respect existing ones, and assume the next AI to read this graph isn't you.
<!-- codevira:end -->
