# Codevira MCP — Project Roadmap

This is the public roadmap for Codevira MCP itself — what's been built, what's coming next, and what's being considered for the future.

Have a suggestion? [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md).

---

## ✅ v1.0 — Initial Release

The foundation. Everything needed to give AI agents persistent memory on any project.

- **26 MCP tools** across graph, roadmap, changeset, search, and code reader modules
- **Context graph** — YAML-based file nodes with rules, stability, blast-radius BFS
- **Semantic code search** — ChromaDB + sentence-transformers, fully local
- **Roadmap system** — phase-based tracker with auto-stub on first use
- **Changeset tracking** — atomic multi-file change management with session resume
- **Decision log** — structured session logs, searchable by future agents
- **Agent personas** — Orchestrator, Planner, Developer, Reviewer, Tester, Builder, Documenter
- **Config-driven setup** — `config.yaml` for watched dirs, language, collection name
- **Auto-reindex git hook** — incremental re-index on every commit
- **Works with** Claude Code, Cursor, Windsurf, Google Antigravity, any MCP-compatible tool

---

## ✅ v1.2 — Language Expansion & Persistence Overhaul

Extend full feature support beyond Python. Migrate to a high-performance persistence layer.

- **Tree-sitter integration** — AST-based chunking for TypeScript, Go, and Rust
- **`get_signature` for TypeScript/Go/Rust** — symbol extraction via tree-sitter
- **Multi-language expansion** — 16+ languages via `tree-sitter-language-pack` (Java, C#, Ruby, PHP, C++, etc.)
- **Auto-generated graph stubs for all languages** — not just Python
- **Language-specific playbook entries** — task rules for TypeScript patterns, Go idioms
- **SQLite graph database** — migrated from hundreds of `.yaml` files to a single `graph.db`
- **SQLite memory & session logs** — agent sessions and decisions stored in `graph.db` tables
- **Blast-radius via recursive CTEs** — instant SQL-based dependency traversal
- **SHA-256 hash-based indexing** — skip unmodified files, sub-second incremental reindex
- **Live auto-watch (default)** — background file watcher auto-starts with MCP server; 2-second debounce
- **Chaos testing & parser hardening** — Rust `is_public`, Go struct/interface, import extraction fixes

---

## ✅ v1.3 — Developer Experience

Make setup and daily use smoother.

- **Progress bar for indexing** — visual feedback during `--full` builds on large codebases
- **`codevira` CLI** — single entry point: `codevira init`, `codevira status`, `codevira index`
- **Global Installation Support** — capability to install codevira globally (e.g., via `pipx` or `uv`) and use the MCP server across all projects without per-project setup
- **Index health dashboard** — `codevira status` shows stale files, graph coverage, last indexed

---

## ✅ v1.4 — Living Memory (current)

Transform Codevira from a static knowledge base into an adaptive memory that learns.

- **Real dependency graph** — wired up `extract_imports()` → `add_edge()` pipeline; `get_impact()` now returns actual blast-radius results (was always empty before)
- **Dependency edge auto-refresh** — edges re-derived on every file save via live watcher
- **Tree-sitter import resolution** — TypeScript/JS relative imports, Go packages, Rust use paths resolved to file paths
- **Graph visualization** — `export_graph()` generates Mermaid or DOT dependency diagrams
- **Graph diff on PR** — `get_graph_diff()` shows changed nodes, stability flags, and union blast radius
- **Outcome tracking** — git-based feedback loop classifies agent changes as kept/modified/reverted
- **Confidence scoring** — `get_decision_confidence()` returns outcome-based reliability scores
- **Developer preference learning** — `get_preferences()` learns coding style from post-edit corrections
- **Automatic rule learning** — `get_learned_rules()` infers test pairing, import hotspots, co-change patterns
- **Project maturity metric** — `get_project_maturity()` returns a 0–100 intelligence score
- **Session handoff** — `get_session_context()` single "catch me up" call for cross-tool continuity
- **33 MCP tools** (up from 26)

---

## 🔜 v1.5 — Ecosystem & Scale

Expand beyond single-developer, single-repo usage.

- **VS Code extension** — one-click MCP server setup without editing JSON config manually
- **Multi-repo support** — federated graph across multiple repositories in a workspace
- **Remote ChromaDB** — option to share a single index across a team (instead of per-developer local)
- **GitHub Actions integration** — run index + graph refresh in CI on every merge
- **Natural language graph queries** — "show me all files that publish events" via semantic graph search

---

## 💭 Considering (no timeline)

Ideas being evaluated — not yet committed.

- **JSONL interoperability for session logs** — enable cross-framework agent integrations
- **Plugin system** — custom MCP tools per project without forking Codevira
- **JetBrains plugin** — native MCP integration for IntelliJ/PyCharm
- **WASM-based tree-sitter** — browser-compatible parsing for web-based AI tools

---

## How Priorities Are Set

Features move from "Considering" to a versioned milestone based on:

1. **Community demand** — upvote issues or comment on feature requests
2. **Contribution** — if someone opens a well-scoped PR, it gets prioritized
3. **Project fit** — does it make AI-assisted coding better without adding fragility?

The roadmap is updated as milestones are completed or priorities shift.
