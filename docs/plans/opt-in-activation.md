# Plan — Opt-in project activation (stop auto-adopt)

> Status: **IMPLEMENTED — Phases 1–6 shipped (2026-07-15). Phase 7 dropped
> (redundant), Phase 8 backlog.** D1–D4 LOCKED. See "Execution outcome" below.
>
> **Locked decisions:** D1 = `hint` (reads empty+hint, writes refuse+hint) ·
> D2 = **refuse writes** until `codevira init`, with actionable hint ·
> D3 = marker is the in-repo `.codevira/config.yaml` · D4 = ghost-dir cleanup
> is **backlog** (a separate `codevira untrack` follow-up; the gate stops NEW adoption).

## Execution outcome (2026-07-15)

Shipped on `main` as 6 commits (`4d8ecbe` Phase 1 → Phase 6). Full suite: **3058
passed**; the only red is a **pre-existing** `mcp.types`-mock test-isolation
leak unrelated to opt-in (14 tests, all green in isolation — decision `D00011E`).

- **Phase 1 — predicate** (`mcp_server/opt_in.py`): `is_project_opted_in`,
  `tracking_mode`, `activation_allowed`. **Hardened during Phase 5:** the marker
  is accepted in the in-repo **OR** centralized `config.yaml` — the v1.6
  `migrate_to_centralized` renames an in-repo `.codevira/` →
  `.codevira.migrated/` and moves the store to the centralized dir, which
  destroyed an in-repo-only marker (also bites a git-clone onto a fresh
  machine). Cache is **positive-only**: a negative is always re-stat'd, so a
  fresh `codevira init` (CLI) is seen immediately by a long-lived MCP server —
  this subsumed the Phase-7 cache-invalidation work.
- **Phase 2 — centralized bootstrap** gated (`auto_init` + `_repair_init`).
- **Phase 3 — graph-read** gated (the dominant vector; `tools/graph.py`
  get_node/get_impact/query_graph/refresh_graph guard before `_get_db()`).
  The engine blast-radius signal reads `get_impact`, so the engine goes inert
  for un-init'd projects automatically.
- **Phase 4 — startup registration** gated (`global_sync`).
- **Phase 5 — dispatch gate** (`server.py call_tool`): the PRIMARY chokepoint —
  reads inert+hint, writes refuse+hint; every dispatched tool classified
  (READ_TOOLS/WRITE_TOOLS + a completeness test).
- **Phase 6 — automatic non-MCP write vectors.** The plan's blanket
  `ensure_dirs` guard was **reverted** — too blast-heavy (hundreds of direct/CLI
  writes legitimately scaffold tmp projects, and the MCP write surface is
  already refused at the dispatch gate). Instead gated the paths that fire
  AUTOMATICALLY for a merely-opened project: the **Claude Code hook entry**
  (`claude_code_hooks.handle` — the sole CLI hook chokepoint: no policy eval,
  no prompt-capture, no memory fan-out for non-opted), **`memory_fanout.flush`**,
  **`run_startup_migrations`**, and the **roadmap stub** persistence.
- **Phase 7 — DROPPED as redundant.** The hardened marker (`is_project_opted_in`,
  in-repo OR centralized `config.yaml`) already distinguishes real projects from
  ghosts, so a `global.db explicit_init` column adds nothing a future
  `codevira untrack` (D4) can't get from the predicate. The cache-invalidation
  half was subsumed by positive-only caching.
- **Phase 8 — backlog (D4).** ~60 existing graph-only ghost dirs stay on disk;
  a separate `codevira untrack` removes them. The gate stops NEW adoption.

**Grandfathering:** existing real projects (in-repo `.codevira/config.yaml`)
classify as opted-in with zero migration; graph-only ghosts classify as
not-opted-in and go inert. `CODEVIRA_AUTO_ADOPT=1` restores pre-v3.7.0 behavior.

## Problem

With the v3.7.0 **single global MCP registration**, codevira's tools are available
in every IDE window. Today it **auto-adopts any project you open** — it creates a
data dir and starts tracking without you ever running `codevira init`. The user
has ~71 projects in `~/.codevira/projects/`; most were never deliberately tracked.

**The user's requirement:** `init` should be the explicit opt-in. Codevira tracks
**only** the projects the user chose; it stays **inert** everywhere else. Must be
**non-breaking** for projects already tracked.

## The auto-adopt surface (what actually creates dirs) — VERIFIED

There are **two storage models, and FOUR creation vectors**. Both models auto-adopt.

| # | Vector | File:line | Fires on | Notes |
|---|--------|-----------|----------|-------|
| 1 | **Graph read (DOMINANT)** | `indexer/sqlite_graph.py:50-56` via `mcp_server/tools/graph.py:_get_db` (get_node:90, get_impact:272, query_graph:521, get_signature, get_code) | any **read** (`get_impact` is called before every edit per CLAUDE.md) | `SQLiteGraph.__init__` does `mkdir(parents=True)` + connect → creates `~/.codevira/projects/<key>/graph/graph.db`. **57 of 60 ghost dirs are graph-only** → this is the real culprit. |
| 2 | Centralized bootstrap | `mcp_server/auto_init.py:120-127` → `mcp_server/_repair_init.py:40-150` | first tool call (`server.py:1866`) | writes config.yaml + metadata.json + global.db row. |
| 3 | In-repo write | `mcp_server/storage/paths.py:241-278` (`ensure_dirs`) | first **write** (`decisions_store.record`, `sessions_store.write`, …) | mkdirs `<repo>/.codevira/`. Guarded by locked **D000012** forbidden-root check — do NOT weaken it. |
| 4 | Startup registration | `mcp_server/global_sync.py:register_current_project` (server.py:2539, http_server.py:439) | every server startup | adds a `global.db` projects row (no dir, but inventory pollution). |

Also relevant: `mcp_server/tools/roadmap.py:_load_roadmap:184-187` auto-creates a
stub `roadmap.yaml` under the **centralized** dir on first read (update_phase_status).

`get_data_dir()` / `_resolve_data_dir` (paths.py:422-491) do **not** create anything
— they only compute paths. Good: that's the natural home for the opt-in predicate.

## The opt-in marker (recommended)

**Use the in-repo `<project>/.codevira/config.yaml` as the source of truth.**
- Created **only** by explicit `codevira init` (`cli_init.cmd_init`). auto_init /
  repair / graph-read **never** create it.
- Empirically separates the 9 real projects (all have in-repo `.codevira/`) from the
  60 ghost dirs (none have it) — **zero migration needed; existing real projects are
  auto-grandfathered.**
- Git-committed → travels across machines/IDEs (matches the cross-tool-memory promise).
- Reuse `mcp_server/storage/paths.py:is_initialized` (line 280) but **require
  `config.yaml`** (not just the dir) so a stray empty `.codevira/` doesn't count.
- Secondary/denormalized signals (for `codevira projects` inventory, NOT the hot-path
  gate): a `global.db` `explicit_init` column + `metadata.json` `explicit_init` field.

## DECISIONS — LOCKED (D1–D4, Sachin 2026-07-15)

- **D1 — Default mode = `hint`.** For an un-adopted project: **reads** return an inert
  valid payload + a `hint` to run `codevira init`; **writes** refuse + hint. Global
  default lives in `~/.codevira/config.yaml`; env `CODEVIRA_AUTO_ADOPT` overrides
  (`1`→`auto_adopt` restores old behavior, `0`→`strict`). Rejected: `auto_adopt`
  default (the bug we're fixing) and `strict` reads (too abrupt — a bare error with no
  guidance).
- **D2 — Writes REFUSE until `init`.** A write tool (`record_decision`,
  `update_phase_status`, …) on an un-adopted project returns a friendly refusal +
  actionable hint ("run `codevira init` to track this project"), and creates NO dirs.
  Rejected: first-write-auto-inits (implicit adoption is exactly what the user wants
  control over). Known trade-off: the AI's first `record_decision` in a fresh repo
  needs an explicit `init` first — acceptable for the control it buys.
- **D3 — Marker = in-repo `.codevira/config.yaml`.** Created ONLY by explicit
  `codevira init`. Zero new state; auto-grandfathers the 9 real projects; git-committed
  so it travels across machines/IDEs. Rejected: global.db column (per-machine, doesn't
  travel) and a new sentinel file (redundant with config.yaml).
- **D4 — Ghost-dir cleanup = BACKLOG.** The gate stops NEW adoption; retroactively
  removing the ~60 existing graph-only ghost dirs is a separate, safe `codevira untrack`
  follow-up (Phase 8 is optional and NOT required for this release).

## Gate architecture

**One predicate, one primary chokepoint, defense-in-depth at the 4 vectors.**

- **Predicate** (`mcp_server/paths.py` or new `mcp_server/opt_in.py`):
  - `is_project_opted_in(root) -> bool` — in-repo `.codevira/config.yaml` exists.
    Cheap + cached (mirror the `get_data_dir` cache; invalidate with it).
  - `tracking_mode() -> "strict"|"hint"|"auto_adopt"` — `CODEVIRA_AUTO_ADOPT` env
    (`1`→auto_adopt, `0`→strict) > `~/.codevira/config.yaml` global default >
    shipped default (per D1).
  - `activation_allowed(root) -> bool` = `tracking_mode()=="auto_adopt" or is_project_opted_in(root)`.
- **Primary chokepoint:** `server.py call_tool` — gate BOTH `ensure_project_initialized`
  and the tool if/elif dispatch.
- **Defense-in-depth:** re-check at each creation vector so no path can adopt.

## Phased implementation (each phase: failing-first test → code → commit)

### Phase 1 — Opt-in predicate (foundation)
- Add `is_project_opted_in`, `tracking_mode`, `activation_allowed` (+ cache).
- **Done-when test:** opted-in project (has `.codevira/config.yaml`) → True; ghost
  (no in-repo `.codevira/`) → False; `CODEVIRA_AUTO_ADOPT=1` forces auto_adopt;
  `=0` forces strict.

### Phase 2 — Gate centralized bootstrap (vector 2)
- `auto_init.ensure_project_initialized`: after the `is_invalid_project_root` guard,
  if `not activation_allowed(root)` → return `InitStatus(ready=False, indexing=False)`
  WITHOUT `repair_incomplete_init` / background thread.
- Defensive guard at top of `_repair_init.repair_incomplete_init` (2nd caller:
  cli.py:1665-1687 self-heal).
- **Done-when test:** a tool call in a non-opted project creates NO
  `~/.codevira/projects/<key>/config.yaml`.

### Phase 3 — Gate the graph-read auto-adopt (vector 1, THE big one)
- In `mcp_server/tools/graph.py`, move the existing `graph_db_present` `is_file()`
  check (lines 138/301/541) to BEFORE `_get_db()`; when `not activation_allowed`,
  return `{found: False, hint: "run codevira init"}` WITHOUT constructing `SQLiteGraph`.
- Do **NOT** touch `SQLiteGraph.__init__` (high blast radius; `get_impact` first).
- **Done-when test:** `get_impact`/`get_node`/`query_graph` in a non-opted project
  return the hint and create NO `~/.codevira/projects/<key>/graph/graph.db`.

### Phase 4 — Gate startup registration (vector 4)
- `global_sync.register_current_project`: return `{registered: False, reason: "not opted in"}`
  when `not activation_allowed(root)`.
- **Done-when test:** server startup in a non-opted project adds NO global.db row.

### Phase 5 — Dispatch gate + read/write inert responses
- `server.py call_tool`: before dispatch, if `not activation_allowed`:
  - classify `name` into a **maintained READ set / WRITE set** map.
  - READ → inert valid payload + `hint`. WRITE → per D2 (refuse+hint or implicit init).
- Add a test that asserts **every** dispatched tool name is in exactly one set
  (new tools can't silently default to the wrong side).
- **Done-when test:** `get_session_context` (non-opted) → empty + hint;
  `record_decision` (non-opted) → refuse + hint (per D2).

### Phase 6 — Write surface + roadmap (vector 3, per D2)
- If D2 = refuse: the Phase-5 dispatch gate already catches writes; add a defensive
  opt-in check in `ensure_dirs` that raises a friendly error **without weakening the
  D000012 forbidden-root guard**. `roadmap._load_roadmap` must not auto-create a stub
  when not opted in.
- **Done-when test:** `record_decision` / `update_phase_status` in a non-opted project
  create NO in-repo `.codevira/` and NO centralized `roadmap.yaml`.

### Phase 7 — Explicit init sets the marker + inventory signal
- `cli_init.cmd_init`: add `tracking: {opted_in: true}` to the in-repo config.yaml.
- `global_db`: add `explicit_init INTEGER DEFAULT 0` via the existing lazy-ALTER
  pattern (like `git_remote`); `register_project` gains an `explicit_init` param
  (only the explicit-init caller passes True). One-time grandfather:
  `UPDATE ... SET explicit_init=1 WHERE <in-repo .codevira/ exists>`.
- `_write_metadata`: parameterize `explicit_init` (default False; explicit init True).
- **Done-when test:** after `codevira init`, `is_project_opted_in` → True and the
  global.db row has `explicit_init=1`; auto path writes `explicit_init=0`.

### Phase 8 — (Optional, per D4) cleanup command
- `codevira untrack` / `clean --unopted`: remove ghost centralized dirs (graph-only,
  no in-repo `.codevira/`) + their global.db rows, with confirmation.
- **Done-when test:** `untrack` removes a ghost dir + row, leaves opted-in projects.

## Migration / grandfathering (non-breaking)

Because the marker is the in-repo `.codevira/config.yaml`, **all currently-tracked
real projects (they have it) are automatically opted-in** — nothing goes inert on
upgrade. The 60 graph-only ghost dirs correctly classify as NOT opted-in and stop
being written to; they remain on disk until the optional Phase-8 cleanup. The
Phase-7 global.db grandfather `UPDATE` keeps inventory accurate. **Zero data loss.**

## Risks & constraints

- **Hot path:** the predicate runs on every tool call → must be cached (mirror
  `get_data_dir` cache; invalidate together).
- **Locked D000012:** do NOT weaken the forbidden-root guard in `ensure_dirs`; layer
  opt-in on top.
- **`SQLiteGraph.__init__` blast radius:** gate the `tools/graph.py` callers, never
  the primitive (run `get_impact` on it before any change).
- **Dual storage model:** the marker keys off the IN-REPO store; confirm
  `get_data_dir` still resolves correctly for opted-in projects (the centralized
  `config.yaml` is exactly what auto_init writes for un-opted projects, so key off
  the in-repo one).
- **New-tool coverage:** the READ/WRITE map needs a test so a new tool can't default
  to the wrong side.
- **`update_phase_status`/`complete_phase`** persist to the CENTRALIZED roadmap, not
  in-repo — must be caught by the dispatch gate, not an in-repo-only check.

## Test matrix (the done-when bar)

1. Predicate: opted-in True / ghost False / env overrides (Phase 1).
2. No centralized `config.yaml` created on a tool call in a non-opted project (P2).
3. No `graph.db` created by `get_impact` in a non-opted project (P3).
4. No global.db row on startup in a non-opted project (P4).
5. Read tool → empty+hint; write tool → refuse+hint (P5); every tool classified.
6. No in-repo `.codevira/` and no roadmap stub from writes in a non-opted project (P6).
7. `codevira init` → opted-in True + `explicit_init=1`; grandfather works (P7).
8. Existing real projects stay fully functional (regression — run full suite).

## Out of scope / backlog

- Retroactive un-adopt of existing ghosts (Phase 8 is optional; can be a separate
  `codevira untrack` follow-up).
- Any change to the single-MCP HTTP-multiplex (Lane B) — unrelated, see D00011B.
