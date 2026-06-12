# Codevira — Architecture (v3.3.0)

> Codevira is a lean cross-IDE decision-enforcement layer.
> ~1-2 MB per project. In your repo. No cloud. No vectors. MIT.
>
> History: v3.0.0 contracted the tool surface 33 → 23 (changesets,
> preferences, and learned_rules tools removed per the 2026-05-22
> surface-cut audit) and consolidated all storage writes through
> `mcp_server/storage/atomic.py` for crash-safety + concurrent-writer
> safety on both Posix and Windows. v3.1.0 added five memory
> subsystems (working memory, skills, spatial, consensus, reflections;
> +24 tools). v3.3.0 added preference capture (`distill_preferences` /
> `search_preferences`) — the tool surface is now 49 AI-facing tools.

## The three layers (top to bottom)

```
┌───────────────────────────────────────────────────────────────────┐
│  YOUR IDE                                                         │
│  Claude Code / Cursor / Antigravity / Codex / Windsurf / Cline    │
│  / Aider / Roo / Goose / Continue / Claude Desktop / etc.         │
└───────────────────────────────────────────────────────────────────┘
                            │
                            │  MCP (stdio JSON-RPC)
                            ↓
┌───────────────────────────────────────────────────────────────────┐
│  CODEVIRA MCP SERVER                                              │
│  ~85 MB pipx install, <100ms cold start                           │
│                                                                   │
│  Tools (49 surfaced to AI clients):                               │
│    Decisions: record_decision / search_decisions / list_decisions │
│               / list_tags / supersede_decision / check_conflict   │
│               / get_history / reaffirm_decision /                 │
│               set_decision_flag                                   │
│    Sessions:  write_session_log / get_session_context             │
│    Phases:    get_roadmap / get_phase / add_phase /               │
│               update_phase_status / defer_phase / complete_phase  │
│               / bulk_import_phases / update_next_action           │
│    Code graph: get_node / get_impact / query_graph                │
│               / get_signature / get_code / get_playbook           │
│    Memory (v3.1.0): working_* / *_skill[s] / spatial_* /          │
│               consensus_* / origin_of / reflect[ions]             │
│    Preferences (v3.3.0): distill_preferences /                    │
│               search_preferences                                  │
│                                                                   │
│  Hooks (8 active engine policies):                                │
│    PreToolUse → DecisionLock blocks Edit/Write that violate       │
│                 do_not_revert. (THE moat — no competitor blocks)  │
│                 + AntiRegression blocks reintroducing a fix       │
│                 + BlastRadiusVeto warns on high-impact edits      │
│    UserPromptSubmit → RelevanceInject injects ≤3 decisions or     │
│                       0 tokens off-topic                          │
│                       + PromptCapture records sanitized prompts   │
│                         for preference distillation (v3.3.0)      │
│    PostToolUse → PostEditGraphRefresh keeps code graph fresh      │
│                  + TokenBudgetPersist for session budgets         │
│    Stop → SessionLogEnforcer nudges write_session_log when        │
│           commits shipped without one (v3.2.0)                    │
└───────────────────────────────────────────────────────────────────┘
                            │
                            │  read/write — all writes go through
                            │  mcp_server.storage.atomic for crash-
                            │  safety + cross-process file locking
                            ↓
┌───────────────────────────────────────────────────────────────────┐
│  YOUR REPO  (committed)                                           │
│                                                                   │
│  .codevira/                                                       │
│    decisions.jsonl       ← canonical decision log (append-only,   │
│                            fcntl-locked appends, line-atomic)     │
│    outcomes.jsonl        ← git-observed kept/reverted             │
│    sessions.jsonl        ← session events                         │
│    digest.jsonl          ← slim per-decision (regenerable)        │
│    manifest.yaml         ← tag/file → id index (regenerable)      │
│    roadmap.yaml          ← phase tracking (mutation lock-guarded) │
│    enforcement.yaml      ← per-decision enforcement policy        │
│    config.yaml           ← project settings                       │
│                                                                   │
│  AGENTS.md                ← auto-generated, 5 KB cap, marker-     │
│                              bounded (preserves user content)     │
│                                                                   │
│  .codevira-cache/        ← gitignored, rebuildable                │
│    fts5.sqlite           ← BM25 keyword search index              │
│    hash-cache.db         ← file-hash change detection             │
└───────────────────────────────────────────────────────────────────┘
```

> Note on the code graph: the spec target is
> `<project>/.codevira-cache/graph.sqlite`, but v3.0.0 still ships
> with `indexer/` writing to the centralized location
> `~/.codevira/projects/<key>/graph/graph.db` and `signals.graph`
> reading from there via a fallback chain. Functional behavior is
> correct; the spec/impl reconciliation is a v3.1 follow-up.

## Concurrent-write safety

Every on-disk write in the product surface goes through one of two
helpers in `mcp_server/storage/atomic.py`:

| Helper | Use |
|---|---|
| `atomic_write_text(path, content, *, mode=None)` | Whole-file replacement (yaml, json, config). Writes to a unique tmp via `tempfile.mkstemp` in the same dir, `fsync` if supported, then `os.replace(tmp, path)` — atomic on Posix, near-atomic on Windows. |
| `atomic_write_bytes(path, content, *, mode=None)` | Same, for binary payloads. |
| `file_lock(path, *, exclusive=True)` (context manager) | Held around any read-modify-write of a shared file. Posix uses `fcntl.flock`; Windows uses a sentinel `.lock` file with a 5-second retry. Acquires the in-process `threading.Lock` first so two threads in the same process can't both pass through the OS-level lock (which on macOS is per-fd, not per-process). |

This contract was consolidated in the v3.0.0 RC hardening rounds
after the audit caught two distinct race shapes under 50-thread
stress: an *atomic-rename race* (fixed `.tmp` suffix racing on
`os.replace`) and a *lost-update race* (unlocked read-modify-write
on `manifest.yaml` and `roadmap.yaml`). Both are now provably-fixed
via regression tests in `tests/storage/test_concurrent_writes.py`
(in-process) and `tests/storage/test_cross_process_writes.py`
(20 subprocesses via `multiprocessing.spawn`).

The append-only paths (`decisions.jsonl`, `outcomes.jsonl`,
`sessions.jsonl`) get the same locking through
`jsonl_store.append` / `append_many`, which delegate to
`atomic.file_lock`. The append itself is line-atomic — concurrent
appenders never interleave bytes.

For an adversarial probe of the storage layer (process kill mid-lock,
corrupt files, symlink traversal, malformed MCP payloads, etc.), see
`scripts/chaos_smoke.py`. Run with:

```bash
.venv/bin/python scripts/chaos_smoke.py
```

The current commit passes 29 of 29 attacks.

## Source-of-truth vs cache

| Layer | Files | Lifetime |
|---|---|---|
| **Source of truth** | `.codevira/*.jsonl` + `*.yaml` | Permanent. Committed to git. Survive clones. |
| **Cache** | `.codevira-cache/*.sqlite` + `*.db` | Rebuildable. Gitignored. Per-machine. `codevira sync` regenerates. |

The cache is **always** rebuildable from the source of truth. If a teammate
clones the repo, `codevira sync` rebuilds the cache in seconds. No one
ever has to copy a binary blob.

## Decision write path

```
record_decision("Use bcrypt", file_path="auth.py", do_not_revert=True,
                tags=["security", "auth"])
    │
    ↓
mcp_server/storage/decisions_store.py::record()
    │
    ├─ 1. paths.ensure_dirs()  (creates .codevira/ + .codevira-cache/)
    │
    ├─ 2. jsonl_store.append_with_generated_id(
    │       .codevira/decisions.jsonl,
    │       {"ts": "...", "decision": "...", "tags": [...], ...}
    │     )
    │     → returns "D000007"
    │
    ├─ 3. manifest.incremental_add(.codevira/manifest.yaml, record)
    │     → adds D000007 to tags["security"], tags["auth"], files["auth.py"]
    │
    ├─ 4. fts5_index.add_decision(.codevira-cache/fts5.sqlite, record)
    │     → inserts row for BM25 keyword search
    │
    └─ 5. agents_md_generator.sync_after_write()
          → regenerates the codevira block in AGENTS.md
          → preserves any user content outside <!-- codevira:* --> markers
          → enforces 5 KB cap (drops oldest unlocked if needed)
```

Same pattern for `record_decisions` (batch), `supersede_decision`,
`mark_decision_protected`. The append-only contract means amendments
are appended as new lines that reference the original by id; the read
path merges them in order.

## Relevance-gated injection (the v2.2.0 token-budget win)

When a user submits a prompt, the `UserPromptSubmit` hook fires:

```
mcp_server/engine/policies/relevance_inject.py::RelevanceInject.evaluate()
    │
    ├─ Gate 1: event type == USER_PROMPT_SUBMIT
    ├─ Gate 2: prompt length ≥ 10 chars
    ├─ Gate 3: config mode != "off"
    ├─ Gate 4: .codevira/ initialized
    │
    ├─ Load manifest.yaml + digest.jsonl (KB range, fast)
    │
    ├─ Extract candidates:
    │   - tag_candidates  = manifest.tags ∩ prompt words
    │   - file_candidates = manifest.files ∩ prompt substrings (full + basename)
    │   - fts_candidates  = FTS5 BM25 search(prompt, limit=12)
    │
    ├─ Score each candidate:
    │   tag_score   = 0.4 per matching tag
    │   file_score  = 0.4 per file match
    │   fts_score   = 0.2 × (0.5^rank)
    │   weight      = digest.weight (outcome-based: kept=1.0, reverted=0.2, ...)
    │   total       = (tag + file + fts) × max(weight, 0.1)
    │
    ├─ Filter: drop decisions below min_score (0.10)
    ├─ Cap: top 3 by score (config.inject_max_decisions)
    │
    ├─ Render block (sorted by ID for cache stability):
    │     <codevira-context cache_key="<sha256>">
    │     Prior decisions you may want to consider:
    │
    │     🔒 **D000001** Use bcrypt for password hashing  `auth.py`
    │     • **D000003** Always use context.Context as first arg  `main.go`
    │
    │     If your current request conflicts with any of these, surface
    │     the conflict to the user before proceeding.
    │     </codevira-context>
    │
    ├─ Enforce 600-token budget (cut decisions until fits)
    │
    └─ Return PolicyVerdict.inject(context=block, metadata={...})
        OR  PolicyVerdict.allow() (off-topic → 0 tokens)
```

The deterministic byte output (sorted IDs, no timestamps) makes the
injected block identical across identical inputs → Anthropic's prompt
cache hits on repeat prompts → free tokens on subsequent calls.

## What stays unchanged from v2.1.x

These survived the v2.2.0 surgery untouched:

- **Code graph layer** (`indexer/sqlite_graph.py`, `indexer/chunker.py`,
  `indexer/graph_generator.py`) — tree-sitter parsing + SQLite graph.
  `get_impact`, `query_graph`, `get_node`, etc. work exactly the same.
- **PreToolUse enforcement** (`mcp_server/engine/policies/decision_lock.py`) —
  do_not_revert decisions still hard-block matching edits.
- **Anti-regression, scope-contract, token-budget, post-edit-refresh,
  intent-inference, blast-radius, ai-promotion, live-style policies** —
  all unchanged.
- **Setup wizard** (`mcp_server/setup_wizard.py`) — still configures
  8 IDEs in one command.
- **Per-IDE hook scripts** (`~/.claude/hooks/codevira-*.sh`) — unchanged.
- **Pillar 3 discipline scaffold** — 4 skills, gauntlet, smoke harness,
  hooks all still apply.

## The lean numbers (verified)

| Measurement | v2.1.2 | v2.2.0 |
|---|---|---|
| Pipx install size | ~450 MB | **~85 MB** |
| MCP server cold-start time | 1-3s | **<100ms** |
| Per-project disk (active project) | 40-80 MB | **~1-2 MB** |
| Worst-case disk explosion (v2.1.2 HNSW) | up to 64 GB | **structurally impossible** |
| ChromaDB / sentence-transformers / torch | required | **gone** |
| Tree-sitter grammars | 17 langs, 351 MB | **4 langs, ~5 MB** (TS/JS/Go/Rust). Long-tail via opt-in `[all-languages]` |

## Comparison with alternative tools (2026)

| Need | Best tool |
|---|---|
| Memory layer with great recall | **agentmemory** (15.5K stars, 95.2% R@5) |
| Managed cloud memory | **mem0** |
| Spec-driven workflow scaffolding | **GitHub Spec Kit** / **momentum** |
| Open standard instruction file | **AGENTS.md** (Linux Foundation) |
| **AI refuses changes violating team decisions** | **codevira** (uncontested) |
| **Blast-radius warning before refactor** | **codevira** (uncontested) |
| **Decisions versioned in repo, enforced at tool-call** | **codevira** (uncontested) |

Codevira plays in the **enforcement-layer** space, not the
memory-layer race. Every memory competitor is RECALL-only; codevira's
hooks are the only ones that physically BLOCK tool calls.

## Where to read next

- [`docs/plans/v2.2.0.md`](plans/v2.2.0.md) — full architectural plan
  (~960 lines, every design decision documented).
- [`CHANGELOG.md`](../CHANGELOG.md) — `## [2.2.0]` for the user-visible diff.
- [`docs/troubleshooting/`](troubleshooting/) — common issues + recovery.
