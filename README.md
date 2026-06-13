# Codevira

> **Cross-IDE decision memory for AI coding agents.** One in-repo memory
> layer that every AI tool you use can read — plus PreToolUse hooks that
> physically block violating edits **in Claude Code** (other IDEs get the
> same decisions as AGENTS.md guidance, not a hard block). Local-first,
> MIT-licensed, ~83 MB pipx install.

[![PyPI version](https://img.shields.io/pypi/v/codevira?color=orange)](https://pypi.org/project/codevira/)
[![Python](https://img.shields.io/pypi/pyversions/codevira?color=blue)](https://pypi.org/project/codevira/)
[![Downloads](https://static.pepy.tech/badge/codevira)](https://pepy.tech/project/codevira)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

**Built for solo developers** working on local projects with AI agents.
Decisions live in `<repo>/.codevira/decisions.jsonl` — git-committed,
team-shareable, visible in `git diff`. Every modern AI tool reads
AGENTS.md, which codevira auto-generates as a slim 5 KB contract.
Claude Code gets enforcement: PreToolUse hooks block `Edit`/`Write`
calls that contradict decisions you marked `do_not_revert`.

**Works with:** Claude Code · Claude Desktop · Cursor · Windsurf ·
Google Antigravity · OpenAI Codex · GitHub Copilot · any MCP-compatible
AI tool.

---

## What you get

* 🧠 **One memory across every AI tool.** A decision logged in Claude
  Code is visible to Cursor, Windsurf, Antigravity — all of them read
  the same `.codevira/decisions.jsonl` in your repo. No per-tool
  re-onboarding, no cloud sync.
* 🛡️ **Hard enforcement, not soft hints.** Decisions you mark
  `do_not_revert` get a PreToolUse hook (Claude Code) that physically
  refuses any `Edit`/`Write` call violating them. Other IDEs see the
  decision in AGENTS.md; Claude Code is the only one with hard hooks
  today.
* ⚡ **One-command setup.** `pipx install codevira && codevira setup`.
  Auto-detects installed AI tools (strong signals: binary on PATH +
  valid config file); only configures what's actually installed. Pass
  `--force` when the detector misses an install.
* 🔒 **Local-first, MIT-licensed.** Decisions in
  `<repo>/.codevira/*.jsonl`, code graph in `.codevira-cache/` (rebuildable,
  gitignored), nothing leaves your machine. No SaaS, no account, no
  telemetry.
* 📦 **Slim install (~83 MB pipx venv).** No ChromaDB, no
  sentence-transformers, no torch. FTS5 SQLite for decision search,
  individual tree-sitter grammars (TS/JS/Go/Rust) for the code graph.
  MCP server starts in <100 ms.
* 🔐 **Concurrent-safe under multi-IDE load.** Every on-disk write
  goes through `mcp_server/storage/atomic.py` — crash-safe atomic
  writes + Posix `fcntl.flock` (Windows sentinel fallback). Two
  IDEs hitting the same project don't race on `manifest.yaml` /
  `roadmap.yaml` / `AGENTS.md`. Pinned by an in-process 50-thread
  stress test, a 20-subprocess cross-process stress test, and an
  adversarial chaos harness (`scripts/chaos_smoke.py` — 29 attacks
  including SIGKILL during lock, symlink traversal, malformed
  MCP payloads). See [`docs/architecture.md`](docs/architecture.md)
  § "Concurrent-write safety".

---

## The Problem (Four Pains Codevira Solves)

If you code with AI agents on a project longer than a week, you've felt all of these:

### 1. Re-explaining your codebase every session
Every new chat starts from zero. The AI doesn't know your
architecture, your conventions, your "we don't do it that way"
decisions. You waste the first 10 minutes (and thousands of tokens)
catching it up — only to do it again tomorrow.

### 2. AI undoing your careful decisions
Last week you debugged a tricky retry policy for 3 hours. Today's AI
session refactors it to a simpler version because it has no idea why
the complexity exists. Now it's broken again.

### 3. Cross-tool amnesia
You started planning in Claude Code. Switched to Cursor for
autocomplete. Opened Antigravity to run tests. Three different agents,
three different blind copies of your project state. Nothing carries
over.

### 4. Token budget burned on re-discovery
Your AI agent reads the same 12 files every session before doing any
actual work. You're paying API costs for the same lookups, over and
over.

**Codevira is a persistent memory layer that fixes all four — for
every AI tool, on every project, on your local machine.**

---

## What's new in v3.1.1 — hardening + interrogable memory

> 3.1.1 supersedes the briefly-published 3.1.0 (which shipped
> without this README/CHANGELOG entry). Same code shape; this
> release is the documented one. Brings five memory subsystems
> (M1–M9 from 3.1.0) plus the v3.1.1 hardening + viewer overhaul.

| Area | What you get |
|---|---|
| **Five memory subsystems** | Origin tagging (M1), working memory (M2), skill library with FTS5 ranking (M3), spatial memory + activity heatmap (M4), skill induction wired to outcomes (M5), cross-IDE consensus check + handshake (M6/M7), reflections (M8). 22 new MCP tools. |
| **Secret scrubbing everywhere** | Decisions, sessions, working memory, skills, reflections — every store scrubs api-key / Bearer / password / AWS AKIA / long hex / long base64 at the write boundary. One shared `mcp_server/storage/sanitize.py`. |
| **Counter-decision discipline** | `record_decision` now accepts `alternatives_considered: list[str]` and `would_re_examine_if: str` — losing options + invalidation trigger surface in the viewer's rich-detail panel. Optional + back-compat. |
| **Interrogable graph viewer** | `codevira graph` is no longer a passive force-layout. Free-text search → top-K ranked panel with score + outcome badge. Q&A intent detection ("what did we decide about X", "what got reverted", "what's protected"). Outcome lens (kept/modified/reverted). Lineage trace mode for supersession chains. |
| **Auto outcome classification** | `codevira sync` now runs `observe-git` as a best-effort tail step — outcomes flow into the viewer's outcome lens automatically. |
| **G3 real-IDE smoke** | The last permanently-skipped gauntlet gate now ships as a real check. Verifies codevira is registered in each detected IDE config + MCP `tools/list` round-trips in <1s. |
| **AGENTS.md no more churn** | `regenerate()` is now idempotent — no rewrite when content unchanged, no perpetual uncommitted-drift loop. |
| **4 silent bugs fixed** | `commit_session("../escape")` rejected; `triggers.tags="git"` rejected; `list_all(limit=0)` returns `[]`; spatial BFS catches query-time sqlite errors. |

Full v3.1.1 release notes: [CHANGELOG.md](CHANGELOG.md#311--2026-05-30--hardening-viewer-overhaul-g3-sync-observe-git).

---

## What's new in v3.0.0 — audited, lean, opinionated

> Major version. v3.0.0 is the biggest API contraction since v2.0
> shipped: 21 MCP tools deleted, 8 CLI subcommands deleted, per-IDE
> nudge file matrix collapsed to AGENTS.md only, IDE detection
> hardened from "directory exists" to "binary on PATH + valid config
> file." Full plan + rationale in
> [`docs/audit-2026-05-22.md`](docs/audit-2026-05-22.md);
> per-item kill list in
> [`docs/surface-cuts-2026-05-22.md`](docs/surface-cuts-2026-05-22.md).

| Area | What changed |
|---|---|
| **Decision storage** | Moved into `<repo>/.codevira/decisions.jsonl` (git-tracked, one decision per line). v2.x stored decisions in `~/.codevira/projects/<key>/graph/graph.db` (a SQLite blob nobody could read). |
| **AGENTS.md is the nudge file** | Per-IDE nudges (CLAUDE.md / GEMINI.md / .windsurfrules / .cursor/rules/codevira.mdc / .github/copilot-instructions.md) all deleted. Every modern AI tool reads AGENTS.md natively. Slim 5 KB cap; auto-regenerated from decisions; user content outside the `<!-- codevira:begin -->...<!-- codevira:end -->` markers preserved byte-for-byte. |
| **MCP tool surface** | 46 → 24 tools (-48%; 23 surfaced to AI clients, 1 admin-only). The audit found 22 tools that nobody called in real usage (preferences, learned_rules, changesets, project_maturity, list_nodes / add_node / update_node / export_graph, etc.). All deleted. (v3.1.0 later added 24 memory-subsystem tools, and v3.3.0 brought preferences back redesigned — `distill_preferences` / `search_preferences`, LLM-distilled from real prompts instead of the unused v2.x CRUD.) |
| **CLI surface** | 23 → 15 commands (-35%). Deleted: `heal` (use `reset`), `budget`, `agents`, `hooks`, `register`, `configure`, `report` (folded into `doctor`), `calibrate`, `insights`. |
| **IDE detection hardened** | No more false-positives from stale `~/.cursor/` dirs. Each IDE needs a STRONG signal (binary on PATH, or verified config file). Pass `--ide X --force` to override when the detector misses an install. |
| **`codevira uninstall`** | New command. Reverses every system write made by `init`/`setup`: drops the MCP entry from `~/.claude.json`, deletes `~/.claude/hooks/codevira-*.sh`, strips codevira-tagged registrations from `~/.claude/settings.json`, removes per-project `.codevira/` + `.codevira-cache/`, strips the codevira block from AGENTS.md. Optional `--keep-data`. |
| **Install size** | ~83 MB pipx venv (was ~450 MB in v2.1.2 with ChromaDB + sentence-transformers + torch). MCP server starts in <100 ms (was 1-3 s). |

Full v3.0.0 release notes: [CHANGELOG.md](CHANGELOG.md).

Upgrading from v2.x? See the [Migration notes](CHANGELOG.md#migration-notes) section.

---

## Quick Start — three commands

```bash
# 1. Install
pipx install codevira

# 2. Bootstrap the project (writes .codevira/, AGENTS.md, .gitignore)
cd ~/Projects/my-project
codevira init

# 3. Wire codevira into every AI tool detected on this machine
codevira setup
```

Then commit `.codevira/` + `AGENTS.md` + `.gitignore` to git so your
teammates inherit the project memory. Open any IDE; codevira's MCP
server is ready.

**Verify:**

```bash
codevira doctor          # 11-ish health checks, ✓/⚠/✗
codevira list-decisions  # any decisions recorded yet?
codevira sync            # regen AGENTS.md from current decisions.jsonl
```

**Try it.** In your AI tool, ask: *"Use `get_session_context` to brief
me on this project."* You'll get a structured project state in one
tool call instead of the AI re-reading docs.

### What `codevira setup` actually does

The one command above replaces what used to take 5+ steps in v1.x:

* **Detects installed AI tools** via STRONG signals:
  - **Claude Code**: `claude` on PATH
  - **Claude Desktop**: `~/Library/Application Support/Claude/config.json` exists + parses
  - **Cursor**: `~/.cursor/` + (`cursor` on PATH OR `mcp.json` exists)
  - **Windsurf**: `mcp_config.json` in `~/.windsurf/` or `~/.codeium/windsurf/`
  - **Antigravity**: `~/.gemini/antigravity/mcp_config.json`
* **Injects MCP server config** into each detected tool's config file
  (per-IDE schema handled automatically; no JSON to hand-edit).
* **Installs Claude Code lifecycle hooks** (`SessionStart`,
  `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`) — these are
  what turn codevira from passive memory into the active guardian that
  blocks edits violating `do_not_revert` decisions.
* **Writes AGENTS.md** with the slim codevira-managed block (5 KB
  cap; preserves user content outside the marker boundaries).

Flags:

- `--dry-run` — preview without writing
- `--ide <name>` — narrow to one IDE (`claude`, `claude_desktop`,
  `cursor`, `windsurf`, `antigravity`, `agents_md`)
- `--force` — configure an `--ide` value even if codevira didn't
  auto-detect it (escape hatch for portable binaries / unusual config
  locations)
- `-y` / `--yes` — skip the confirmation prompt
- `--no-hooks` / `--no-mcp` / `--no-nudge-files` — scope-narrow the
  steps

### What `codevira doctor` reports

11 health checks in one run, each with a concrete `fix_command` for
any WARN or FAIL. Read-only — never modifies anything.

```text
$ codevira doctor
Codevira health check
────────────────────────────────────────────────────────────
✓  python_version         Python 3.13 (≥ 3.10 required)
✓  codevira_data_dir      /Users/you/.codevira exists and is writable
✓  project_root           /Users/you/Projects/my-project is a valid project root
✓  codevira_dir           .codevira/ present (4 decision(s))
✓  agents_md_size         AGENTS.md is 1,234 bytes (≤10 KB safety threshold)
✓  graph_db               graph.db has all 4 expected tables
✓  global_db              ~/.codevira/global.db opens cleanly
✓  detected_ides          2 AI tool(s) detected: claude, cursor
✓  nudge_files            AGENTS.md present with codevira block
✓  watcher_circuit        watcher circuit clean (no recent failures)
✓  engine_kill_switch     engine ON (default; CODEVIRA_ENGINE not set)
✓  claude_mcp_visibility  codevira visible to Claude Code
✓  crash_log_size         no crash log (clean state)
────────────────────────────────────────────────────────────
summary: 13 pass · 0 warn · 0 fail
```

### Daily-use commands

The v3.0.0 CLI surface is 15 commands:

| Command | What it does |
|---|---|
| `codevira init` | Bootstrap `.codevira/` + AGENTS.md + .gitignore in this project |
| `codevira setup` | Detect installed AI tools + write MCP configs + Claude Code hooks |
| `codevira doctor` | Health check (read-only; ✓/⚠/✗ + fix commands) |
| `codevira status` | Show index health + project state |
| `codevira projects` | List tracked projects with staleness (`today` / `5d ago` / `stale 45d`); `projects archive <name>` drops one from the registry |
| `codevira index` | Build / refresh the code graph cache |
| `codevira sync` | Regenerate AGENTS.md + manifest + digest from `decisions.jsonl` |
| `codevira observe-git` | Classify past decisions as kept/modified/reverted from git history |
| `codevira replay` | Browse the decisions timeline (terminal / markdown / HTML) |
| `codevira clean` | Remove orphaned project data |
| `codevira reset` | Destructive cleanup (auto-exports decisions first) |
| `codevira export` | Standalone decision backup (JSON / SQL); `export setup` bundles project memory + global learning for machine transfer |
| `codevira import` | Restore a `codevira export setup` archive on a new machine (merges global learning) |
| `codevira graph` | Render an interactive, self-contained HTML viewer of decision memory (offline, queryable) |
| `codevira uninstall` | Reverse every system write codevira made (see ["Uninstall"](#uninstall)) |
| `codevira serve` | Start MCP HTTP server (single-project; stdio is the daily mode) |
| `codevira engine` | Internal — invoked by Claude Code lifecycle hook scripts |

Run `codevira <cmd> --help` for the full flag list on any subcommand.

### Uninstall

```bash
# Reverse every system write made by init/setup. Preserves user content
# outside codevira marker blocks byte-for-byte.
codevira uninstall

# Common flags:
codevira uninstall --dry-run     # preview the plan; touch nothing
codevira uninstall --yes          # skip confirmation
codevira uninstall --keep-data    # uninstall the binary's footprint but
                                  # leave ~/.codevira/ and per-project
                                  # .codevira/ dirs alone
# Then remove the binary:
pipx uninstall codevira
```

Uninstall also strips legacy v2.1.x per-IDE nudge files (CLAUDE.md /
GEMINI.md / .windsurfrules / .cursor/rules/codevira.mdc /
.github/copilot-instructions.md) for users upgrading from older
versions. User content outside the codevira markers stays.

---

## How It Works

Codevira is a [Model Context Protocol](https://modelcontextprotocol.io)
server that runs locally and gives any AI tool a structured, queryable
memory of your codebase.

```
┌─────────────────────────────────────────────────────────────────┐
│  IN THE PROJECT REPO (committed to git)                         │
│                                                                 │
│   AGENTS.md                  ≤5 KB slim contract, auto-generated │
│      ↑                                                          │
│      │                                                          │
│   .codevira/                                                    │
│     decisions.jsonl          full text + metadata (append-only) │
│     digest.jsonl             slim summary for prompt injection  │
│     outcomes.jsonl           kept/reverted from git observation │
│     manifest.yaml            tag→ids, file→ids index (regen)    │
│     enforcement.yaml         which decisions hard-block         │
│     config.yaml              project settings                   │
│     sessions.jsonl           session events                     │
│     roadmap.yaml             phase tracking                     │
│                                                                 │
│   .codevira-cache/           gitignored, rebuildable             │
│     graph.sqlite             code graph (tree-sitter)           │
│     fts5.sqlite              FTS5 index over decisions.jsonl    │
│     hash-cache.db            file change detection              │
└─────────────────────────────────────────────────────────────────┘
                              ↑ MCP / hooks
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  PIPX INSTALL (~83 MB venv, ~/.local/pipx/venvs/codevira)       │
│                                                                 │
│   codevira (CLI + MCP server)                                   │
│      - pure Python, <100 ms startup                             │
│      - no chromadb / sentence-transformers / torch              │
└─────────────────────────────────────────────────────────────────┘
                              ↑ stdio MCP
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  IDE (Claude Code / Cursor / Windsurf / Antigravity / Codex /…) │
│                                                                 │
│   UserPromptSubmit → codevira hook → relevance-gated inject     │
│   Edit / Write → PreToolUse → block if do_not_revert violated   │
│   Stop → PostToolUse → optional outcome tracking                 │
└─────────────────────────────────────────────────────────────────┘
```

### Token-efficient by design

Codevira is built around the principle that AI agent context windows
are precious. Tools return **summaries by default** with opt-in full
data:

- `get_node(path)` — ~100 tokens by default (counts + flags). Pass
  `full=true` for the entire rules array.
- `get_impact(path)` — 10 affected files. Pass `summary_only=true`
  for just counts (~80 tokens) before deciding to dig deeper.
- `search_decisions(query)` / `list_decisions()` — truncated matches
  by default. Pass `full=true` for verbatim text; pass
  `summary_only=true` for just IDs and one-line summaries.

The agent always asks for what it needs, in the size it needs.

**Shrink the tool surface itself.** The advertised MCP `tools/list` is a
fixed per-session cost (~4K tokens for the full 24-tool surface). Set
`CODEVIRA_TOOL_PROFILE=lean` in the MCP server's `env` block to advertise
only the 11 daily-driver tools (~46% smaller); hidden tools still work
when called explicitly. Bigger wins usually come from disabling MCP
servers you aren't actively using.

---

## MCP Tools

**49 tools** surfaced to AI clients via `tools/list` (token-optimized,
summary-first): the core decision/roadmap/graph surface plus the
v3.1.0 memory subsystems (working memory, skills, spatial, consensus,
reflections) and the v3.3.0 preference tools. One additional admin
tool (`refresh_graph`) is registered but hidden from `tools/list`
because it runs automatically in the background — humans invoke it via
`codevira sync`. Set `CODEVIRA_TOOL_PROFILE=lean` to advertise only
the daily-driver subset. v3.0.0 cut 21 v2.x tools that produced noise
or had no real users — see [CHANGELOG.md](CHANGELOG.md) for the full
kill list.

### Reads — the memory surface

| Tool | Description |
|---|---|
| `get_session_context` | **THE main "catch me up" call.** Returns ~500 tokens: current phase, next action, recent decisions, top tags, last session brief. |
| `search_decisions(query)` | FTS5 BM25 search over `decisions.jsonl`. Default: top 5 truncated; pass `full=true` or `summary_only=true`. |
| `list_decisions` | Paginate / filter: `since_date`, `file_pattern`, `protected_only`, `tags`, `include_superseded`. |
| `list_tags` | Enumerate all tags with decision counts. |
| `get_history(file_path)` | Recent decisions touching a file. |
| `check_conflict(decision_text)` | Surface duplicate / contradictory decisions BEFORE you write. |

### Writes — capturing decisions

| Tool | Description |
|---|---|
| `record_decision` | Capture an architectural decision. Optional `do_not_revert=true` triggers hard enforcement at the Claude Code PreToolUse hook. Supports `tags`, `force`, `session_id`. |
| `supersede_decision(old_id, new_decision, reason)` | Retire an old decision and link to its replacement. Audit trail preserved. |
| `reaffirm_decision(decision_id)` | Re-confirm a soft-expired `do_not_revert` lock is still wanted. |
| `set_decision_flag(decision_id, ...)` | Toggle `do_not_revert` / tags on an existing decision. |
| `write_session_log` | Structured session record. |

### Roadmap

| Tool | Description |
|---|---|
| `get_roadmap` | Current phase, next action, upcoming phases. |
| `get_phase(number)` | Full details of any phase. |
| `add_phase` | Queue new upcoming work. |
| `update_phase_status` | Mark in_progress / blocked. |
| `update_next_action` | Set what the next agent should do. |
| `complete_phase` | Mark done + record key_decisions. |
| `defer_phase` | Move a phase to the deferred list. |
| `bulk_import_phases` | One-call import of multi-phase git history. |

### Code graph (Python + TS/JS + Go + Rust via tree-sitter)

| Tool | Description |
|---|---|
| `get_node(file_path)` | File-level metadata (role, layer, stability, dependencies). |
| `get_impact(file_path)` | Blast radius — who depends on this file. |
| `query_graph(file_path, query_type)` | Function-level: callers, callees, tests, dependents, symbols. |
| `refresh_graph(file_paths?)` | Background reindex (fire-and-forget). |
| `get_signature(file_path)` | All public symbols, signatures, line numbers. |
| `get_code(file_path, symbol)` | Full source of one function or class. |
| `get_playbook(task_type)` | Curated rules for: `add_tool`, `add_service`, `add_schema`, `debug_pipeline`, `commit`, `write_test`. |

### Memory subsystems (v3.1.0)

| Subsystem | Tools | What it covers |
|---|---|---|
| Working memory | `working_add`, `working_get`, `working_promote`, `get_working_context` | Intra-session scratchpad (decay-scored, capacity-bounded). |
| Skill library | `record_skill`, `get_skill`, `apply_skill_outcome`, `list_skills`, `supersede_skill`, `promote_skill_to_playbook` | Reusable procedures with composite-ranked retrieval and git-driven reinforcement. |
| Spatial | `spatial_nearby`, `spatial_heat`, `spatial_neighborhood`, `spatial_affordances` | Code-as-space: activity heatmap, neighborhoods, what task types apply where. |
| Consensus | `consensus_check`, `consensus_status`, `consensus_propose_supersession`, `consensus_resolve`, `origin_of` | Cross-IDE conflict detection + provenance. |
| Reflections | `reflect`, `get_reflections`, `list_reflections` | LLM-generated abstractions over recent decisions + sessions (MCP sampling). |

### Preferences (v3.3.0)

| Tool | Description |
|---|---|
| `distill_preferences` | Session-end LLM distillation of captured prompts into durable, user-scoped preferences (`~/.codevira/global.db`). |
| `search_preferences(category?)` | Retrieve learned preferences — communication style, workflow habits — across all projects. |

### MCP Workflow Prompts

| Prompt | Description |
|---|---|
| `onboard_session` | Full project context catch-up for new sessions. Wraps `get_session_context()`. |

> v3.0.0 removed 4 v2.x prompts (`review_changes`, `debug_issue`,
> `pre_commit_check`, `architecture_overview`) because they referenced
> deleted MCP tools. The slim surface means the AI can synthesize
> these workflows from the kept tools directly.

---

## Language support

| Feature                       | Python | TS/JS | Go | Rust | Others |
|-------------------------------|:------:|:-----:|:--:|:----:|:------:|
| Decision capture + search     | ✓      | ✓     | ✓  | ✓    | ✓      |
| Cross-IDE memory via AGENTS.md| ✓      | ✓     | ✓  | ✓    | ✓      |
| Roadmap / sessions            | ✓      | ✓     | ✓  | ✓    | ✓      |
| Code graph + blast radius     | ✓      | ✓     | ✓  | ✓    | —      |
| `get_signature` / `get_code`  | ✓      | ✓     | ✓  | ✓    | —      |

Decisions / AGENTS.md / roadmap are language-agnostic. Code-graph
features require a tree-sitter grammar; v3.0.0 ships TS, JS, Go, Rust.
The opt-in extra `pip install 'codevira[all-languages]'` re-adds the
17-grammar pack for Java, Ruby, PHP, C, C++, Kotlin, Swift, etc.

---

## Production-stable vs known-limited

| Production-stable | Known-limited |
|---|---|
| Cross-IDE decision memory via in-repo JSONL | The PreToolUse hook enforcement is Claude Code only today. Other IDEs read AGENTS.md (soft signal), but don't have hard blocks |
| `do_not_revert` enforcement at Claude Code PreToolUse | Multi-language code graph for languages outside Python / TS / JS / Go / Rust — use the `[all-languages]` extra |
| FTS5 decision search with BM25 ranking | Real-time multi-machine sync — by design, codevira is local-first; for team sharing, commit `.codevira/` to git |
| Per-project + cross-machine project inventory (`global.db`) | Web UI for browsing decisions — use the `codevira://decisions` MCP resource in Claude Desktop, or `codevira replay --format html` for a static file |
| All 24 MCP tools (23 AI-facing + 1 admin) + 15 CLI commands + 6 engine policies | The HTTP server (`codevira serve`) is single-project per launch — for daily use, stick with stdio via `codevira setup` |
| Concurrent-safe storage layer (Posix `fcntl.flock` + Windows sentinel fallback). Proven against 50-thread + 20-subprocess stress + 29-attack chaos harness | The cross-process file-lock contract has been exercised on macOS + Linux CI; the Windows sentinel-file fallback is verified via unit-test simulation but hasn't been load-tested on real Windows yet |
| Code graph data store is functional but the v3.0.0 spec target (`<project>/.codevira-cache/graph.sqlite`) and the actual location (`<data_dir>/graph/graph.db`) drifted during the surface-cut audit. Tracked for v3.1 reconciliation | n/a (functional today; spec-truthfulness gap only) |

---

## Background

Want to understand the full story behind why this was built, the
design decisions, what didn't work, and how it compares to other tools
in the ecosystem?

Read the full write-up:
[How I Built Persistent Memory for AI Coding Agents](docs/how-i-built-persistent-memory-for-ai-agents.md)

---

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for
the full guide.

- **Reporting a bug?** [Open a bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md)
- **Requesting a feature?** [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md)
- **Found a security issue?** Read [SECURITY.md](SECURITY.md) — please
  don't use public issues for vulnerabilities.

---

## FAQ

Common questions about setup, usage, architecture, and troubleshooting
— see [FAQ.md](FAQ.md).

## Roadmap

See what's built, what's next, and the long-term vision — see
[ROADMAP.md](ROADMAP.md).

## Star History

If Codevira saves you tokens or sanity, a star helps other developers
find it.

<a href="https://star-history.com/#sachinshelke/codevira&Date">
  <img src="https://api.star-history.com/svg?repos=sachinshelke/codevira&type=Date" alt="Star History Chart" width="600"/>
</a>

## License

MIT — free to use, modify, and distribute.
