# Codevira v2.2.0 — Architecture

> v2.2.0 ships codevira as a lean cross-IDE decision-enforcement layer.
> ~1 MB per project. In your repo. No cloud. No vectors. MIT.

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
│  Tools (33):                                                      │
│    Decisions: record/record_many/search/list/list_tags/           │
│               check_conflict/supersede/mark_protected             │
│    Sessions:  write_session_log(s) / get_session_context          │
│    Phases:    get/add/update/complete/defer + bulk_import_phases  │
│    Changesets: start/update/complete/list_open                    │
│    Code graph: get_node/get_impact/query_graph/get_signature/     │
│                get_code/get_playbook                              │
│                                                                   │
│  Hooks (engine policies):                                         │
│    PreToolUse  → DecisionLock blocks Edit/Write that violate      │
│                  do_not_revert. (THE moat — no competitor blocks) │
│    UserPromptSubmit → RelevanceInject injects ≤3 decisions or     │
│                       0 tokens off-topic                          │
│    PostToolUse → outcome / scope tracking                         │
└───────────────────────────────────────────────────────────────────┘
                            │
                            │  read/write
                            ↓
┌───────────────────────────────────────────────────────────────────┐
│  YOUR REPO  (committed)                                           │
│                                                                   │
│  .codevira/                                                       │
│    decisions.jsonl       ← canonical decision log (append-only)   │
│    outcomes.jsonl        ← git-observed kept/reverted             │
│    sessions.jsonl        ← session events                         │
│    changesets.jsonl      ← multi-file work tracking               │
│    preferences.jsonl     ← extracted style preferences            │
│    learned_rules.jsonl   ← regex-extracted patterns               │
│    digest.jsonl          ← slim per-decision (regenerable)        │
│    manifest.yaml         ← tag/file → id index (regenerable)      │
│    roadmap.yaml          ← phase tracking                         │
│    enforcement.yaml      ← per-decision enforcement policy        │
│    config.yaml           ← project settings                       │
│                                                                   │
│  AGENTS.md                ← auto-generated, 5 KB cap, marker-     │
│                              bounded (preserves user content)     │
│                                                                   │
│  .codevira-cache/        ← gitignored, rebuildable                │
│    fts5.sqlite           ← BM25 keyword search index              │
│    graph.sqlite          ← tree-sitter code graph                 │
│    hash-cache.db         ← file-hash change detection             │
└───────────────────────────────────────────────────────────────────┘
```

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
