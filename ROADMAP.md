# Codevira — Project Roadmap

**Local-first persistent memory for AI coding agents.**

Codevira gives every AI coding tool — Claude Code, Cursor, Windsurf, Google Antigravity, or any MCP-compatible agent — persistent memory that survives across sessions, learns from developer behavior, and works on any project in any language.

**Built for solo developers.** All memory lives on your machine in `~/.codevira/`. No cloud, no accounts, no team sharing (yet). One install handles every project you work on.

**Vision:** Install once. Register once. Every project on your machine gets intelligent memory automatically. No config files, no manual setup, no vendor lock-in.

Have a suggestion? [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md).

---

## ✅ v1.0 — Foundation (March 2026)

Everything needed to give AI agents persistent memory on a single project.

- **26 MCP tools** across graph, roadmap, changeset, search, and code reader modules
- **Context graph** — YAML-based file nodes with rules, stability, blast-radius BFS
- **Semantic code search** — ChromaDB + sentence-transformers, fully local
- **Roadmap system** — phase-based tracker with auto-stub on first use
- **Changeset tracking** — atomic multi-file change management with session resume
- **Decision log** — structured session logs, searchable by future agents
- **Agent personas** — 7 built-in roles (Orchestrator, Planner, Developer, Reviewer, Tester, Builder, Documenter)
- **Config-driven setup** — `config.yaml` for watched dirs, language, collection name
- **Auto-reindex git hook** — incremental re-index on every commit

---

## ✅ v1.2 — Language Expansion & Persistence Overhaul (March 2026)

Full feature support beyond Python. High-performance storage layer.

- **Tree-sitter integration** — AST-based chunking for TypeScript, Go, Rust, and 16+ languages via `tree-sitter-language-pack`
- **`get_signature` / `get_code` for all languages** — symbol extraction via tree-sitter, not just Python AST
- **SQLite graph database** — migrated from hundreds of YAML files to a single `graph.db`
- **SQLite memory & session logs** — agent sessions and decisions stored in `graph.db` tables
- **Blast-radius via recursive CTEs** — instant SQL-based dependency traversal
- **SHA-256 hash-based indexing** — skip unmodified files, sub-second incremental reindex
- **Live auto-watch** — background file watcher auto-starts with MCP server; 2-second debounce

---

## ✅ v1.3 — Developer Experience (March 2026)

Make setup and daily use smoother.

- **`codevira` CLI** — single entry point: `codevira init`, `codevira status`, `codevira index`
- **Progress bar for indexing** — visual feedback during `--full` builds on large codebases
- **Index health dashboard** — `codevira status` shows stale files, graph coverage, last indexed
- **Global install support** — `pipx install codevira` works across all projects without per-project virtual environments

---

## ✅ v1.4 — Living Memory (March 2026)

Transform from static knowledge base into adaptive memory that learns from developer behavior.

- **Real dependency graph** — wired up `extract_imports()` → `add_edge()` pipeline; `get_impact()` now returns actual blast-radius results
- **Tree-sitter import resolution** — TypeScript/JS relative imports, Go packages, Rust use paths resolved to file paths
- **Graph visualization** — `export_graph()` generates Mermaid or DOT dependency diagrams
- **Graph diff on PR** — `get_graph_diff()` shows changed nodes, stability flags, and union blast radius
- **Outcome tracking** — git-based feedback loop classifies agent changes as kept/modified/reverted
- **Confidence scoring** — `get_decision_confidence()` returns outcome-based reliability scores
- **Developer preference learning** — `get_preferences()` learns coding style from post-edit corrections
- **Automatic rule learning** — `get_learned_rules()` infers test pairing, import hotspots, co-change patterns
- **Session handoff** — `get_session_context()` single "catch me up" call for cross-tool continuity
- **33 MCP tools** + 7 new learning and graph tools (up from 26)

---

## ✅ v1.5 — Zero-Config + Deep Graph Intelligence (April 2026)

Make Codevira instant to set up and intelligent across all projects.

- **Zero-config init** — auto-detects language, source dirs, file extensions from project markers (15+ languages); no interactive prompts
- **Smart directory scanning** — scans actual project tree for source files instead of relying on fixed folder conventions; skips known noise dirs (`node_modules`, `.venv`, `build`, etc.)
- **IDE auto-inject** — writes MCP config directly into Claude Code, Cursor, Windsurf, and Google Antigravity on `init`; non-destructive merge preserves existing settings
- **Reliable binary resolution** — finds `codevira` binary across PATH, pipx venvs, pip --user, and sibling bin; falls back to `python -m mcp_server` if needed
- **Cross-project global memory** — `~/.codevira/global.db` aggregates preferences and rules across all projects; imported on startup with confidence decay
- **Optional ML dependencies** — base install is lightweight (~50MB); `pip install 'codevira[search]'` adds ChromaDB + sentence-transformers for semantic search
- **Function-level call graph** — `symbols` + `call_edges` tables; knows which function calls which, across files
- **3 new tools**: `query_graph()` (callers/callees/tests), `analyze_changes()` (function-level risk scoring), `find_hotspots()` (complexity heatmap)
- **5 MCP workflow prompts** — `review_changes`, `debug_issue`, `onboard_session`, `pre_commit_check`, `architecture_overview`
- **36 MCP tools + 5 prompts**

---

## ✅ v1.6 — True Zero-Friction: No Init, No Config, Just Works (April 2026)

**The big shift.** Eliminate every manual step between install and working memory.

Today a developer must: install → `cd project` → `codevira init` → restart IDE → repeat per project. v1.6 reduces this to: install → done.

### One-Time Global Registration
- Single MCP entry in each IDE config: `{"command": "codevira"}` — no project path, no `cwd` override, no per-project config files
- Works for every project the developer opens, forever
- `codevira init` becomes optional (power-user override for custom settings)

### Auto-Init on First Use
- When an AI tool calls any Codevira tool, the server detects the project from `cwd`
- If the project isn't indexed yet, background thread starts indexing immediately
- Tools return partial/minimal results while indexing progresses
- First `get_roadmap()` call on a brand-new project responds within milliseconds

### Centralized Storage
- All project data lives under `~/.codevira/projects/<key>/` instead of `.codevira/` inside each repo
- Keyed by absolute path, with git remote URL as secondary key (survives directory moves)
- No `.codevira/` polluting project directories, no `.gitignore` entry needed
- Projects that want shared team rules can opt into an in-repo `.codevira/rules/` overlay

### .gitignore-Aware File Discovery
- **Flip the model**: instead of "detect which folders to watch", watch everything and exclude what's noise
- Respect `.gitignore` + nested `.gitignore` files — the developer already maintains this
- Everything not ignored is indexed: `.ts`, `.css`, `.json`, `.prisma`, `.graphql`, `.sql`, `.md`, `.yaml`, `.env.example`
- Language label inferred from dominant file type — only used for tree-sitter parser selection, not for filtering

### Background Indexing
- Indexing runs in a background thread, never blocks tool responses
- Progressive availability: roadmap and graph tools work immediately, search tools become available as chunks are embedded
- Status indicator: `get_session_context()` includes indexing progress

### Distribution
- **Publish to PyPI** — `pipx install codevira` works for anyone worldwide
- **List on MCP registries** — Anthropic MCP registry, Cursor marketplace, Windsurf plugin store

---

## ✅ v1.7 — Token Efficiency & AI-First Tool Design (current — April 2026)

The biggest design shift since v1.0. v1.6 made setup invisible; v1.7 makes the runtime efficient.

### The problem v1.7 solves
Earlier versions returned bulk data when AI agents asked for context. A single `list_nodes()` call could dump 60,000 tokens. The "92% token reduction" value prop was being defeated by the very tools meant to deliver it.

### Token-efficient tool responses
Every high-traffic tool now returns a **summary by default** with opt-in full data:
- `get_session_context()` — compacted to ~800 tokens (was 4k+)
- `get_node(path)` — counts + flags (~100 tokens), `full=true` for arrays
- `get_impact(path)` — 10 affected files default, `summary_only=true` for ~80 tokens
- `search_codebase(q)` — file/symbol pointers only, `include_content=true` to inline source
- `search_decisions(q)` — 5 truncated matches, `full=true` for verbatim
- `get_history(file)` — 5 truncated matches, `full=true` for verbatim
- `get_full_roadmap()` — completed phases summarized, `include_decisions=true` for full

### AI-facing tool surface trimmed (36 → 23)
12 admin/dashboard tools hidden from `list_tools()` but still callable via dispatch:
- Bulk discovery (use targeted queries instead): `list_nodes`, `add_node`
- Background automation (self-managed): `refresh_graph`, `refresh_index`
- Dashboards/reports (CLI only): `get_full_roadmap`, `get_project_maturity`, `find_hotspots`, `analyze_changes`, `get_graph_diff`, `export_graph`
- Redundant with `get_session_context`: `get_preferences`, `get_learned_rules`

### Quality
- Default install includes ChromaDB + sentence-transformers (was `[search]` extra)
- `refresh_index` is now non-blocking (was hanging agents on large projects)
- `codevira clean` command for full uninstall
- Antigravity config + global mode fixed
- Browser-friendly HTML landing at `GET /` on HTTP server
- 1,304 tests passing

---

## 🔜 v1.8 — Multi-Project HTTPS & Diagnostics

The **top priority** for v1.8 is making HTTPS transport match what stdio already delivers: **one server, every project, no per-project setup.**

### Multi-project HTTPS (the big one)

Today (v1.7): the HTTPS server binds to ONE project at `codevira serve` startup. Users running multiple projects need either multiple servers on multiple ports, or just stick with stdio. This misaligns with Codevira's "one memory layer for every project" promise.

v1.8 plan:
- **Read `rootUri` from MCP `initialize` handshake** — each AI session that connects via HTTPS identifies its project, server routes the session accordingly
- **Per-session project context** — the server maintains separate state for each connected project
- **`codevira register --autostart` returns** — installs a launchd service that runs a multi-project HTTPS server on login; works for every project the developer opens forever (same guarantee as stdio)
- **`codevira serve` deprecation of `--project-dir`** for use with `--install-service` — warn that it pins the server to one project

Until v1.8 ships:
- **stdio** (`codevira register`) is the correct choice for multi-project work
- **HTTPS** (`codevira serve`) remains available as a preview for single-project deployments (Claude.ai, headless, one-project focus)

### Other v1.8 items

- **Config validation on init** — warn on common mistakes (malformed `file_extensions`, watched_dirs that don't exist, etc.)
- **`codevira doctor`** — diagnostic command that checks: PATH, IDE configs, project initialization, graph health, common misconfigurations
- **Better tool descriptions** — embed concrete usage patterns in MCP tool descriptions so agents pick the right tool first time

---

## 🔜 v1.9+ — Solo Developer Power-Ups

Stay local. Stay focused on the one developer working on their machine.

- **Multi-project search** — `search_decisions("retry policy")` across ALL your local projects, not just current
- **Project switcher** — `codevira switch <project>` for explicit per-project work outside an IDE
- **Decision import/export** — share your `~/.codevira/global.db` between machines (laptop ↔ desktop) without cloud
- **Custom MCP tool plugins** — define project-specific tools (e.g. `get_db_schema`) without forking Codevira
- **Better Mermaid export** — interactive HTML diagram with click-to-drill-down (instead of static Mermaid text)
- **VS Code extension** — for VS Code users who don't have Claude Code installed; same MCP server, native UI

---

## ✅ v3.0.0 — Audit, lean, opinionated (May 22 2026)

Major version. Biggest API contraction since v2.0 shipped — driven
by a 2026-05-22 surface-cut audit that traced 5 categories of user
complaints to overgrown surface, false-positive IDE detections, and
"junk left behind" after uninstall. All five categories fixed.

Headline cuts (v2.1.x → v3.0.0):

| Surface                       | v2.1.x  | v3.0.0    | Δ      |
|-------------------------------|---------|-----------|--------|
| MCP tools                     | 46      | 24        | -48%   |
| CLI subcommands               | 23      | 15        | -35%   |
| Engine policies               | 10      | 6         | -40%   |
| Per-project nudge files       | 6       | 1         | -83%   |
| MCP prompt library            | 5       | 1         | -80%   |
| Pipx install size             | ~450 MB | ~83 MB    | -82%   |
| MCP server startup            | 1–3 s   | <100 ms   | -97%   |

Key changes:

- **IDE auto-detection hardened.** STRONG signals only (binary on
  PATH + valid config file). v2.x weak signals (empty `~/.cursor/`
  dir, parent of Claude Desktop's config) deleted. New ``--force``
  flag is the escape hatch for the rare case detection misses an
  install. ``--ide cursor`` on a Cursor-less machine no longer
  silently fails — it raises a clear error.
- **Per-IDE nudge files collapsed to AGENTS.md only.** Setup writes
  exactly one nudge file regardless of detected IDEs. Per-IDE
  duplicates (`CLAUDE.md` / `GEMINI.md` / `.windsurfrules` etc.)
  were pure surface bloat.
- **`codevira uninstall` shipped.** Reverses every system write
  made by `init`/`setup`. Optional `--keep-data`. Closes the
  "uninstalling left junk" complaint.
- **21 MCP tools deleted** across changesets, preferences,
  learned_rules, project_maturity, list_nodes / add_node /
  update_node / export_graph, batch endpoints, mark_decision_protected
  (use `supersede_decision(..., do_not_revert=True)` for the same
  flip + audit trail).
- **8 CLI subcommands deleted**: heal (use `reset`), budget, agents,
  hooks, register, configure, report (folded into doctor), calibrate.
- **4 engine policies deleted**: LiveStyleEnforcement,
  AIPromotionScore, ProactiveIntentInference,
  ProactiveScopeContractLock.
- **Dead-code sweep**: ~3,800 LOC removed after the audit surfaced
  internal helpers + tests for deleted features. `indexer/rule_learner.py`
  deleted. 7 SQLiteGraph methods deleted (tables stay for back-compat).
  `mcp_server/engine/signals.py::preferences` (broken — imported a
  non-existent symbol) deleted.

### Pre-publish hardening (RC audit rounds 2 + 3)

The release candidate went through two additional audit passes
before shipping to PyPI. Both surfaced classes of silent failures
the structured test suite hadn't covered:

- **Round 2** (concurrent-write race). A 50-thread stress run of
  `record_decision` exposed `[Errno 2]` warnings on the cache files
  (`manifest.yaml`, `digest.jsonl`, `AGENTS.md`) — fixed-suffix
  `.tmp` rename race + unlocked read-modify-write on the manifest.
  Decisions stayed safe in JSONL (canonical append-only path was
  always fcntl-locked); only the cache files lost data.
- **Round 3** (sweep + harden). Found the same race shape in
  `roadmap.yaml` and 11 more unguarded write sites. Consolidated
  all 16 writers through a single `mcp_server/storage/atomic.py`
  helper (atomic write + Posix/Windows file lock). Proved
  cross-process safety with 20 subprocesses. Added
  `scripts/chaos_smoke.py` adversarial harness — 29 attacks pass.

Two regression test files pin the invariants:
`tests/storage/test_concurrent_writes.py` (in-process) and
`tests/storage/test_cross_process_writes.py` (subprocesses).

Full v3.0.0 plan + RC-audit story: [`docs/plans/v3.0.0.md`](docs/plans/v3.0.0.md).
Full surface-cut audit: [`docs/audit-2026-05-22.md`](docs/audit-2026-05-22.md)
+ [`docs/surface-cuts-2026-05-22.md`](docs/surface-cuts-2026-05-22.md).
Full v3.0.0 entry in [CHANGELOG.md](CHANGELOG.md).

### Known limitations carried into v3.0.0

- **Graph spec vs implementation drift.** The v3.0.0 spec
  documents the code graph at `<project>/.codevira-cache/graph.sqlite`,
  but `indexer/` still writes to `<data_dir>/graph/graph.db`
  (centralized v1.6 location). Runtime is correct — everyone
  agrees on the centralized location — but the spec-truthfulness
  gap should be reconciled in v3.1 with a migration step.
- **No decision-input sanitization.** Null bytes, 1 MB text, path
  traversal in `file_path`, control chars are accepted today.
  Trust-the-agent design choice; v3.1 candidate for hardening.
- **Cross-process flock tested on macOS + Linux CI only.** Windows
  uses the sentinel-file fallback (proved by a monkey-patched
  `sys.platform` unit test) but hasn't been exercised on real
  Windows under load.

---

## ✅ v2.2.0 — Lean (May 2026)

Architectural pivot. ~3,800 LOC deleted, ~150 MB runtime deps removed.

After v2.1.2's 64 GB HNSW corruption exposed ChromaDB's structural
fragility, v2.2.0 walks back the 2022-era vector-DB bet and ships
codevira as a **lean cross-IDE decision-enforcement layer** (~1 MB per
project, in your repo, no cloud, no vectors).

Key changes:

- Decisions now in `<repo>/.codevira/decisions.jsonl` (git-tracked,
  team-shareable, replaces graph.db SQLite).
- AGENTS.md auto-generated with hard 5 KB cap (other AI tools read it
  natively — decisions are portable across Copilot, Codex, Cursor,
  Gemini, Factory, Amp, Windsurf, Zed, RooCode, Jules).
- Relevance-gated UserPromptSubmit: off-topic → 0 tokens; on-topic →
  ≤600 tokens / ≤3 decisions; deterministic byte output (Anthropic
  prompt-cache friendly).
- ChromaDB / sentence-transformers / torch / search_codebase / calibrate
  REMOVED.

| Metric | v2.1.2 | v2.2.0 |
|---|---|---|
| Pipx install | ~200 MB | **~50 MB** |
| Cold start | 1-3s | **<100ms** |
| Per-project disk | 40-80 MB | **~1-2 MB** |
| ChromaDB bug surface | exists | **gone** |

New CLI: `codevira sync`, `codevira observe-git`. `codevira init` also
scaffolds `.codevira/`.

Full plan: [`docs/plans/v2.2.0.md`](docs/plans/v2.2.0.md).

---

## ✅ v2.1.2 — Trust Recovery + QoL (May 2026)

**Full plan:** [`docs/plans/v2.1.2.md`](docs/plans/v2.1.2.md) — 823 lines,
16 items + per-item verification + deferred list.

After v2.1.1 shipped hybrid search, three independent field-test reports
converged on the same diagnosis: **codevira works, but doesn't yet earn
trust.** v2.1.2 is a trust-recovery release — each fix restores
confidence in something codevira already does:

- **Trust-recovery (4):** smart similarity threshold (self-calibrating),
  null-not-zero for unindexed graph nodes, heal safety + `reset` rename
  + auto-export-before-destroy, auto-refresh stale graph post-edit
- **Audit-recovered (6):** cross-project rules leak fix, `complete_phase
  --backfill` for retroactive phases, `list_decisions` enumeration,
  `complete_phase` optional `git_ref`, `clean --orphans` handles bare
  global.db rows, `clean --ghosts` catches truly-empty data dirs
- **QoL wins (4):** `do_not_revert` type consistency (`1` → `true`),
  smart truncation in top_signals.rules, derive ad-hoc decision summaries
  from text, hide empty auto-signal fields
- **User-attraction (2):** README rewrite (hero block, 60-sec demo,
  comparison table, field-tester quotes), plan-in-codebase discipline

Cross-cutting: **every code change in this release must update relevant
documentation in the same commit.**

---

## ✅ v2.1.1 — Hybrid decision search (2026-05-17)

Released. Search_decisions now uses hybrid BM25 + ChromaDB semantic +
RRF fusion. Closes the UDAP-benchmark gap where natural-language queries
returned 0 hits. Added `codevira heal --decisions` non-destructive
backfill for v2.0 → v2.1.x upgrades.

---

## ✅ v2.1.0 — Reliability hardening + Pillar 3 discipline scaffold (2026-05-17)

Released. 22 P-violations fixed across detect / indexer / chunker / cli
/ crash_logger / ide_inject. Discipline scaffold (5 SKILL.md files +
hooks + Makefile gauntlet + e2e fixtures) shipped as reference
implementation.

---

## 🔜 v2.1 — New-user first contact + reliability hardening

**Renamed 2026-05-15.** The original v2.1 framing ("close credibility
gaps + ship benchmark") was correct but premature. Two days of
fresh-install testing across UDAP / lh-interface / AgentStore /
ToolsConnector + the Claude Desktop disconnect pattern surfaced 23
distinct bugs where codevira silently fails or appears broken on first
contact. Hybrid search and the public benchmark are useless if a new
HN visitor hits 5 silent failures in their first 5 minutes.

**v2.1 priority order:**
1. **Reliability hardening** (this section) — close every silent-failure path
2. **Capabilities to ship** (next section) — the credibility gaps from the
   benchmark
3. **Public benchmark suite** — only meaningful after #1 + #2 land

The benchmark, demo video, hackathon launch, and partnership pitches
shift to **v2.2** — they require a v2.1 install experience that
doesn't burn first impressions.

### Foolproof-product standard — non-negotiable for every fix

Every bug fix in this section MUST satisfy the 10 product principles
(P1–P10) defined in [`docs/foolproof-product-charter.md`](../docs/foolproof-product-charter.md):

| P | Principle |
|---|---|
| P1 | No silent failures — every 0-result path emits reason + `fix_command` |
| P2 | Self-diagnose on startup — doctor detects known-bad states |
| P3 | Atomic state mutations — tmp→fsync→rename, transactions |
| P4 | Defensive parsing — malformed input → safe default, never crash |
| P5 | Bounded resources — circuit breakers, rate limits, log rotation |
| P6 | Predictable detection — one matcher, no parallel implementations |
| P7 | Reversible operations — every install has tested uninstall |
| P8 | Helpful error messages — WHAT + WHY + FIX in every error |
| P9 | Graceful degradation — single-subsystem failure isolated |
| P10 | Observability — structured logs, doctor reads actual state |

Every PR fixing a bug below MUST:
1. Add a regression test to `tests/e2e/test_first_contact.py`
2. Confirm `tests/e2e/test_product_invariants.py` still passes
3. Walk the P1–P10 checklist explicitly in the PR description
4. Pass the release gauntlet's G2 before merging

No exceptions. The discipline-scaffold hook + CI enforce this.

### Reliability hardening — silent-failure elimination

Surfaced 2026-05-14 / 2026-05-15 across fresh-install testing. Grouped
by sub-pillar so engineering ships them coherently.

#### Group 1 — install / setup / detection

| # | Bug / item | Surfaced where | Fix |
|---|---|---|---|
| L | `init` lists generic dirs not present in project | `init` says "Source dirs: docs" for a repo where docs is irrelevant | Use the same `discover_source_files()` scanner `configure` uses; emit only dirs that exist AND contain matching files |
| M | `init` writes ALL 80 known extensions despite "Auto-detected" label | every `init` run | Intersect known extensions with what's actually on disk |
| F | `init` drops top-level files (CLAUDE.md, README.md) from `watched_dirs` | lh-interface init: "Source dirs: docs" — missing `.` | Include `.` if top-level has matching files |
| N | `init` is interactive when it should match `configure`'s scanner | `init` and `configure` give different results for same project | `init` becomes "scan + accept everything"; same scanner as `configure`, no prompts |
| D | `configure` writes in-repo `.codevira/config.yaml`; `init` writes centralized | lh-interface ended up with two disagreeing configs | Both write to `~/.codevira/projects/<key>/config.yaml`; auto-migrate legacy in-repo paths |
| G | Project keys are unreadable hashes (`Users_sachin_Documents_..._6d2f5d4d`) | `ls ~/.codevira/projects/lh-interface/` returned empty | Readable name + short collision suffix: `lh-interface__6d2f5d4d/` |
| O | `configure` requires typing "1,3,5" instead of arrow-key multi-select | every `configure` run | Use `questionary` (or `python-prompt-toolkit`) for arrow-key + space multi-select; fall back to text mode if not a TTY |
| I | `init` UI label "Auto-detected" promises detection but shows defaults | every `init` run | Resolved by L+M (real detection) |

#### Group 2 — file handling

| # | Bug / item | Surfaced where | Fix |
|---|---|---|---|
| A | Discovery vs. indexing path mismatch (configure finds 8 files, indexer finds 0) | every docs-only or polyglot project | Single shared file-matcher used by `configure` + `index` + indexer; emit error if discovered > 0 but matched = 0 |
| E | Docs-only repos silently produce 0 chunks (no markdown chunker) | lh-interface — `.md` + `.json` only | Paragraph-split markdown chunker (~50 lines); also verify `.ipynb` and large JSON schemas don't fall into the same trap |
| H | No `--verbose` / `--explain` flag on `index` | `codevira index --full -v` rejected | Add `--verbose` flag with per-file rejection reasons (parser missing, size threshold, gitignore'd, etc.) |

#### Group 3 — MCP runtime / startup

| # | Bug / item | Surfaced where | Fix |
|---|---|---|---|
| K | MCP server takes ~5s to respond to first `tools/list` (Claude Desktop times out) | Claude Desktop logs at 19:41:56 — connection closed 80ms after listrequest, response 5s later | `tools/list` answers from a static manifest; chromadb / sentence-transformers NOT imported on this critical path |
| J | sentence-transformers model loads on critical path of every MCP boot | Same as K, plus first-time HF download surprises new users | (a) Pre-warm at install (post-`pipx install` model download). (b) MCP server lazy-loads the model only on first `search_codebase`; background warmup thread starts after `initialize` responds. |
| (Chroma corruption) | HNSW segment writer crashes; watcher retries 41× with no circuit breaker | UDAP after install/uninstall churn | Detect corrupted Chroma at startup → clear error + remediation hint; circuit-break watcher after 5 same-error failures |

#### Group 4 — diagnostics + observability + self-help

| # | Bug / item | Surfaced where | Fix |
|---|---|---|---|
| B | `codevira index` says "up to date" when there's nothing in the index | Fresh `lh-interface` | Index command checks graph state first; says "not initialized" or "no files matched" when appropriate |
| C | `codevira status` doesn't warn on uninitialized project | Fresh `lh-interface` | Status uses doctor's project-state check; surfaces "run `codevira init`" when applicable |
| (heal) | `codevira heal` self-service command | User had to manually `rm -rf` Chroma collection | Single command detects + fixes: corrupted Chroma, split config, orphaned hooks, stale graph |
| (clean A) | `codevira clean` leaves orphaned hook scripts in `~/.claude/hooks/` | Earlier fresh-install test (this session, deferred from v2.0.1) | `clean` removes all `codevira-*` hook scripts AND drops codevira entries from `~/.claude/settings.json` |
| (clean B) | Post-clean message says deprecated `register` instead of `setup` | Same as above | One-line text fix |
| (clean C) | `codevira clean --project <name>` for single-project cleanup | Currently all-or-nothing | Per-project flag scoped to one project's data dir |
| (logs) | `crashes.log` grows forever, never auto-pruned | UDAP had 41 crashes from one session | Auto-prune crashes older than 30 days OR on version change |
| (atomic) | IDE config writes are non-atomic; one race cleared Claude Desktop config | Earlier in this session, hard to reproduce | Write to `.tmp`, fsync, rename, re-read to verify before reporting success |

#### Group 5 — testing / regression prevention

| # | Bug / item | Why it matters | Fix |
|---|---|---|---|
| (e2e) | No end-to-end install tests; every release ships with the same first-contact bugs | Bugs A–O are all things a single e2e test would have caught | `tests/e2e/test_first_contact.py` — runs full `init → configure → index → status` flow against fixtures: docs-only, code-only, polyglot, monorepo. CI blocks merges that regress any flow. |

### Capabilities to ship

These are the user-value items that close the credibility gaps from
the benchmark. Each is a 1–3 week shipment; total ~10 weeks if focused.
v2.2 promotes them into the launch story once v2.1 reliability lands.

#### Natural-language decision search (hybrid retrieval)
**Today.** `search_decisions("DDD architecture layer")` returns 0 hits
even when the matching decision is literally about 4-layer DDD.
`search_decisions("codevira backfill")` returns 0 hits 30 seconds after
recording. BM25-style keyword full-text silently misses every
natural-language phrasing.

**Impact.** Every agent (and human) phrasing a question naturally gets a
false "no prior decision exists" → re-decides the same thing → drift.
The whole memory layer becomes invisible. **Single biggest credibility
blocker.**

**Fix.** Embed decision text on write, store vectors alongside the
SQLite row. Search becomes `embedding_top_k + BM25_top_k → rerank`.
Optional cross-encoder reranking. ChromaDB is already in the stack —
reuse it. Until shipped, document in tool descriptions: "query with the
single rarest keyword, not natural language."

#### Decision deduplication (intelligent ADD/UPDATE/NOOP)
**Today.** Recording the same decision twice creates two rows.
Conflicting decisions silently both exist. Memory accumulates
duplicates and contradictions over months of use.

**Impact.** `search_decisions` returns 3 conflicting rows for the same
question, no signal which is current. Trust collapses. Storage grows
unbounded.

**Fix.** On every `record_decision`, run a similarity check against
existing decisions in scope. Decide one of: ADD (no overlap),
UPDATE (refines existing — bump version, keep history), NOOP
(identical), CONFLICT (surfaces to user, doesn't silently overwrite).
Backwards-compatible — agents that don't care still work.

#### Decision audit trail (history per decision)
**Today.** A decision is a row with timestamps but no edit history.
Update a decision and the previous wording is lost.

**Impact.** Regulated-codebase users can't adopt codevira — there's no
auditable trail of "what changed when, by whom." Solo devs hit it less,
but every team-mode evaluation surfaces it as a blocker.

**Fix.** Add `decision_history` table — every UPDATE writes the
previous state. Expose `get_decision_history(decision_id)` MCP tool.
Bonus: `record_decision` accepts optional `commit_hash`; full body
fetched on demand for `full=true` responses.

#### Conditional hook injection (kill the always-on token tax)
**Today.** `user_prompt_submit.sh` injects a generic "prior decisions
you may want to consider" reminder on every turn — ~175 tokens per
turn. Over a 50-turn session that's 8,750 tokens, ~74% of codevira's
total cost. Most fires on turns where the AI has no need for memory.

**Impact.** Auto-memory beats codevira on raw token cost above 6
queries/session because of this hook tax. The "lazy / 0 fixed cost"
marketing line is undermined.

**Fix.** Hook fires only when the prompt semantically warrants it:
mentions a file path / decision keyword / phase / changeset / pattern
name. Else: silent. Inject a 1-line summary of the *most relevant*
decision (using the new hybrid search), not generic boilerplate. Cost
drops ~70% on typical sessions.

#### Multi-language `get_signature` / `get_code`
**Today.** Both tools use Python's `ast` module and only read Python
files. For TypeScript / Go / Rust / Java the response is "Python-only
by design" — AI calls `Read` directly, losing the value prop.

**Fix.** Wire the existing tree-sitter parsers (already used for graph
indexing) into these two tools so they extract function signatures and
bodies for every language we already index. Per-language tests required.

#### `record_decision` batch API
**Today.** Each `record_decision` MCP call costs ~800 B of protocol
overhead. A session logging 50 decisions burns ~40 KB on framing alone.

**Fix.** Add `record_decisions_batch(items=[...])` (or extend
`write_session_log(decisions=[...])`). Backwards-compatible —
single-decision callers keep working unchanged.

### Quality / parity items (lower priority but in v2.1)

These don't block launch gates but round out the surface so codevira
doesn't look unfinished against alternatives:

#### Auto-refresh the stale code graph
`get_node()` returns `stale:true` with no actionable command. **Fix.**
Expose `refresh_node(path)` and `refresh_graph()`. Wire a post-commit
git hook to auto-refresh touched files. Stop returning `stale:true`
without offering remediation.

#### First-class user-profile / preferences memory
`search_decisions("Sachin")` returns 0 hits — codevira tracks code
decisions and rules but not collaboration style. Identity context
lives elsewhere (CLAUDE.md, auto-memory). **Fix.** Add
`record_preference(category, observation, confidence)` +
`get_user_profile()`. Categories: communication-style, commit-discipline,
depth-vs-speed bias, tooling preferences. Auto-update confidence on
confirmation/contradiction.

#### Phase number namespacing
Phase numbers are flat integers. Active "Phase 2 (Go CLI)" collides
with historical "Phase 2." 14 historical phases had to be backfilled
as session logs because real phase numbers couldn't be reused.
**Fix.** Accept `H17`, `v1.1/p1`, `2-Go-CLI` as phase IDs. Group by
prefix. Add `get_roadmap(track="active")` vs `get_roadmap(track="historical")`.

#### Bulk-import for completed phases
`complete_phase` is forward-only. No way to register a phase with
`completed_at = 2026-05-04` after the fact. **Fix.** Admin tool
`import_completed_phase(number, name, completed_at, key_decisions, files)`.
Idempotent so re-running the backfill doesn't duplicate.

#### Anti-pattern / "tried-and-rejected" memory
Codevira captures "we chose X" but not "we tried Y and abandoned it."
**Fix.** `record_decision` gets a `rejects` field pointing to an
earlier decision ID. `search_decisions` surfaces the rejection chain:
"X was tried in Phase 14, abandoned May 2026 because of Y."

---

## 🔜 v2.2 — Public launch + framework reach

v2.1 makes codevira reliable on first contact and closes the
credibility gaps. v2.2 takes it to market: publish the benchmark, ship
the cross-tool demo, run the first hackathon, and pitch partnerships.

### Launch gates (the bar before going public)

Five measurable gates. Hit all five and v2.2 ships. Not before.

| Gate | Today | v2.2 bar |
|---|---|---|
| **Public benchmark score** (UDAP-derived 8-question suite) | 88/96 | **95+/96** |
| **Per-session token cost** (20-turn coding session) | ~9,500 | **<2,500** |
| **Setup steps** (install → working on every project) | 4 commands | **1 command** |
| **Cross-tool demo reproducibility** (Claude → Cursor recall on fresh machine) | Works manually | **Recorded 60-sec video, no edits** |
| **Skeptic-to-evangelist conversion** | unmeasured | **5 outside engineers use it for a week + recommend unprompted** |

### Public benchmark suite (the credibility moat)

Open-source the UDAP-derived 8-question benchmark as a runnable repo
at `codevira/benchmark`. Adapter pattern so anyone can plug in their
memory tool (mem0, Letta, Zep, Claude auto-memory, CLAUDE.md, git).
GitHub Action runs nightly; results posted to `codevira.dev/leaderboard`.

Codevira shows up as one entry — sometimes losing on a dimension. That
honesty is the credibility moat. Within 6 months every memory-tool
launch references this benchmark.

### Cross-tool demo + 60-sec launch video

Recorded screencast: open Claude Code in a project → make a decision
→ quit → open Cursor on same project → recall the decision in 30
seconds. Caption: *"This is impossible without codevira. Local-first,
free forever."* Posted to HN, X, YouTube on launch day.

### First hackathon

"AI Memory Hackathon" — virtual, 48h, ~$2k prize pool. Drives
content, early plugin ecosystem, next round of benchmark-feeding
stories.

### Partnership pitches

One conversation per partner. If Claude Code's onboarding suggests
codevira as the recommended memory server → mass adoption in weeks.
Backup plays in priority order: Cursor team, Windsurf team,
Antigravity team.

### Framework reach + ecosystem adapters

Expanding codevira into ecosystems we don't reach today.

### Capabilities

#### Bring-your-own vector store
**Today.** Codevira is ChromaDB-only. Users with existing Pinecone /
Qdrant / pgvector infrastructure can't reuse it.

**Fix.** Adapter pattern for vector stores. Ship Pinecone, Qdrant,
pgvector adapters. Default stays ChromaDB (no setup needed). Documented
upgrade path: "if you already run X, point codevira at it."

#### Reranker integration
**Fix.** Pluggable reranker. Default: cross-encoder via
sentence-transformers (already in stack, free). Optional: Cohere Rerank
(BYO API key). LLM-as-reranker for power users.

#### LangChain / LlamaIndex adapters
**Today.** Python AI agents that aren't on MCP can't use codevira.
LangChain/LlamaIndex/CrewAI/AutoGen are the dominant frameworks.

**Fix.** Thin adapter packages: `langchain-codevira`,
`llamaindex-codevira` that wrap codevira's MCP tools as the framework's
native tool/memory interfaces. No core code change — adapters only.

#### Node SDK + Vercel AI SDK integration
**Today.** Codevira reaches the JS ecosystem only via MCP. Cursor
extensions, Vercel AI SDK apps, Continue.dev plugins, browser-side
agents are locked out.

**Fix.** `@codevira/sdk` Node package. Same operations as Python SDK.
Vercel AI SDK provider so codevira drops in as a memory layer on the
Edge.

#### REST API surface
**Today.** Codevira's HTTP server speaks MCP, not REST. Non-MCP
consumers can't integrate.

**Fix.** Add `/v1/decisions`, `/v1/preferences`, `/v1/graph` REST
endpoints alongside MCP. Same auth, same SQLite backend.

#### Memory feedback API + batch ops
**Fix.** `record_decision_feedback(id, signal)` for explicit quality
signals. `batch_update_decisions` and `batch_delete_decisions` for
bulk maintenance.

---

## 🔜 v2.3 — Cloud sync + decision support generalization

v2.1/2.2 establish codevira as the local-first standard. v2.3 expands
without abandoning that.

### Cloud sync (low-cost, opt-in, $3–5/mo target)

| Feature | Why |
|---|---|
| Cross-machine sync (laptop ↔ desktop ↔ work) | "memory follows me" |
| Encrypted backup | switch machines without losing memory |
| Hosted MCP endpoint | use from any device, including iPad / browser |
| Team sharing for 2–5 devs (small teams) | "our team's brain" |

Local-first stays the default. Cloud is purely opt-in sync. No data
leaves the machine without explicit `codevira sync enable`.

### Decision support generalization (pre-research)

Codevira's primitive — "structured, queryable, persistent decisions
with confidence scores and outcomes" — applies beyond AI agents.
Same engine could serve PMs, tech leads, founders making technical
decisions: "why did we pick Postgres over DynamoDB", "what did we
try and reject for the rate-limit problem."

Audience expansion from "developers using AI" to "anyone making
technical decisions." 10–20× the addressable market.

**Critical constraint:** do not pivot until v2.1 launch gates clear
and v2.2 ecosystem reach is established. Premature generalization
kills focused products. v2.3 is research/prototyping only — full
pivot evaluated for v3.0 based on traction signal.

---

## 🔧 v2.0.0 known limitations (rc.5 audit + post-launch surfacing)

The rc.5 audit (29 CLI items + 7 product-credibility P0s + the "index all
the code" question) closed 38 issues but surfaced 5 adjacent items that
weren't formal P-numbered findings. Two are quick UX siblings already
addressed during rc.5 (the `get_impact` and `query_graph` error messages
got the same 3-case differentiation we shipped in `get_node`). The
other three plus two new post-launch items are deferred here:

### `init` output is misleading (auto-detection lies)

Surfaced 2026-05-14 by a fresh-install on a polyglot Python project
(UDAP). Two related bugs in `mcp_server/detect.py`:

* **Extensions are NOT auto-detected** — `auto_detect_project()`
  returns the union of all ~80 known source extensions
  (`_ALL_SOURCE_EXTENSIONS`) regardless of what files exist on disk.
  The init UI prints "Auto-detected: Extensions: .adoc, .astro, .bash,
  .bazel, .c, .cap, .cc, .cjs, .clj, …" — a Python-only project
  reads .swift / .elm / .dart in the list and (rightly) loses trust
  in the tool. **Fix:** intersect the known-extensions union with
  extensions actually seen on disk via `discover_source_files()`,
  OR rename the label from "Auto-detected" to "Indexing (defaults)"
  so the promise matches the behavior. The existing `configure`
  command already detects correctly — the same scan should drive
  init's display.

* **`detect_watched_dirs` filters by single-language extension** —
  `detect.py:193` uses `LANGUAGE_EXTENSIONS.get(language, [".py"])`
  to pick which dirs count as "source dirs". For a Python project,
  any top-level dir without `.py` files (`docs/`, `configs/`,
  `migrations/`, `notebooks/`, `seeds/`, SQL-only dirs, etc.) is
  invisible — even though those files *would* be indexed because
  the extension picker is all-languages. Architectural mismatch:
  dir detector is single-language, extension picker is all-languages.
  **Fix:** make `detect_watched_dirs` use the same extension union
  as the extension picker so any top-level dir containing any
  tracked file shows up.

  Constraint: **do not introduce new commands or flags** to work
  around this. `--single-language` is a band-aid that pushes the
  problem onto the user. Installation must stay
  zero-prompt / zero-flag — the existing `configure` command is the
  one and only path for users who want to customize after install.

### Hooks documentation gap

Surfaced 2026-05-14 reviewing v2.0.0 docs against the install flow.
Hooks are mentioned in three docs (README, MIGRATING, CHANGELOG)
but no standalone page explains:

* What each of the 5 hook scripts actually does (`session_start.sh`,
  `pre_tool_use.sh`, `post_tool_use.sh`, `user_prompt_submit.sh`,
  `stop.sh`) — the active-guardian story is asserted, never shown.
* That `pipx install` does NOT inject hooks — only `codevira setup`
  (or standalone `codevira hooks install`) does. The boundary lives
  only in `setup_wizard.py` code, never spelled out for users.
* Coexistence story: codevira only adds codevira-* entries to
  `~/.claude/settings.json` and preserves existing user hooks. The
  code does this correctly but no doc says so, so cautious users
  assume it stomps their config.
* "My hooks aren't firing" troubleshooting — `codevira hooks list`
  exists but is undiscoverable.

**Fix:** add `docs/hooks.md` covering all four points; link from
README's `What `codevira setup` does` section and from MIGRATING.md's
"Lifecycle hooks" mention.

### CLI naming clarity

Three design tensions a new user runs into in the first 10 minutes:

* **`init` vs `setup` vs `register` vs `configure`** — four commands
  with overlapping scope. Pick a canonical hierarchy
  (e.g. `setup` becomes the umbrella and the others become
  `setup init` / `setup configure` subcommands), then add transition
  aliases for the old spellings.
* **`codevira inspect`** — a single "tell me everything about this
  project" command that combines `status`, `status --global`, `doctor`,
  and `projects --json` into one structured view. New top-level
  command; doesn't deprecate any existing command.
* **`--project-dir` (global flag) vs `--project PATH` (per-subcommand
  flag)** — both work today, do the same thing, take different
  spellings. Pick one canonical form, deprecate the other on a
  v2.1 → v2.2 cycle. Print a deprecation banner on the deprecated
  spelling for a release before removal.

---

## ❓ Considering (depends on user demand)

These would change Codevira's positioning. We won't build them unless solo developers explicitly ask for them.

- **Chrome extension** — overlay Codevira's `get_node` / `get_impact` on GitHub PRs and file views. See README "How It Works" for what this would unlock.
- **JetBrains plugin** — native MCP integration for IntelliJ/PyCharm/WebStorm.
- **Natural language graph queries** — "show me all files that handle authentication" via semantic graph traversal.

If you want one of these, [open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md) — we prioritize by user demand.

---

## 💭 Considering (no timeline)

Ideas being evaluated — not yet committed to a version.

- **WASM-based tree-sitter** — browser-compatible parsing for web-based AI tools (Replit, Gitpod, StackBlitz)
- **JSONL interoperability** — export/import session logs for cross-framework agent integrations
- **Memory visualization dashboard** — web UI showing project graph, learned rules, confidence heatmaps
- **Monorepo-aware indexing** — understand workspace/package boundaries in Turborepo, Nx, Lerna monorepos
- **LLM-powered rule refinement** — use LLMs to consolidate, deduplicate, and improve auto-learned rules
- **Offline-first mobile companion** — review project memory and roadmap from phone

---

## How Priorities Are Set

Features move from "Considering" to a versioned milestone based on:

1. **Developer friction** — what causes the most pain or manual work today?
2. **Community demand** — upvote issues or comment on feature requests
3. **Contribution** — well-scoped PRs get prioritized
4. **Project fit** — does it make AI-assisted coding better without adding complexity?

The guiding principle: **Codevira should be invisible.** The best developer tool is one you never have to think about — it just makes your AI agent smarter every time you use it.
