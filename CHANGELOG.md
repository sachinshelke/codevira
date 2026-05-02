# Changelog

All notable changes to Codevira MCP will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

---

## [Unreleased]

### Planned for v1.9

- **Interactive checkbox UI for `codevira configure`**. The current
  prompt asks users to type comma-separated indices ("1,3,5") into a
  numbered list — fine for 3–5 items, awkward for 15+. v1.9 will add
  arrow-key navigation + space-to-toggle multi-select, matching the
  UX of `npm create vite`, `gh repo create`, etc.

  **Design (opt-in dependency):**
  - Default install (`pip install codevira`) keeps the current numbered
    prompt — zero new dependencies, zero new failure modes.
  - `pip install codevira[ui]` pulls `questionary` (~3 MB). When
    importable AND `sys.stdin.isatty()` AND `os.environ.get("TERM")
    != "dumb"`, `prompt_multi_select` switches to the checkbox UI.
  - `--dirs` and `--extensions` flags continue to work for both paths
    (CI / scripts / non-interactive use).

  **Why deferred from v1.8.1:** v1.8.1 is a pure crash hotfix; mixing
  in a UX feature would slow the release and complicate testing. The
  numbered prompt has shipped since v1.8.0 and works fine — this is
  polish, not a fix.

  **Implementation notes for whoever picks this up:**
  - Site: `mcp_server/cli_configure.py:prompt_multi_select` (line ~176)
  - Add `[ui]` extra in `pyproject.toml` with `questionary>=2.0`
  - TTY detection already exists (`NonInteractiveError` raised on
    `not sys.stdin.isatty()`); extend it to also branch on
    questionary availability
  - Test surface: split into two test classes — one mocks `input()`
    (current behavior), one mocks `questionary.checkbox()`. Skip the
    questionary tests when not installed.
  - Accessibility: keep the numbered prompt for screen-reader users;
    document `CODEVIRA_DISABLE_TUI=1` env var as the override.
  - No schema changes, no public-API changes.

### Other v1.9 candidates (no design yet)

- Watcher restart circuit breaker (deferred from v1.8.1 — see "Out of
  scope" below).
- Refactor `_enable_wal_with_retry` into a shared `indexer/_sqlite_util.py`
  (deferred from v1.8.1).
- Watcher hot-reload of `config.yaml` on disk changes.
- `crash_logger` size cap or rotation (currently grows unbounded).

---

## [1.8.1] — 2026-05-02 — Production Hotfix from Real-World Crash Logs

Pure bug-fix release. No new features, no schema changes, no public-API
changes. Motivated by a real production failure on the maintainer's
machine: **43 crashes in 70 minutes** logged by `crash_logger` between
07:37 and 08:47 on 2026-04-24, all under
`WHERE: background watcher: incremental reindex`.

Breakdown:
- **41 × `InterruptedError` (EINTR, errno 4)** in
  `_get_changed_files`'s rglob walk, all walking
  `~/Library/Group Containers/...` (WhatsApp, Office, etc.) and
  `~/Library/Containers/...` (TextEdit, mediaanalysisd, …).
- **2 × `OperationalError("database is locked")`** in
  `SQLiteGraph.add_symbol` and `remove_symbols_for_file`.

Root cause: a rogue project data dir with
`metadata.json.original_path = "/Users/sachin"` (the user's `$HOME`).
`auto_detect_project` saw `Library`, `Downloads`, `Documents`, `go` as
"subdirs", and the watcher then walked huge unrelated trees. v1.8.0's
bootstrap (`cmd_configure`, `auto_init`) didn't refuse `$HOME`, and
neither did `cmd_init`.

### Fixed

- **Refuse `$HOME` and system top-levels as a project root** (the
  critical fix — eliminates 41 of the 43 production crashes by
  preventing the rogue project from forming). New helper
  `mcp_server.paths.is_invalid_project_root()` rejects `$HOME`, `/`,
  `/Users`, `/home`, `/tmp`, `/private/tmp`, `/var`, `/private/var`,
  `/etc`, `/opt` (plus the macOS-resolved `/private/etc` and
  `/System/Volumes/Data/home` forms). Wired into TEN distinct sites
  covering every state-creating path the codebase exposes:
  - **CLI entry points (6):** `cmd_configure`, `cmd_init`, `cmd_index`,
    `cmd_register`, `cmd_serve` (refuses both regular serve AND
    `--install-service`; `--uninstall-service` is exempt so users can
    always remove old launchd plists), `auto_init._run_background_init`.
  - **MCP server entry points (2):** `mcp_server.server.main()` (stdio
    transport) and `mcp_server.http_server.run_http_server()` (HTTP
    transport). Both are reachable directly via `python -m`, not just
    through the CLI.
  - **Direct module entry (1):** `indexer.index_codebase.__main__`
    (`python -m indexer.index_codebase --full | --watch | (default)`)
    — this is a separate CLI surface from the `codevira` binary;
    pre-revalidation it bypassed `cli.cmd_index`'s guard entirely.
    `--status` is exempt (read-only, bails on missing graph.db).
  - **Defense-in-depth (1):** `indexer.index_codebase.start_background_watcher`
    refuses to start the watcher even if a programmatic caller bypasses
    every entry-point guard above. Returns `None`; both `cmd_watch` and
    `server.main` handle `None` correctly.

  The `server.main()` and `run_http_server` guards are the most critical
  — without them, a user upgrading from v1.8.0 *without* first running
  `clean --orphans` would still hit the original crash mode: their
  leftover rogue `config.yaml` would drive `start_background_watcher`
  into walking `~/Library/Group Containers/...`, which is exactly where
  the 41 production `InterruptedError` crashes came from. The
  `start_background_watcher` defense-in-depth guard is a belt-and-braces
  fallback — even if all entry-point guards regressed, the watcher
  itself cannot start with an invalid project root.

  `cmd_index`, `cmd_register`, `cmd_serve --install-service` close
  defense-in-depth holes that pre-revalidation could have leaked state
  on disk: silent dead-weight `mkdir` of
  `~/.codevira/projects/<HOME_slug>/{graph,codeindex}/`, IDE configs
  pinned to broken paths, and persistent launchd plists pointing at
  `$HOME`. Pre-release revalidation across three rounds walked every
  CLI sub-command, the stdio/HTTP server entry, the launchd
  `--install-service` path, the `start_background_watcher` direct call
  path, and the production-replay scenario (synthetic v1.8.0 leftover
  rogue + `codevira` from `$HOME`). All paths refuse cleanly with zero
  new crashes; legitimate projects untouched.

  `auto_init` sets `_progress["status"] = "error"` so the MCP server
  stops looping on retries.

- **`SQLiteGraph` WAL with retry — port of the v1.8.0 GlobalDB fix**
  (eliminates the 2 of 43 `database is locked` crashes). v1.8.0 fixed
  the same race for `GlobalDB` after round 3 of binocular review;
  `SQLiteGraph` was missed. `__init__` now opens with `timeout=30`,
  enables WAL via the same retry loop pattern, and sets
  `PRAGMA busy_timeout=30000` for subsequent writes.

- **`_get_changed_files` and `cmd_full_rebuild` rglob loops tolerate
  `OSError`** (defense-in-depth — even on legitimate projects,
  transient `EINTR`, `PermissionError`, or "directory changed during
  iteration" should not kill the whole reindex). Per-watch-dir scope
  matches `watchdog.Observer`'s thread-per-watch model: the
  microsecond-spaced parallel-thread crashes (3 within 6μs at
  08:15:29 and 08:26:04 in the production log) confirm this is the
  right granularity. `InterruptedError` is a subclass of `OSError`, so
  the broader catch covers EINTR plus other transient walk failures.

### Added

- **`codevira clean --orphans`** — recovery path for users already hit
  by the `$HOME`-bootstrap bug on v1.8.0. Walks
  `~/.codevira/projects/*/metadata.json`; for each entry whose
  `original_path` is rejected by `is_invalid_project_root()` OR no
  longer exists on disk, removes the data dir and deletes the matching
  row from `~/.codevira/global.db`. Reuses the existing `--dry-run`
  and `-y/--yes` flags. Without this, affected users would need to
  `rm -rf` and run raw sqlite by hand.

- **Denylist macOS/Linux/cloud-sync user-data dirs in
  `auto_detect_project`** (defense-in-depth). `_SKIP_DIRS` extended
  with `Library`, `Downloads`, `Music`, `Movies`, `Pictures`,
  `Desktop`, `Public`, `Applications`, `Videos`, `Templates`, plus
  cloud-sync top-levels (`Dropbox`, `iCloud Drive`, `OneDrive`,
  `Google Drive`, `Box`). Even if `is_invalid_project_root` somehow
  misses (e.g. a user passes `--project-dir` to a `$HOME`-shaped
  layout), these never show up in `watched_dirs`. A user who
  legitimately has a project named e.g. `Library` can still pass
  `codevira configure --dirs Library` to opt in.

### Out of scope (deferred to v1.9)

- **Watcher restart circuit breaker.** Crash log shows ~60s gaps
  between EINTR crashes — no backoff. Adding a circuit breaker is real
  design work; the rglob `OSError` tolerance closes the immediate
  hole.
- **Refactoring `_enable_wal_with_retry` to a shared util.** 25 lines
  of duplication for one patch cycle is the right call; touching
  `GlobalDB`'s tested-in-v1.8.0 code is higher risk.
- **`crash_logger` size cap or rotation.** Log file is ~97KB now; will
  grow unbounded over time. Out of scope for hotfix; flagged for v1.9.

---

## [1.8.0] — 2026-04-23 — Memory Sharpening + Config UX

Three internal improvements that make the memory we already capture **sharper**,
without making it heavier. Zero new MCP tools. Zero new tables. The public API
shape changes only one thing: `get_session_context()` gains a `focus_source`
field (~10 tokens, additive, backwards-compatible).

The problem this release solves:
- `get_session_context()` returned the 3 newest decisions by timestamp —
  regardless of whether they had anything to do with the current task.
- `search_decisions()` ordered purely by recency — a `file_path` match
  was no better than a match buried in an unrelated session summary.
- `log_session()` inserted every decision unconditionally — a day of
  iterative agent work logged the same intent 5+ times.

### Fixed

- **MCP `serverInfo.version` reported the MCP library version, not codevira's**
  (pre-existing bug, surfaced during v1.8 install verification on Python
  3.13). `Server("codevira")` was constructed without a `version=` argument,
  so the framework defaulted to its own pip-package version (e.g. `1.27.0`)
  in the JSON-RPC `initialize` handshake response. Clients use this field
  for telemetry and version gating, so the wrong value misled them.
  One-line fix: `Server("codevira", version=__version__)`.
- **`get_session_context()` read the wrong dict key** (pre-existing bug).
  `list_open_changesets()` returns `{"open_changesets": [...], ...}`, but
  `get_session_context` looked for `"changesets"`. The `open_changesets`
  field in the session-context response was **always empty** in production.
  Tests didn't catch it because mocks used the same wrong key.

- **`GlobalDB` concurrent-open race condition** (pre-existing bug — latent
  since v1.6's centralized storage introduced shared `~/.codevira/global.db`).
  `PRAGMA journal_mode=WAL` requires an exclusive lock and — unlike normal
  SQL — does NOT honour `sqlite3`'s `busy_timeout`. When multiple processes
  or threads opened the same fresh database concurrently (e.g. several
  projects' first-ever `codevira register` running in parallel, or the
  `global_sync` background export racing the MCP server thread), one or
  more would raise `OperationalError('database is locked')` and silently
  fail to register. The test `test_concurrent_access_from_threads` was
  flaky at 60% failure rate, hinting at the real issue. Fixed with WAL-
  enable retry loop + short-circuit when WAL is already active. Stability
  verified at 20/20 passes across 20 test runs.

### Changed

- **Focus-weighted `recent_decisions` in `get_session_context()`**. Instead
  of chronological "newest 3", decisions are now ranked by what the agent
  is currently focused on:
  1. Open changeset with `files_pending` → focus = first file path of the
     most-recently-created changeset.
  2. Strong `current_phase.next_action` signal → focus = extracted keywords
     (rejects short or stop-list-only actions like "continue work").
  3. Otherwise → chronological fallback (unchanged behaviour).
  If focus returns fewer than 3, the list pads with `get_recent_decisions()`.
  New response field `focus_source` (`"open_changeset:<id>"`, `"next_action"`,
  or `null`) lets the agent see *why* it got these decisions.

- **Smarter `search_decisions()` ranking**. SQL now adds `file_path` to both
  the WHERE clause and a CASE-based ORDER BY:
  `file_path match (0) > decision text (1) > context (2) > summary-only (3)`,
  then newest first within each tier. Searching for `"src/auth.py"` now
  surfaces file-path matches even when the decision text doesn't mention
  the path.

- **Decision dedup on write**. `log_session()` now skips a new decision
  if it has a `file_path` and its token-set overlaps ≥ 80% with any of
  the 5 most recent decisions for that same file. The session row is
  always created; only redundant *decisions* are dropped. Short
  decisions (< 3 tokens) and decisions without `file_path` are always
  inserted.

### Added

- `focus_source` field on `get_session_context()` response (≈10 tokens).
- `mcp_server.tools.learning._infer_focus()` — pure helper, module-private.
- `indexer.sqlite_graph._is_duplicate()` — pure token-overlap helper,
  module-private, independently testable.
- **`codevira configure`** — new CLI subcommand. Scans your project
  (gitignore-aware via existing `discover_source_files()`), shows discovered
  directories and file extensions with counts, lets you pick via a numbered-
  list prompt, writes the choices back to `.codevira/config.yaml`, and offers
  to rebuild the index. Non-interactive:
  `codevira configure --dirs src,lib --extensions .py,.ts --no-reindex`.
  Solves the AgentStore-style "0 chunks indexed" case where
  `auto_detect_project()` mis-guesses a monorepo layout.
  When `config.yaml` is missing (normal state after `codevira register` but
  before the first MCP tool call), `configure` auto-bootstraps it in full
  parity with `auto_init`'s first-init path: writes `metadata.json` (rename-
  resilient lookup via `git_remote`) and registers the project in
  `~/.codevira/global.db` for cross-project intelligence. Missing these on
  earlier drafts would have left the project invisible to rename-resilient
  path lookup and absent from global memory until the first session log.
- **Zero-chunks safety hint at index time.** When `codevira index --full` or
  `codevira index` (incremental, project-wide) matches no files against your
  `watched_dirs` + `file_extensions`, the indexer now prints a one-line
  remedy pointing at `codevira configure`. Output goes to **stderr** (not
  stdout) so the hint never leaks into the MCP JSON-RPC wire when
  `start_background_full_index` runs during auto_init inside the MCP server
  process. Also logged at WARNING level so background invocations
  (auto-init, launchd watcher) leave a trace regardless of terminal
  capture. Does NOT fire for caller-scoped incremental runs (e.g. the
  `refresh_index` MCP tool targeting a specific file) — zero matches there
  is the caller's choice, not a misconfiguration.
- `codevira register` success banner now nudges toward `codevira configure`.

### Internal

- 87 new tests:
  - 34 for v1.8 memory sharpening (focus inference priority rules, ranking
    tier ordering, dedup threshold behaviour, session-row-always-created
    invariant, NULL file_path fallback, session_id filtering + new ranking SQL)
  - 43 for `codevira configure` (scan_project with centralized-mode
    decoupling + skip_dirs honoring, multi-select prompt incl. non-TTY
    fallback + Ctrl+C clean-abort, config writer preserve/dedupe/idempotency,
    orchestrator edge cases incl. bootstrap on missing config, dry-run disk
    safety, corrupt-YAML handling, empty-extensions safety, PermissionError
    friendly wrapper, `--dirs`/`--extensions` normalization)
  - 10 for the zero-chunks hint (unit tests of the helper proving it writes
    to stderr not stdout + integration tests proving it fires ONLY for full
    or project-wide-incremental scans, not caller-scoped or normal "no files
    changed")
- Full test suite: **1,398 passing, 0 deterministic failures** (up from
  1,306 at v1.7.1 → +92). The two "pre-existing watchdog failures" that
  haunted earlier drafts of this CHANGELOG turned out to be an environment
  issue in a single dev machine (system Python 3.9 without `watchdog`);
  the pipx-installed v1.8.0 environment has all required deps. The one
  pre-existing flaky test (`test_concurrent_access_from_threads`) is now
  fixed by the `GlobalDB` WAL-enable retry loop described above.

### Verified environments

- **macOS (APFS)** + Python 3.9 system + Python 3.11 pipx: full regression
  passes; all interactive + non-interactive flows manually verified on three
  real projects (AgentStore, UDAP, ToolsConnector).
- **Cross-process + thread concurrency**: stress-tested (12 threads × 20
  writes, 8 subprocesses × 25 writes, 100 concurrent-read/write cycles) —
  0 errors, 0 data loss.

### Unverified environments / known gaps (candidates for v1.8.1 or v1.9)

- **Windows**: `os.replace` atomicity weakens when the destination is open
  by another process. If a Windows user has Claude Code reading
  `config.yaml` at the moment `codevira configure` writes it, the write
  may fail with `PermissionError`. Pre-existing risk; v1.8 does not fix
  and does not regress. Windows smoke-testing is a v1.9 scope item.
- **Network filesystems (NFS, SMB)**: atomic-replace guarantees are weaker
  on network FS. Unlikely in solo-dev environments (codevira's target);
  possible in enterprise setups.
- **Python 3.10, 3.12**: The APIs `codevira configure` uses are stable
  across 3.10+. Syntax-verified against 3.10+. **Python 3.13.7
  empirically verified** during v1.8 install validation (full pipx
  install + MCP handshake working). 3.10 and 3.12 are syntax-verified
  only. CI on all Python versions is a v1.8.x task.
- **Case-insensitive filesystem slugs**: On macOS APFS (default), paths
  differing only in case (`~/Documents` vs `~/documents`) produce
  different slugs for the same physical directory, creating split state.
  Pre-existing since v1.5 — fixing requires a migration step for existing
  users and is scoped to v1.9.
- **Interactive TTY automated coverage**: The interactive prompt flow is
  tested via mocked stdin + `sys.stdin.isatty`. A real terminal session
  was manually verified during development; automated TTY testing (via
  pexpect or similar) is a v1.8.x nice-to-have.
- **MCP client post-upgrade reload**: `codevira register` writes config;
  each MCP client (Claude Code, Cursor, Windsurf, Antigravity, Claude
  Desktop) needs to reload to see changes. Verified for Claude Code.
  Other clients may have edge cases that surface post-release.

### Known test flake (NOT v1.8; pre-existing)

- `test_chunk_error_continues_to_next_file` fails ~3/10 times in the full
  suite on Python 3.9 (system) due to a chromadb+pydantic version
  incompatibility raising `TypeError` during `import chromadb`, which
  `_check_search_deps()` doesn't catch (it only catches `ImportError`).
  **Not introduced by v1.8 and not touched by v1.8 code paths** — verified
  by measuring the same 3/10 flake rate on clean v1.7.1. v1.8 deliberately
  does not widen the exception catch because it would silently mask real
  dep issues; a proper fix belongs in a targeted follow-up PR with its own
  test coverage. Does not affect production users — the condition requires
  a specific dev environment (Py 3.9 + mismatched chromadb/pydantic).
- Regression guards added by the binocular review pass:
  - `test_centralized_mode_data_dir_and_project_root_decoupled` — catches
    the production bug where `data_dir.parent` was used where
    `get_project_root()` was required (centralized mode v1.6+).
  - `test_bootstraps_config_when_missing` — catches the workflow where
    `codevira register` was run but config.yaml hasn't been written yet
    (auto_init hadn't fired because no MCP tool call had happened).
  - `test_bootstrap_respects_dry_run` — catches bootstrap writing disk
    during `--dry-run`.
  - `test_fires_on_stderr_when_not_quiet` — catches zero-chunks hint
    leaking to stdout, which would corrupt the MCP JSON-RPC wire in stdio
    mode.
  - `test_empty_extensions_non_interactive_errors_exit_2` — catches
    `--extensions ""` being silently accepted, which would write an empty
    `file_extensions: []` and re-create the zero-chunks bug.
  - `test_ctrl_c_in_prompt_returns_exit_0` — catches KeyboardInterrupt
    propagating a traceback to the user.
  - `test_permission_error_on_write_exits_1` — catches PermissionError /
    OSError propagating a traceback when config.yaml isn't writable.
  - `test_honors_user_skip_dirs_from_config` — catches scan_project
    ignoring the user's explicit skip_dirs in config.yaml.

### Known limitations

- A running file-watcher or live MCP server session won't pick up config
  changes until it restarts (the watcher snapshots `watched_dirs` at boot).
  Restart your AI tool after `codevira configure` to apply changes.
- `yaml.safe_dump` doesn't preserve comments in `config.yaml`. First-time
  configs are auto-generated and have no comments; users who hand-edited
  may see formatting normalized after `codevira configure` rewrites the file.

### Unchanged (intentionally)

- No new MCP tools. No new tables. No schema migration.
- `search_decisions()` method signature unchanged.
- `log_session()` method signature unchanged.
- `get_session_context()` keys are additive — no removals.
- `auto_init.py`, `detect.py`, `gitignore.py`, and `metadata.json` writer
  untouched; `codevira configure` reuses all existing detection machinery.

---

## [1.7.1] — 2026-04-22 — Search Timeout Fix & Version Display

Two small but user-visible fixes on top of v1.7.0.

### Fixed

- **`search_codebase` timeout on first call** (reported by a user testing
  on Antigravity). The embedding model (`all-MiniLM-L6-v2`) was being
  instantiated fresh on every MCP tool call, which triggered a ~90MB
  download + PyTorch init on first ever use (30-60s on slow networks)
  and 1-3s of re-init overhead on every subsequent call. Antigravity's
  ~30s MCP tool timeout killed the query before the model finished loading.

  Three-layer fix:
  1. Module-level cache for the chroma client + embedding function,
     keyed by `db_dir`. Subsequent calls are now instant.
  2. Background `prewarm_embedding_model()` spawned at server startup
     (both stdio and HTTP transports). Model loads in parallel with
     the MCP handshake window.
  3. Cold-path timeout guard: if a query arrives while warmup is still
     in progress, returns `{"status": "warming", ...}` within 10 seconds
     instead of blocking until the MCP timeout fires. The agent gets a
     clean retryable response.

- **`codevira register` banner showed hardcoded `v1.6`** after upgrading
  to v1.7.0. Now reads `mcp_server.__version__` dynamically. Same fix
  applied to `metadata.json` version field written during auto-init and
  migration.

---

## [1.7.0] — 2026-04-18 — Token Efficiency & AI-First Tool Design

**The biggest release since v1.0.** We realized Codevira was dumping 15k-60k
tokens per session into AI agent context windows — defeating the entire
"92% token reduction" value prop. This release redesigns tool responses
around what agents actually need, not what the database can return.

### Changed — Dependency model
- **`chromadb` + `sentence-transformers` now required** (was `[search]` extra).
  `pip install codevira` installs all 36 MCP tools out of the box.
  Trade-off: ~500MB install (ML runtime) vs. ~50MB. Eliminates the
  "why doesn't semantic search work?" confusion.
- **`[search]` extra kept as no-op alias** for backwards compatibility.

### Changed — Token-efficient tool responses (the big one)

Every high-traffic tool now returns a **summary by default**, with opt-in
full data. On a 500-node project, a single agent session went from ~60k
tokens to ~5k.

- **`get_session_context`**: Compacted ~4k → ~800 tokens. Dropped
  `global_intelligence`/`indexing_progress` (admin data, not session data).
  Truncated decision/summary text. Nested `current_phase` at top level.
- **`get_node(path)`**: Default returns counts (`rules_count`,
  `dependencies_count`) + flags. Pass `full=True` for the full arrays.
  Typical response: ~100 tokens (was 500-3000).
- **`get_impact(path)`**: Default returns 10 affected files + protected/
  high-stability counts. Pass `summary_only=True` for just counts
  (~80 tokens — perfect for gate checks before modifying).
- **`search_codebase(query)`**: Default returns file/symbol pointers only.
  Pass `include_content=True` to inline chunk source (500-3000 tokens per
  match). `limit` capped at 20.
- **`search_decisions(query)`**: Default limit 5 (was 10), context truncated
  to 150 chars. Pass `full=True` for untruncated text. `limit` capped at 20.
- **`get_history(file)`**: Default limit 5 (was 20), text truncated.
  Pass `full=True` for untruncated. `limit` capped at 50.
- **`get_full_roadmap`**: Completed phases summarized (number + name + date
  + decision_count) instead of inlining all `key_decisions`. Pass
  `include_decisions=true` for the old behavior.
- **`list_nodes`**: Paginated (50 per page, max 500) with `offset` support.
  Response includes total count + per-layer distribution.

### Changed — AI-facing MCP tool surface trimmed to 23 tools (was 36)

12 tools moved to admin-only — they still work via `call_tool` dispatch
but are **hidden from `list_tools()`**. AI agents only see tools they
should use. The hidden tools are either:
- Dashboard/reporting (human workflows): `get_full_roadmap`,
  `get_project_maturity`, `find_hotspots`, `analyze_changes`, `get_graph_diff`
- Bulk discovery (replaced by targeted queries): `list_nodes`, `add_node`
- Background automation (self-managed): `refresh_graph`, `refresh_index`
- Redundant with session_context: `get_preferences`, `get_learned_rules`
- Dumps too many tokens: `export_graph` (can be 50k tokens)

Admins can still call these via CLI. Prompts like `architecture_overview`
still reference them server-side.

### Added
- **Non-blocking `refresh_index`**: Returns in <100ms with
  `{"status": "Refresh started in background"}`. Heavy work (graph regen +
  semantic embedding) runs in a daemon thread. Previously, this hung AI
  agents for minutes on 500+ file projects.
- **`codevira clean` command**: One-shot removal of all Codevira data, IDE
  configs, and services. `--all`, `--dry-run`, `-y` supported.
- **Google Antigravity global mode**: `codevira register` now includes
  Antigravity with a single global entry (was missing + wrong config path).
- **Browser-friendly landing page**: `GET /` on HTTP server returns helpful
  HTML with setup instructions for browsers. API clients still get JSON.

### Fixed
- **`refresh_graph` ignored its `file_paths` parameter** — dead code that
  always regenerated the entire graph. Cleaned up.
- **`generate_graph_sqlite` crashed on macOS system paths**: Now skips
  `Library`, `System`, `Applications`, `node_modules`, `.venv`, etc.,
  and catches `OSError`/`ValueError` per-entry so one bad symlink doesn't
  abort indexing.
- **Crash log test isolation**: `crash_logger._get_log_dir()` now uses
  `get_global_home()`. Tests no longer pollute the real user's crash log.
- **`_get_embedding_fn` ValueError not caught**: When chromadb is installed
  but sentence-transformers isn't, chromadb raises `ValueError`. Now caught.
- **Playbook `add_route` → `add_tool`**: The valid task type was renamed
  in code but the description still said `add_route`. Fixed.
- **Antigravity config path**: Was wrong (`~/.gemini/settings/`). Now uses
  the correct `~/.gemini/antigravity/mcp_config.json`.

### Added — Post-release enhancements (merged into 1.7.0)

- **`codevira status` is now fast** (~200ms for uninitialized projects,
  ~1s for initialized). Was ~5-6s because it was loading the ~90MB
  sentence-transformers embedding model just to count chunks. Now uses
  `collection.count()` which doesn't need the embedding function, and
  short-circuits entirely when there's no graph DB yet.
- **`codevira status --global`** flag shows launchd service state +
  cross-project memory stats in a dedicated panel. Works on both
  initialized and uninitialized projects.
- **`codevira status --check-stale`** flag opt-in for the slow SHA256
  file-walk (was always-on, made status take 5s+).
- **`codevira clean --legacy`** — remove `.codevira.migrated/` backup
  directories accumulating across all initialized projects. Shows size
  and confirms before deletion.
- **`logs.retention_days` actually works now** (was dead config in earlier
  versions). Opt-in only — default 0 keeps sessions/decisions forever.
  Set > 0 for privacy-driven time-bounded history. Runs at most once
  per 24h at server startup.
- **HTTP/HTTPS transport marked as PREVIEW** (single-project only). The
  server binds to one project at startup and cannot switch contexts per
  request. Multi-project HTTPS routing via MCP `initialize` `rootUri` is
  the top v1.8 priority. `codevira serve` prints a preview warning on
  startup. README / FAQ / PROTOCOL updated to position stdio as the
  clear recommendation for multi-project work.
- **Dead-code audit** — removed 4 unused functions (`find_project_by_remote`
  in global_db, `get_file_outcome_summary`, `add_open_changeset`,
  `remove_open_changeset`), renamed `get_changeset` → `_get_changeset`
  (was module-private usage only). Wired up 3 unused-but-useful functions
  (`launchd_status`, `cleanup_legacy_dir`, `get_global_stats`) into the
  CLI where they belong.
- **Open-source readiness pass** — removed stray test-playground files
  from git, fixed PR template typo (`mcp-server` → `mcp_server`), replaced
  hardcoded author username in docstring examples (`/Users/sachin/...`
  → `/Users/alice/...`), added `__all__` + `__version__` to
  `mcp_server/__init__.py`, removed duplicate `requirements.txt`.

### Tests
- 1,306 tests passing (added 15 new tests for `log_retention.py`)

---

## [1.6.2] — 2026-04-16 — Crash Log Isolation & Browser UX

### Fixed
- **Crash log test isolation**: `crash_logger._get_log_dir()` now uses
  `get_global_home()` instead of hardcoding `~/.codevira/logs/`. Tests
  no longer pollute the real user's crash log with pytest mock tracebacks.
- **`_get_embedding_fn` ValueError not caught**: When chromadb is installed
  but sentence-transformers isn't, chromadb wraps the ImportError as a
  ValueError. `_get_embedding_fn` now catches both and re-raises as
  ImportError for consistent handling by callers.

### Added
- **Browser-friendly landing page**: `GET /` on the HTTP server now returns
  a helpful HTML page for browsers (with setup instructions) instead of
  just JSON. API clients with `Accept: application/json` still get JSON.

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
