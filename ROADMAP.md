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

## ❓ Considering (depends on user demand)

These would change Codevira's positioning. We won't build them unless solo developers explicitly ask for them.

- **Team / cloud sync** — currently out of scope (Codevira is local-first by design). If demand is high, we'd add an optional `codevira sync` daemon that pushes a subset of memory to a sync target you control.
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
