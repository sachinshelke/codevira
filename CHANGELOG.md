# Changelog

All notable changes to Codevira MCP will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

---

## [Unreleased]

### Added
- **Multi-language support expansion**: Added `tree-sitter-language-pack` to seamlessly support AST parsing, `get_signature`, and `get_code` across 14+ languages including Java, C#, Ruby, PHP, and C++.
- **SQLite Graph Database**: Migrated context graph from `.yaml` files to a single, high-performance `graph.db` SQLite database.
- **SQLite Memory & Session Logs**: Agent session logs and decisions are now stored directly in the `graph.db` `sessions` and `decisions` tables, deprecating `.md` and `.yaml` log files.
- **Blast-Radius Analysis**: Upgraded `get_impact` to use recursive CTE SQL queries for lightning-fast dependency tracing.
- **Hash-based Incremental Indexing**: Replaced modification timestamp checks with `SHA-256` content hashing, allowing the indexer to completely skip unmodified or purely "touched" files.
- **Live Auto-Watch (Default)**: The MCP server now automatically starts a background file watcher on boot. Source file changes are detected via `watchdog` and the index is incrementally updated after a 2-second debounce window — no manual `codevira index` or git commit needed. CLI `--watch` mode and post-commit hook remain available as alternatives.

### Fixed & Hardened (Chaos Testing)
- **Config Nesting Bug (Critical)**: `_load_config()` now correctly extracts the `project` sub-dict from `config.yaml`, resolving a failure where the indexer fell back to scanning `src/` (non-existent) instead of the configured `watched_dirs`, resulting in 0 chunks indexed.
- **Chunk Deduplication**: Full rebuild and incremental indexing no longer produce duplicate entries when `watched_dirs` contains overlapping paths (e.g., `"."` alongside `"indexer"`, `"mcp_server"`).
- **Rust `is_public` Detection**: Fixed a broken comparison (`"pub " in _node_text(node, b"pub ")`) that always returned `False`. Now correctly checks for `visibility_modifier` AST nodes and source text prefix.
- **Go Struct/Interface Detection**: `type_declaration` nodes now properly traverse `type_spec` children to extract `struct_type` and `interface_type` kinds, which were previously missed entirely.
- **Rust Import Extraction**: `use_declaration` nodes (e.g., `use std::collections::HashMap`) now extract scoped module paths, not just quoted strings (which only worked for JS/TS).
- **Stale Test Fixture**: Updated `test_unsupported_language` to use `"brainfuck"` instead of `"java"` since Java is now a supported language via `tree-sitter-language-pack`.


---

## [1.2.0] — 2026-03-24 — Language Expansion & Developer Experience

### Added
- **Multi-language support via tree-sitter**: Full AST-based feature parity for **TypeScript**, **Go**, and **Rust** alongside Python.
- **`indexer/treesitter_parser.py`**: Unified tree-sitter parser foundation with language-specific queries for symbol extraction, import parsing, docstring extraction, and visibility detection.
- **Multi-language chunking** (`indexer/chunker.py`): `chunk_file()` and `extract_imports()` dispatch to tree-sitter for `.ts`, `.tsx`, `.go`, `.rs` files; Python files continue using stdlib `ast`.
- **Multi-language code reader** (`mcp_server/tools/code_reader.py`): `get_signature()` and `get_code()` now support all 4 languages — `.py`-only gate removed.
- **Multi-language graph generation** (`indexer/graph_generator.py`): `generate_graph_node()`, `_get_module_docstring()`, `_get_public_symbols()` dispatch to tree-sitter for non-Python files.
- **Multi-language playbook rules** (`mcp_server/data/rules/multi-language.md`): Language-specific coding standards for TypeScript, Go, and Rust.
- **`codevira` CLI entry point**: Consolidated `codevira-mcp` commands into a shorter `codevira` global alias for simpler daily use (`codevira init`, `codevira index`, `codevira status`).
- **Index health dashboard**: the `status` command now displays a highly readable `rich` Table and Panel outlining index statistics, outdated files, and timestamp.
- **Progress bar for indexing**: Full and incremental `index` commands now display a visual `rich.progress` bar for chunk indexing progress.
- **Global Installation Support**: Built-in support to run `codevira` from anywhere without virtual environment dependencies, correctly resolving the target `cwd` path instead of strictly `__file__`.
- **36 tree-sitter parser tests** (`tests/test_treesitter_parser.py`): Comprehensive coverage for all 3 languages.
- **Test fixtures**: Sample files for TypeScript, Go, and Rust in `tests/fixtures/`.

### Changed
- `iter_source_files()` now reads `file_extensions` from config instead of hardcoding `.py`.
- `config.example.yaml` updated to document full support for all 4 languages.

### Fixed & Hardened (Destructive Testing)
- **CLI Startup Crash**: Removed an erroneous nested `asyncio.run()` wrapper in `mcp_server/cli.py` that caused fatal `ValueError: a coroutine was expected` crashes when the CLI was executed as a raw MCP server.
- **AST Relative Import Bug**: Fixed a `NoneType` attribute error in Python AST chunking where relative imports (`from . import x`, level > 0) caused the indexer to fail.
- **Database Corruption Recovery**: Deep OS-level chaos testing revealed that corrupted ChromaDB files or locked `.codevira` directories leaked raw SQLite stack traces. Added robust interception that outputs formatted, step-by-step shell commands instructing developers how to rebuild the missing database (`rm -rf ... && codevira index --full`), bypassing the panic.
- **Idempotent Missing State**: Running `codevira index` without an initialized configuration safely warns `No baseline found...` instead of faulting.

### Dependencies
- Added `tree-sitter>=0.23`, `tree-sitter-typescript>=0.23`, `tree-sitter-go>=0.23`, `tree-sitter-rust>=0.23`.
- Added `rich>=13.0.0` for premium terminal output and formatting.

## [1.1.2] — 2026-03-09

### Added
- **Global MCP Client Guide:** Added explicit documentation in `README.md` and `FAQ.md` explaining how to configure uniquely named servers (e.g., `codevira-project-a`) to prevent cross-project roadmap contamination when using global clients like Google Antigravity or Claude Desktop.
- **Gitignore Safeguard:** Added `.codevira/` to the default project `.gitignore` to prevent auto-generated configuration and database files from being accidentally committed to public repositories.

---

## [1.0.0] — 2026-03-06 — Initial Release

### Added

**Core MCP Server — 26 tools across 5 modules**
- `get_node`, `get_impact`, `list_nodes`, `add_node`, `update_node`, `refresh_graph`, `refresh_index` — context graph tools
- `get_roadmap`, `get_full_roadmap`, `get_phase`, `update_next_action`, `update_phase_status`, `add_phase`, `complete_phase`, `defer_phase` — roadmap tools
- `list_open_changesets`, `get_changeset`, `start_changeset`, `complete_changeset`, `update_changeset_progress` — changeset tools
- `search_codebase`, `search_decisions`, `get_history`, `write_session_log` — search and session tools
- `get_signature`, `get_code` — Python AST code reader tools
- `get_playbook` — curated task rule lookup

**Indexer**
- ChromaDB + sentence-transformers semantic code index
- Python AST chunker with function/class-level granularity
- Auto-generated context graph stubs from imports and docstrings
- Incremental indexing (only changed files since last build)
- `--full`, `--status`, `--watch`, `--generate-graph`, `--bootstrap-roadmap` CLI flags
- Config-driven via `.agents/config.yaml` (watched_dirs, language, file_extensions, collection_name)

**Agent System**
- Seven agent persona definitions: Orchestrator, Planner, Developer, Reviewer, Tester, Builder, Documenter
- Session protocol (`PROTOCOL.md`) with mandatory start/end steps
- 16 engineering rules files covering coding standards, testing, API design, git governance, and more

**Developer Experience**
- `roadmap.yaml` auto-stub on first `get_roadmap()` call — zero setup required
- Git post-commit hook for auto-reindex on every commit
- `config.example.yaml` template for quick project setup
- Graph node schema reference (`graph/_schema.yaml`)

**Documentation**
- Full README with quickstart, tool reference, agent personas, language support table
- `PROTOCOL.md` — session protocol for AI agents
- `FAQ.md` — setup, usage, architecture, and troubleshooting
- `ROADMAP.md` — public project roadmap with versioned milestones
- `CONTRIBUTING.md` — contribution guide including AI-assisted workflow
- `CODE_OF_CONDUCT.md`, `SECURITY.md`
- GitHub issue templates (bug report, feature request) and PR template

**Language Support**
- Full support: Python (AST chunking, get_signature, get_code, auto graph stubs)
- Partial support: TypeScript, Go, Rust (regex chunking; all non-AST tools work)
