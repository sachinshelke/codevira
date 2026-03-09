# Codevira MCP — Project Roadmap

This is the public roadmap for Codevira MCP itself — what's been built, what's coming next, and what's being considered for the future.

Have a suggestion? [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md).

---

## ✅ v1.0 — Initial Release (current)

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

## 🔜 v1.1 — Language Expansion

Extend full feature support beyond Python.

- **Tree-sitter integration** — AST-based chunking for TypeScript, Go, and Rust
- **`get_signature` for TypeScript/Go/Rust** — symbol extraction via tree-sitter
- **Auto-generated graph stubs for all languages** — not just Python
- **Language-specific playbook entries** — task rules for TypeScript patterns, Go idioms

---

## 🔜 v1.2 — Developer Experience

Make setup and daily use smoother.

- **Progress bar for indexing** — visual feedback during `--full` builds on large codebases
- **`codevira` CLI** — single entry point: `codevira init`, `codevira status`, `codevira index`
- **Global Installation Support** — capability to install codevira globally (e.g., via `pipx` or `uv`) and use the MCP server across all projects without per-project setup
- **Index health dashboard** — `codevira status` shows stale files, graph coverage, last indexed
- **VS Code extension** — one-click MCP server setup without editing JSON config manually

---

## 🔜 v1.3 — Smarter Graph

Make the context graph richer and more automatic.

- **Cross-file rule inference** — auto-detect invariants from patterns across sessions
- **Graph diff on PR** — show what graph nodes changed in a pull request
- **Dependency edge auto-refresh** — re-derive `connects_to` edges when imports change
- **Graph visualization** — export to Mermaid or DOT format for documentation

---

## 💭 Considering (no timeline)

Ideas being evaluated — not yet committed.

- **JSONL interoperability for session logs** — enable cross-framework agent integrations (mmkr, hydra, netherbrain etc.) by writing a parallel `.jsonl` log alongside the existing YAML format
- **Multi-repo support** — federated graph across multiple repositories in a workspace
- **Remote ChromaDB** — option to share a single index across a team (instead of per-developer local)
- **GitHub Actions integration** — run index + graph refresh in CI on every merge
- **Natural language graph queries** — "show me all files that publish events" via semantic graph search
- **Plugin system** — custom MCP tools per project without forking Codevira

---

## How Priorities Are Set

Features move from "Considering" to a versioned milestone based on:

1. **Community demand** — upvote issues or comment on feature requests
2. **Contribution** — if someone opens a well-scoped PR, it gets prioritized
3. **Project fit** — does it make AI-assisted coding better without adding fragility?

The roadmap is updated as milestones are completed or priorities shift.
