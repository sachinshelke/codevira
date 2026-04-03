# Codevira — Project Roadmap

**Universal persistent memory for AI coding agents.**

Codevira gives every AI coding tool — Claude Code, Cursor, Windsurf, Google Antigravity, or any MCP-compatible agent — persistent memory that survives across sessions, learns from developer behavior, and works on any project in any language.

**Vision:** Install once. Register once. Every project gets intelligent memory automatically. No config files, no manual setup, no vendor lock-in.

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
- **Global install support** — `pipx install codevira-mcp` works across all projects without per-project virtual environments

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

## ✅ v1.5 — Zero-Config + Deep Graph Intelligence (current)

Make Codevira instant to set up and intelligent across all projects.

- **Zero-config init** — auto-detects language, source dirs, file extensions from project markers (15+ languages); no interactive prompts
- **Smart directory scanning** — scans actual project tree for source files instead of relying on fixed folder conventions; skips known noise dirs (`node_modules`, `.venv`, `build`, etc.)
- **IDE auto-inject** — writes MCP config directly into Claude Code, Cursor, Windsurf, and Google Antigravity on `init`; non-destructive merge preserves existing settings
- **Reliable binary resolution** — finds `codevira-mcp` binary across PATH, pipx venvs, pip --user, and sibling bin; falls back to `python -m mcp_server` if needed
- **Cross-project global memory** — `~/.codevira/global.db` aggregates preferences and rules across all projects; imported on startup with confidence decay
- **Optional ML dependencies** — base install is lightweight (~50MB); `pip install 'codevira-mcp[search]'` adds ChromaDB + sentence-transformers for semantic search
- **Function-level call graph** — `symbols` + `call_edges` tables; knows which function calls which, across files
- **3 new tools**: `query_graph()` (callers/callees/tests), `analyze_changes()` (function-level risk scoring), `find_hotspots()` (complexity heatmap)
- **5 MCP workflow prompts** — `review_changes`, `debug_issue`, `onboard_session`, `pre_commit_check`, `architecture_overview`
- **36 MCP tools + 5 prompts**

---

## 🔜 v1.6 — True Zero-Friction: No Init, No Config, Just Works

**The big shift.** Eliminate every manual step between install and working memory.

Today a developer must: install → `cd project` → `codevira init` → restart IDE → repeat per project. v1.6 reduces this to: install → done.

### One-Time Global Registration
- Single MCP entry in each IDE config: `{"command": "codevira-mcp"}` — no project path, no `cwd` override, no per-project config files
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
- **Publish to PyPI** — `pipx install codevira-mcp` works for anyone worldwide
- **List on MCP registries** — Anthropic MCP registry, Cursor marketplace, Windsurf plugin store

---

## 🔜 v2.0 — Cloud Sync & Team Intelligence

Bridge from local-first tool to team-connected platform. Memory stays local by default but can sync.

### Cloud Sync
- **`codevira sync` daemon** — lightweight local agent that pushes memory data to Codevira Cloud
- **Cloud MCP server (SSE/HTTP transport)** — AI tools connect via URL + API key instead of local subprocess; enables mobile/web-based AI tools
- **GitHub Actions integration** — run index + graph refresh in CI on every merge; keep cloud memory current even when developers aren't coding

### Team Memory
- **Shared team rules and preferences** — org-wide learned rules that apply across all team members' projects
- **Multi-repo federated graph** — dependency graph spans multiple repositories in a workspace; "who depends on this shared library?"
- **Remote semantic index** — shared ChromaDB or equivalent for team-wide code search
- **Access control** — team admin controls what memory is shared vs. private

### IDE Integrations
- **VS Code extension** — one-click setup, inline memory indicators, "Codevira: Initialize" command palette
- **JetBrains plugin** — native MCP integration for IntelliJ/PyCharm/WebStorm

---

## 🔜 v3.0 — Agent Orchestration Layer

Move from passive memory to active intelligence that drives agent workflows.

- **Workflow engine** — define multi-step agent workflows (review → test → deploy) that Codevira orchestrates across tools
- **Cross-agent coordination** — when multiple AI tools work on the same project, Codevira mediates to prevent conflicts
- **Predictive suggestions** — based on learned patterns, proactively suggest what to review, test, or refactor before the developer asks
- **Natural language graph queries** — "show me all files that handle authentication" via semantic graph search
- **Custom MCP tool plugins** — developers define project-specific tools without forking Codevira
- **Webhook integrations** — trigger Codevira workflows from GitHub events, CI failures, Slack messages

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
