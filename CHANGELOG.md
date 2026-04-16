# Changelog

All notable changes to Codevira MCP will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

---

## [Unreleased]

---

## [1.6.1] — 2026-04-16 — Stability, Graceful Degradation & Cleanup

### Added
- **`codevira clean` command**: One-shot removal of all Codevira data, IDE configs,
  and services. Supports `--all` (per-project artifacts), `--dry-run` (preview),
  and `-y` (skip confirmation).
- **Google Antigravity global mode**: `codevira register` now includes Antigravity
  with a single global entry — no per-project hardcoded paths.

### Fixed
- **Graceful degradation when chromadb not installed**: `refresh_index` and
  `cmd_incremental` now work in graph-only mode instead of crashing with
  ImportError. Background file watcher no longer generates noisy exceptions
  on every file save.
- **`sys.exit()` crashes eliminated**: `server.py` module-level import failure
  now uses stderr + raise instead of corrupting the MCP stdio protocol.
  `cmd_incremental` no longer kills the MCP server process from background
  watcher threads.
- **Binary resolution in user-facing output**: `codevira init` "For other AI
  tools" section now shows the resolved `codevira` binary path instead of a
  hardcoded Python interpreter path (e.g. `/opt/homebrew/...`).
- **Antigravity config path**: Fixed from `~/.gemini/settings/mcp_config.json`
  (wrong) to `~/.gemini/antigravity/mcp_config.json` (correct).
- **Rich markup escaping**: `codevira[search]` install hints now display
  correctly — Rich no longer strips `[search]` as a style tag.
- **`codevira status` without chromadb**: Shows "Semantic Search: not installed"
  with install tip instead of crashing. Added graph node count to status.
- **Git hook uses full binary resolution**: Post-commit hook now uses
  `_resolve_command()` instead of simple `shutil.which`.

### Performance
- **`get_data_dir()` caching**: Result cached per project root. First call runs
  subprocess + metadata scan; subsequent calls are O(1) dict lookups.
- **`set_project_dir()` cache invalidation**: Changing the project root now
  clears the data-dir cache automatically.
- **Unbounded join timeout**: Background semantic indexing thread capped at
  5 minutes; server continues in graph-only mode if it hangs.

### Changed
- **Package renamed**: `codevira-mcp` → `codevira`. Install with `pip install
  codevira`. CLI command is now `codevira` (not `codevira-mcp`).
- **Removed unused `gitpython` dependency**: CodeVira uses `subprocess` for
  all git operations. Saves ~20MB install weight.
- **Removed out-of-scope rule files**: REST API standards, SSE/UI events,
  TUI layout/keybinding rules — none apply to an MCP server.
- **Removed vendor-specific secret patterns from crash logger**: Stripe, AWS,
  GitHub token regexes were irrelevant to CodeVira's scope.
- **Test isolation hardened**: Autouse fixture clears `_data_dir_cache` and
  resets `_project_dir_override` between every test.
- **`_init_done` renamed to `_init_started`**: Name matches semantics — the
  flag signals thread launch, not completion.
- **`install_launchd()` accepts `project_dir`**: Adds `--project-dir` to
  ProgramArguments and `WorkingDirectory` to the plist.

---

## [1.6.0] — 2026-04-03 — True Zero-Friction: No Init, No Config, Just Works

### Added — Centralized Storage
- **`~/.codevira/projects/<key>/`**: All project data now lives centrally, keyed by sanitized path. No more `.codevira/` directories polluting project repos.
- **`mcp_server/paths.py` v1.6 resolution chain**: `get_data_dir()` checks centralized dir → git remote lookup (survives renames) → legacy `<root>/.codevira/` fallback → defaults to centralized for new projects.
- **`_discover_project_root()`** now uses project markers (`.git`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`) instead of requiring `.codevira/config.yaml`.
- **`mcp_server/migrate.py`** (NEW): `detect_migration_needed()` + `migrate_to_centralized()` — safe WAL-mode SQLite backup, copies graph.db/codeindex/config.yaml/roadmap.yaml, writes metadata.json, renames old `.codevira/` to `.codevira.migrated/` as safety net. Idempotent.
- **Auto-migration on server startup**: Both stdio (`server.py`) and HTTP (`http_server.py`) servers detect and migrate legacy projects automatically.
- **`indexer/global_db.py`**: Added `git_remote TEXT` column to `projects` table. `register_project()` now accepts `git_remote` parameter. New `find_project_by_remote()` method for rename-resilient lookup.

### Added — .gitignore-Aware File Discovery
- **`mcp_server/gitignore.py`** (NEW): `load_gitignore_spec()` recursively loads all `.gitignore` files (including nested). `discover_source_files()` walks the full project tree with gitignore + safety-net exclusions. `infer_language_from_files()` counts extensions to detect dominant language.
- **`pathspec>=0.12.0`** added as base dependency.
- **`mcp_server/detect.py`**: `_scan_dominant_language()` and `detect_watched_dirs()` now delegate to `discover_source_files()` + `infer_language_from_files()` with legacy fallback.

### Added — Auto-Init on First Tool Call
- **`mcp_server/auto_init.py`** (NEW): `ensure_project_initialized()` — fast-path no-op if already done, otherwise starts background thread that auto-detects project, creates centralized dirs, writes config.yaml + metadata.json, registers in global.db, builds graph and index.
- **`server.py call_tool()`**: Calls `ensure_project_initialized()` before every tool dispatch (< 1ms no-op overhead after first call).
- **Graceful degradation**: `search_codebase()` returns `{status: "indexing", message: "..."}` instead of error while index is building. `get_node()` returns `{status: "initializing", ...}` for missing nodes during graph build.
- **`get_session_context()`** now includes `indexing_progress` field when background init is running.

### Added — Global IDE Registration (v1.6)
- **`codevira register`** (NEW CLI subcommand): One-time global injection into all detected IDEs. Works for every project automatically. No per-project `init` required.
- **`codevira register --claude-desktop`**: Configure Claude Desktop specifically (stdio mode, full binary path, --project-dir).
- **`codevira register --http-url https://localhost:7443/mcp`**: Inject HTTP URL format into Claude Code global settings.
- **`mcp_server/ide_inject.py` v1.6**: Added Claude Desktop injection (`_inject_claude_desktop()`), global mode functions (`inject_global_claude_code/cursor/windsurf()`), HTTP URL injection (`inject_claude_http_url()`). Fixed Windows cross-platform bug (`sysconfig.get_path("posix_user")` → `"nt_user"` on Windows). Fixed Antigravity server name sanitization (regex handles all special chars).

### Added — macOS Service Auto-Start
- **`mcp_server/launchd.py`** (NEW): `install_launchd(port, use_https)` generates `~/Library/LaunchAgents/com.codevira.mcp-serve.plist` and loads it. `uninstall_launchd()` removes it. `launchd_status()` reports current state.
- **`codevira serve --install-service`**: Install macOS launchd plist so HTTP server starts on login.
- **`codevira serve --uninstall-service`**: Remove the launchd service.

### Fixed — Module-Level Path Evaluation
- **`indexer/index_codebase.py`**: Removed module-level `PROJECT_ROOT = get_project_root()` and `INDEX_DIR = get_data_dir() / "codeindex"`. Replaced with lazy `_project_root()` and `_index_dir()` functions. All 12 call sites updated.
- **`indexer/outcome_tracker.py`**: Removed module-level `PROJECT_ROOT = get_project_root()`. Replaced with lazy `_project_root()`. All 2 call sites updated.
- **`indexer/chunker.py`**: Removed module-level `_config = _load_config()` and derived variables. Replaced with `@functools.lru_cache` `_get_project_config()` function. All call sites updated.

### Fixed — Thread Safety
- **`indexer/index_codebase.py`**: Added `_chroma_write_lock` (threading.Lock) around all ChromaDB write operations. Background watcher's `_do_reindex()` and `start_background_full_index()` both acquire this lock — prevents concurrent write corruption.
- **`start_background_full_index()`** (NEW): Start a full index rebuild in a background daemon thread, used by auto_init.py.

### Fixed — HTTP Server Cert Path
- **`mcp_server/http_server.py`**: Module-level `_CERTS_DIR = Path.home() / ".codevira" / "certs"` replaced with lazy `_certs_dir()` function using `get_global_home()`. Cert file accessors updated to functions `_cert_file()` / `_key_file()`.

---

## [1.5.2] — 2026-04-03 — HTTP Transport + Claude Desktop Support

### Added
- **HTTP/Streamable transport** (`mcp_server/http_server.py`): New `codevira serve [--port N] [--https] [--host ADDR]` command starts a persistent MCP HTTP server using the MCP Streamable HTTP 2025-03-26 spec. Endpoint: `/mcp`. Health check: `GET /`.
- **HTTPS with mkcert**: `--https` flag auto-generates trusted localhost certificates to `~/.codevira/certs/` using mkcert. Certs are reused on subsequent runs.
- **Claude Desktop support**: `claude_desktop_config.json` now documented and auto-injected correctly using `command`+`args` (stdio) format, which is the only format Claude Desktop supports.
- **Transport decision table**: README, PROTOCOL, and FAQ updated with a clear matrix — which transport to use for each client (Claude Desktop, Claude Code CLI, Cursor, Windsurf, Antigravity).
- **`NODE_EXTRA_CA_CERTS` setup guide**: FAQ documents the one-time mkcert trust setup required for Claude Code CLI to accept local HTTPS certs.

### Fixed
- `--project-dir` flag now works both before and after the `serve` subcommand (argparse previously rejected it after the subcommand name).

---

## [1.5.0] — 2026-04-02 — Zero-Config Global Memory + Deep Graph Intelligence

### Added — Zero-Config Init
- **Auto project detection** (`mcp_server/detect.py`): `codevira init` now requires zero prompts. Language, watched dirs, and file extensions are inferred from project markers (`Cargo.toml`, `go.mod`, `tsconfig.json`, `pyproject.toml`, `package.json`, etc.) across 15 languages.
- **IDE auto-inject** (`mcp_server/ide_inject.py`): On `init`, automatically writes MCP server config into Claude Code (`.claude/settings.json`), Cursor (`.cursor/mcp.json`), Windsurf (`.windsurf/mcp.json`), and Google Antigravity config — non-destructively, merging with existing entries.
- **CLI flags**: `--name`, `--language`, `--dirs`, `--ext`, `--no-inject` for overriding auto-detection without interactive prompts.

### Added — Cross-Project Global Memory
- **Global DB** (`indexer/global_db.py`): `~/.codevira/global.db` aggregates preferences and learned rules across all projects. Tables: `projects`, `global_preferences`, `global_rules`.
- **Global sync** (`mcp_server/global_sync.py`): On server startup, imports global preferences (frequency ≥ 3) and rules (confidence ≥ 0.6) into the current project with 0.8× decay. On session end, exports project-level signals back to global.
- **`get_global_stats()` in `get_session_context()`**: Single-call context now includes cross-project intelligence count.
- **Paths** (`mcp_server/paths.py`): `get_global_home()` / `get_global_db_path()` create `~/.codevira/` on first use.

### Added — Function-Level Call Graph
- **`symbols` table** in SQLite: stores functions/classes/methods with name, kind, signature, parameters, return type, start/end line, docstring, visibility.
- **`call_edges` table** in SQLite: caller → callee relationships with line numbers, resolved at index time.
- **`add_symbol()`, `add_call_edge()`, `get_callers()`, `get_callees()`, `get_symbols_for_file()`, `find_symbol()`, `find_hotspot_functions()`, `find_high_fan_in()`** — 8 new SQLite methods.
- **Phase 2/3 indexing** in `graph_generator.py`: After file nodes, populates symbols via `_get_python_symbols_detailed()` (ast.walk with call extraction), then resolves cross-file call edges.

### Added — Deep Graph Tools (3 new MCP tools)
- **`query_graph(file_path, symbol?, query_type)`**: Traverses call graph for `callers`, `callees`, `tests`, `dependents`, or `symbols` — function-level, not just file-level.
- **`analyze_changes(base_ref?, head_ref?)`**: Function-level risk scoring for every changed file — flags missing tests, counts callers, identifies high-risk changes.
- **`find_hotspots(threshold?)`**: Finds large functions (>50 lines), high fan-in (>5 callers), high fan-out nodes — complexity heatmap for the codebase.

### Added — MCP Workflow Prompts (5 prompts)
- **`review_changes`**: Staged diff + blast radius + risk score in one prompt.
- **`debug_issue`**: Symptom → affected files → call chain → hypothesis.
- **`onboard_session`**: Full project context catch-up for new AI sessions.
- **`pre_commit_check`**: Test coverage gaps + high-risk functions before commit.
- **`architecture_overview`**: Module map + hotspots + dependency summary.

### Added — Tests
- **`tests/test_v15_zero_config.py`**: 31 new tests covering auto-detection, IDE inject, global DB, call graph, hotspot detection, MCP prompts, and global sync lifecycle.

### Changed
- **`mcp_server/cli.py`**: Replaced all 4 `input()` calls with `auto_detect_project()`; replaced manual JSON printing with `inject_ide_config()`; registers project in global DB on init.
- **`mcp_server/server.py`**: Registered 3 new graph tools + 5 MCP prompts via `@server.list_prompts()` / `@server.get_prompt()`; runs `import_global_to_project()` on startup.
- **`mcp_server/tools/learning.py`**: `get_session_context()` now includes `global_intelligence` stats.

### Verified
- Full tool audit: **36/36** tool dispatches registered (33 tools + 3 new graph tools).
- MCP prompts: **5/5** registered and resolvable.
- Unit tests: **101/101** pass (70 existing + 31 new).

---

## [1.4.0] — 2026-04-02 — Living Memory: Adaptive Learning & Real Dependency Graph

### Added — Dependency Graph (was broken, now works)
- **Dependency edges wired up**: `extract_imports()` is now called during graph generation, populating the `edges` table via new `add_edge()` / `remove_edges_for_node()` methods. `get_impact()` now returns real blast-radius results (was always empty before).
- **Tree-sitter import resolution**: Enhanced `_extract_imports_treesitter()` to resolve TypeScript/JS relative imports, Go package imports, and Rust use paths to actual project file paths.
- **Edge auto-refresh**: Dependency edges are re-derived on every incremental index and live file-watcher trigger — edges stay current within 2 seconds of file save.

### Added — Adaptive Learning Engine (7 new MCP tools)
- **`get_decision_confidence(file_path?, pattern?)`**: Returns outcome-based confidence scores — how often past decisions in an area were kept, modified, or reverted.
- **`get_preferences(category?)`**: Returns learned developer style preferences (naming, structure, patterns) from post-edit correction signals.
- **`get_learned_rules(file_path?, category?)`**: Returns auto-generated rules from observed patterns — test pairing, import hotspots, co-change files, recurring decision phrases.
- **`get_project_maturity()`**: Returns a 0–100 maturity score combining session count, file coverage, confidence, learned rules, and preference signals.
- **`get_session_context()`**: Single "catch me up" call for cross-tool continuity. Returns current roadmap phase, open changesets, recent decisions with confidence, top preferences, and active rules.
- **`export_graph(format, scope?)`**: Generates dependency diagrams in Mermaid or DOT format, with stability-based node styling.
- **`get_graph_diff(base_ref?, head_ref?)`**: Shows which graph nodes changed between git refs, their stability, do_not_revert flags, and union blast radius.

### Added — Backend (3 new files)
- **`indexer/outcome_tracker.py`**: Git-based feedback loop — analyzes post-session git history to classify changes as kept, modified, or reverted. Feeds confidence scoring and preference learning.
- **`indexer/rule_learner.py`**: Pattern detection engine — infers test pairing rules, import hotspot rules, decision pattern rules, and co-change rules from session history.
- **`mcp_server/tools/learning.py`**: MCP tool implementations for all 7 learning tools, including maturity scoring and cross-tool session handoff.

### Added — SQLite Schema (3 new tables)
- **`outcomes`**: Tracks kept/modified/reverted outcomes per decision with delta summaries.
- **`preferences`**: Stores developer style signals with frequency counts and examples.
- **`learned_rules`**: Auto-generated rules with confidence scores, categories, and file pattern matching.

### Changed
- **`generate_graph_sqlite()`** now returns `edges_added` count alongside `nodes_added` / `nodes_skipped`.
- **MCP server startup** now runs outcome analysis and rule inference on boot (best-effort, non-blocking).

### Verified
- Full tool audit: **33/33** tool dispatches registered and passing.
- Unit tests: **70/70** pass (41 existing + 22 new + 7 edge-case).
- Real codebase validation: 57 dependency edges populated, blast radius returns 27 affected files for core modules.

---

## [1.3.1] — 2026-03-26 — MCP Tool Dispatch Hotfix

### Fixed
- **`write_session_log` crash**: Simplified from 12 mismatched parameters to 6 clean parameters (`session_id`, `task`, `phase`, `files_changed`, `decisions`, `next_steps`). The MCP schema now expects `decisions` as structured `list[object]` with `{file_path, decision, context}` instead of plain strings. Both `documenter.md` copies updated to match.
- **`search_codebase` crash**: Server dispatch passed `limit=` and `layer=` but function expects `top_k=` and has no `layer` param. Fixed dispatch to use `top_k=`.
- **`add_node` crash**: Server dispatch passed `graph_file=` but function doesn't accept it. Removed from dispatch and schema.
- **`get_history` crash**: Server dispatch passed `n=5` but function only accepts `file_path`. Removed `n` from dispatch and schema.
- **`refresh_index` crash**: Server dispatch passed `None` via `.get()` but function expects `list[str]`. Added `or []` fallback.
- **`update_node` crash**: Dispatch was calling `update_node_after_change()` from `changesets.py` which had a **broken import** (`from tools.graph import _load_all_nodes` — function doesn't exist). Switched to SQLite-based `update_node()` from `graph.py`.
- **Schema accuracy**: Removed `n` parameter from `get_history` schema and `graph_file` parameter from `add_node` schema — these params were advertised to AI agents but never accepted by the backend.
- **Documentation sync**: Updated both `agents/documenter.md` and `mcp_server/data/agents/documenter.md` to show correct 6-param `write_session_log` usage with structured decisions.

### Verified
- Full dispatch audit: **26/26** tool dispatches pass parameter matching tests.
- Unit tests: **37/37** pass.

---

## [1.3.0] — 2026-03-26 — Persistence Overhaul, Live Auto-Watch & Parser Hardening

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
- **`codevira` CLI entry point**: Consolidated `codevira` commands into a shorter `codevira` global alias for simpler daily use (`codevira init`, `codevira index`, `codevira status`).
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
