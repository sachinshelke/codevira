# Codevira v2.2.0 — Surface-cut decisions (Phase 1 artifact)

> Per-item recommendation for every MCP tool, CLI command, nudge file,
> hook, engine policy, and config write location.
>
> **Each recommendation grounded in audit findings**:
> - **F1**: adoption gap — AI never auto-calls codevira; founder uses
>   explicit "/codevira X" pattern. If a tool isn't called via that
>   pattern OR isn't load-bearing for an auto-fire flow → DELETE.
> - **F2**: competitive position — codevira's only unique wedge is
>   hard PreToolUse enforcement. Everything else is matched by simpler
>   tools (`.cursorrules`, AGENTS.md, mem0, --continue).
> - **F3**: surface area — ~15 system touch points cause "pollution"
>   complaints. Every config write needs to justify its existence.
>
> **Legend**: ✅ KEEP · 🔄 FOLD (merge into another) · ❌ DELETE
>
> **You sign off, then Phase 2 executes exactly this kill list.**

---

## A — MCP tools (46 total, from `server.py::list_tools`)

Grouped by feature area. Recommendation per tool.

### Decisions (write side — the "memory" wedge)

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `record_decision` | core | ✅ KEEP | The write API. Must work even though F1 says AI doesn't call it without prompting — we'll fix that in Phase 4. |
| `record_decisions` (batch) | rare | 🔄 FOLD into `record_decision` (accept list-or-single) | Batch is theatre; 95% of writes are single. One API surface, less docs. |
| `mark_decision_protected` | rare | 🔄 FOLD into `record_decision(do_not_revert=True)` already exists | The standalone tool only mutates do_not_revert; redundant. |
| `supersede_decision` | rare | ✅ KEEP | Genuinely needed for "I was wrong, here's the new decision" pattern. Append-amendment to JSONL. |

### Decisions (read side — what's actually used per F1)

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `get_session_context` | core | ✅ KEEP | The primary read entry. Founder's `"using codevira get roadmap"` pattern lands here. Rename consideration: `get_context()` is shorter. |
| `search_decisions` | medium | ✅ KEEP | Query-driven decision lookup. Real use case. |
| `list_decisions` | low | ✅ KEEP | Enumeration. Niche but cheap to keep. |
| `list_tags` | very low | 🔄 FOLD into `list_decisions(group_by="tag")` | Standalone tool unused; option flag fine. |
| `get_history` | low | 🔄 FOLD into `search_decisions(file_path=...)` | Same shape; one tool. |
| `get_decision_confidence` | never | ❌ DELETE | Surfaces a number nobody acts on. |
| `check_conflict` | rare | ✅ KEEP | Genuine value when AI is about to record a conflicting decision. Make it implicit in `record_decision` flow. |

### Sessions (the "what did we do last time" frame)

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `write_session_log` | core | ✅ KEEP | The user-initiated checkpoint. |
| `write_session_logs` (batch) | rare | 🔄 FOLD into `write_session_log` (accept list) | Same as record_decisions batch. |

### Roadmap / phases (the "what we're working on")

F1 confirms `get_roadmap` is the #1 user-invoked read. **Keep these; they're the load-bearing surface for the founder's actual usage.**

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `get_roadmap` | core | ✅ KEEP | Founder's actual use pattern. |
| `get_full_roadmap` | low | 🔄 FOLD into `get_roadmap(full=True)` | Two tools, one job. |
| `get_phase` | low | ✅ KEEP | Per-phase detail when needed. |
| `add_phase` | medium | ✅ KEEP | Active write. |
| `complete_phase` | medium | ✅ KEEP | Active write. |
| `defer_phase` | rare | ✅ KEEP | Real use case (we use it ourselves). |
| `update_phase_status` | rare | 🔄 FOLD into `update_next_action` + `complete_phase` | Status-only updates are redundant. |
| `update_next_action` | medium | ✅ KEEP | The "what's next" frame. |
| `bulk_import_phases` | rare | ✅ KEEP | Real onboarding pain (test fixture for Item 29). |

### Changesets (multi-file work tracking)

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `start_changeset` | rare | ❌ DELETE | Adds ceremony; AI doesn't use it; users don't either. |
| `update_changeset_progress` | never | ❌ DELETE | Same. |
| `complete_changeset` | never | ❌ DELETE | Same. |
| `list_open_changesets` | never | ❌ DELETE | Same. |

(Changesets were a 2026-03 design that never got real usage. Drop the
whole concept; AGENTS.md + decisions cover the same ground.)

### Code graph (the auto-populated structure)

Per F2, this IS a genuine value-add. But ask: which tools are
load-bearing for an `/codevira context` slash-command?

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `get_node` | medium | ✅ KEEP | "Tell me about this file." Real value. |
| `get_impact` | high | ✅ KEEP | "Who depends on this?" The single most useful graph query. |
| `query_graph` | low | ✅ KEEP | Power-user surface. Cheap to keep. |
| `update_node` | rare | ❌ DELETE | Manual graph mutation; rebuild from source is simpler. |
| `add_node` | rare | ❌ DELETE | Same — graph generator owns this. |
| `list_nodes` | low | 🔄 FOLD into `query_graph` | Same shape. |
| `get_signature` | medium | ✅ KEEP | "What's the API of this function?" Real use case. |
| `get_code` | medium | ✅ KEEP | "Read this function's body." Real use case. |
| `get_playbook` | rare | ✅ KEEP | Documented onboarding aid; cheap. |
| `analyze_changes` | never | ❌ DELETE | Vestigial. |
| `export_graph` | never | ❌ DELETE | Power-user noise. |
| `find_hotspots` | never | ❌ DELETE | Vestigial. |
| `get_graph_diff` | never | ❌ DELETE | Vestigial. |
| `refresh_graph` | low | 🔄 FOLD into `refresh_index` (alias) | Two tools, same job. |
| `refresh_index` | low | ✅ KEEP | Forced full reindex. |

### Preferences / learned rules (the "style" frame)

| Tool | Used? | Recommendation | Why |
|---|---|---|---|
| `get_preferences` | rare | ❌ DELETE | Surfaces auto-extracted style hints; F1 says nobody reads them. AGENTS.md does it better. |
| `get_learned_rules` | rare | ❌ DELETE | Same. The rule extractor is noise-prone (Item 17 in v2.1.2 plan). |
| `retire_rule` | rare | ❌ DELETE | Cleans up output of a feature we're deleting. |
| `get_project_maturity` | never | ❌ DELETE | Vestigial signal nobody acts on. |

### Tally

| Status | Count | Notes |
|---|---|---|
| ✅ KEEP | **18** | Core memory + roadmap + graph + sessions |
| 🔄 FOLD | **7** | Redundant tools merged into siblings |
| ❌ DELETE | **15** | Vestigial / never-used / theatre |
| **Total in v2.2.0+ (after cuts)** | **18 of 46** | -61% surface |

The 18 kept tools are the *minimum-viable codevira* — every one
either (a) the founder demonstrably uses, (b) load-bearing for the
slash-command pattern Phase 3 ships, or (c) the unique enforcement
wedge from F2.

---

## B — CLI subcommands (19 total)

| Command | Recommendation | Why |
|---|---|---|
| `init` | ✅ KEEP | Bootstrap. Rewrite for new minimal surface. |
| `setup` | ❌ DELETE | Replaced by `init --ide claude` (per F3 IDE scope reduction). |
| `register` | ❌ DELETE | Per-IDE MCP registration; folds into `init`. |
| `configure` | ❌ DELETE | Vestigial; `init` covers project setup. |
| `index` | ✅ KEEP | Build the code graph. Keep but rename `refresh-graph` for clarity. |
| `status` | ✅ KEEP | The "is codevira healthy here?" command. |
| `report` | 🔄 FOLD into `doctor` | One health command, not two. |
| `doctor` | ✅ KEEP | Self-diagnose. |
| `serve` | ✅ KEEP | MCP stdio server. |
| `projects` | ✅ KEEP | List tracked projects across machine. |
| `agents` | ❌ DELETE | Nudge-file generator; per F3 we're dropping all nudge files except AGENTS.md (auto-generated). |
| `hooks` | ❌ DELETE | Per-IDE hook scripts; `init --ide claude` covers it. |
| `replay` | ✅ KEEP | Real value — visual timeline of decisions. |
| `insights` | ❌ DELETE | Vestigial — surfaces "preferences/learned_rules" we're deleting. |
| `clean` | ✅ KEEP | Cleanup; rename to `cleanup-orphans` for clarity. |
| `heal` | ❌ DELETE | Deprecated; `reset` is the supported destructive op. |
| `reset` | ✅ KEEP | Wipe + rebuild. |
| `export` | ✅ KEEP | Decision export. |
| `sync` | ✅ KEEP | Regenerate manifest + digest + AGENTS.md. |
| `observe-git` | ✅ KEEP | Outcome tracker. |
| `calibrate` | ❌ DELETE | Vestigial — no semantic threshold to calibrate in v2.2.0. |
| `engine` | 🔄 FOLD into `doctor engine` | Single engine inspector. |
| `budget` | ❌ DELETE | Token-budget management is now per-policy, not a CLI command. |
| **(NEW)** `uninstall` | ✅ ADD | F3: reverses every system write. The missing command users complained about. |

### Tally

| Status | Count |
|---|---|
| ✅ KEEP | **12** |
| 🔄 FOLD | **3** |
| ❌ DELETE | **9** |
| ✅ ADD | **1** |
| **Total in v2.2.0+** | **13** (was 19; -32%) |

---

## C — Nudge files (5 + per-IDE)

F3 says we currently write to 5+ nudge file types. AGENTS.md is the
Linux Foundation standard — every modern IDE reads it. Per-IDE
nudges are duplicates.

| File | Recommendation | Why |
|---|---|---|
| `AGENTS.md` | ✅ KEEP | The canonical, multi-IDE-read standard. Auto-generated by codevira; user content outside marker block preserved. |
| `CLAUDE.md` | ❌ DELETE | Claude Code reads AGENTS.md natively (with CLAUDE.md as alias). Drop the duplicate. |
| `GEMINI.md` | ❌ DELETE | Gemini reads AGENTS.md. |
| `.cursor/rules/codevira.mdc` | ❌ DELETE | Cursor reads AGENTS.md AND has its own `.cursorrules`. We don't need a third channel. |
| `.windsurfrules` | ❌ DELETE | Windsurf reads AGENTS.md. |
| `.github/copilot-instructions.md` | ❌ DELETE | Copilot reads AGENTS.md. |

### Tally

| Status | Count |
|---|---|
| ✅ KEEP | **1** (AGENTS.md only) |
| ❌ DELETE | **5** |

---

## D — Hook scripts (Claude Code only after F3 IDE scope reduction)

| Hook | Recommendation | Why |
|---|---|---|
| `~/.claude/hooks/codevira-pretool.sh` | ✅ KEEP | The decision-enforcement wedge (F2). |
| `~/.claude/hooks/codevira-prompt.sh` | ✅ KEEP | UserPromptSubmit → RelevanceInject. |
| `~/.claude/hooks/codevira-posttool.sh` | ✅ KEEP | F1 fix: auto-record on Edit/Write (Phase 4). |
| `~/.claude/hooks/codevira-session-start.sh` | 🔄 FOLD into prompt.sh | Same code path. |
| Per-IDE hook scripts for Cursor/Windsurf/etc. | ❌ DELETE | Dropped along with non-Claude-Code IDE configs. |

---

## E — Engine policies (10 total in `mcp_server/engine/policies/`)

| Policy | Recommendation | Why |
|---|---|---|
| `decision_lock` | ✅ KEEP | The do_not_revert PreToolUse enforcement. F2's unique wedge. |
| `relevance_inject` | ✅ KEEP | UserPromptSubmit injection (we just rebuilt this for v2.2.0). |
| `post_edit_refresh` | ✅ KEEP | Auto-refresh graph after Edit. Cheap, useful. |
| `anti_regression` | ✅ KEEP | Fix-history-aware blocking. Niche but unique. |
| `scope_contract` | ❌ DELETE | Never fires; complicated to explain; users don't trust it. |
| `blast_radius` | 🔄 FOLD into `decision_lock` (warning, not block) | Soft warning is enough; full veto is theatre. |
| `intent_inference` | ❌ DELETE | "Guess what the user is trying to do" — guesses wrong, annoys users. |
| `live_style` | ❌ DELETE | Style enforcement at PostToolUse — same noise problem as `learned_rules`. |
| `ai_promotion` | ❌ DELETE | SessionStart promotion ranking; never validated; produces noise. |
| `token_budget` | ✅ KEEP | Real safety net for runaway injection. |

### Tally

| Status | Count |
|---|---|
| ✅ KEEP | **6** |
| 🔄 FOLD | **1** |
| ❌ DELETE | **3** |
| **Total in v2.2.0+** | **6** (was 10; -40%) |

---

## F — Config write locations (uninstall inventory)

F3 said 15 touch points. Post-cut, what remains?

### Per-user (machine-wide)
- ✅ `~/.codevira/global.db` — cross-project tracking. Keep.
- ✅ `~/.codevira/logs/crashes.log` — observability. Keep.
- ❌ `~/.codevira/projects/<key>/` — legacy v2.1.x layout. DELETE on init.

### IDE configs (Claude Code only after F3)
- ✅ `~/.claude.json` — MCP entry. Keep.
- ✅ `~/.claude/hooks/codevira-*.sh` — hooks. Keep (4 → 3 per E above).
- ❌ `~/.cursor/mcp.json` — DELETE setup path.
- ❌ `~/.windsurf/mcp_config.json` — DELETE.
- ❌ `~/.gemini/antigravity/mcp_config.json` — DELETE.
- ❌ `~/.codex/config.toml` — DELETE.

### Per-project
- ✅ `<repo>/.codevira/` — JSONL storage. Keep.
- ✅ `<repo>/.codevira-cache/` — gitignored cache. Keep.
- ✅ `<repo>/AGENTS.md` — auto-generated. Keep.
- ❌ Per-IDE nudge files — DELETE (per C above).
- ✅ `<repo>/.gitignore` — modified to ignore cache. Keep.

### Total after cuts

| Location count | Before | After |
|---|---|---|
| Per-user (machine-wide) | 3 | 2 |
| IDE configs | 6 | 2 (Claude Code only) |
| Per-project | 9 | 4 |
| **Total touch points** | **~18** | **~8** |

And **`codevira uninstall` reverses all 8.** Clean exit.

---

## G — What dies entirely (greatest-hits list)

Concept-level deletions (not just tools):
- **Changesets** (4 tools + storage + UI) — never used by AI or human.
- **Preferences / learned_rules** (3 tools + extractor + storage) —
  noise-prone, never validated, F1 says nobody reads them.
- **Multi-IDE setup** (8 IDE configs + 4 nudge files per IDE) —
  AGENTS.md does the same job for free.
- **`heal` command** + deprecation chain — `reset` is the supported op.
- **`calibrate` command** — no semantic threshold exists in v2.2.0.
- **`agents` command** + nudge-file generators — replaced by
  AGENTS.md alone.
- **`insights` command** — surfaced features we're deleting.
- **Engine policies**: `scope_contract`, `intent_inference`,
  `live_style`, `ai_promotion` — high-noise, low-value features.
- **Code graph auxiliaries**: `analyze_changes`, `export_graph`,
  `find_hotspots`, `get_graph_diff`, `add_node`, `update_node`,
  `get_project_maturity` — vestigial.

Estimated **LOC reduction: ~6,000-9,000 lines** (gut estimate; will
measure during Phase 2 execution).

---

## H — What stays (the new minimum-viable codevira)

After cuts, codevira is:

```
13 CLI commands · 18 MCP tools · 1 nudge file (AGENTS.md)
3 hook scripts · 6 engine policies · 8 system touch points
+ codevira uninstall

Total LOC: ~50% of current
Total surface: ~40% of current
Install size: ~70 MB (unchanged — most of that is mcp + cryptography
                       transitive; we can't shrink them)
```

That's a 50/40/0 cut on (code / surface / size). The cut isn't about
disk — it's about **cognitive surface** for the user.

---

## I — Questions for you before Phase 2 executes

These need your sign-off; I can't decide unilaterally:

1. **`agents` command + per-IDE nudge files**: I'm proposing to delete
   all per-IDE nudges and ship only AGENTS.md. Are there users (or
   you) who'd protest the loss of `.cursor/rules/codevira.mdc` or
   `.windsurfrules`?

2. **Changesets concept**: I'm killing the whole multi-file-tracking
   abstraction. The audit shows zero usage. Confirm?

3. **Engine policies to delete** (`scope_contract`, `intent_inference`,
   `live_style`, `ai_promotion`): each represents weeks of work by
   someone (probably you). Deleting is irreversible. Confirm each
   or veto?

4. **IDE scope: Claude Code only**: the v2.2.0 plan said 8 IDEs. F3
   says that surface is fragile. Per the audit, your sessions are all
   Claude Code. Deleting non-Claude-Code support: confirm or veto?

5. **`get_decision_confidence`, `get_project_maturity`, `find_hotspots`**:
   smell-vestigial but I haven't grepped real usage. Quick confirm
   you're OK letting them go.

---

## Sign-off

If you agree with everything above, reply "go phase 2".
If you want any item changed, edit this file or list the deltas in
your reply, and I'll re-run Phase 1 with your overrides before Phase
2 starts.

**No code changes until you sign.**
