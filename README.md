<p align="center">
  <img src="https://raw.githubusercontent.com/sachinshelke/codevira/main/website/assets/logo.png" alt="Codevira" width="140" height="140">
</p>

<h1 align="center">Codevira</h1>

<p align="center"><strong>Stop re-explaining your codebase to every AI tool. Make the decisions stick.</strong></p>

<p align="center">
One local, in-repo memory layer that every AI coding agent you use can read and write — decisions, fix history, a code graph, your preferences — so what one tool learns, all of them know.
</p>

[![PyPI version](https://img.shields.io/pypi/v/codevira?color=orange)](https://pypi.org/project/codevira/)
[![Python](https://img.shields.io/pypi/pyversions/codevira?color=blue)](https://pypi.org/project/codevira/)
[![Downloads](https://static.pepy.tech/badge/codevira)](https://pepy.tech/project/codevira)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

Codevira also **enforces** those decisions: in **Claude Code**, a `PreToolUse`
hook physically blocks an `Edit`/`Write` that would revert a decision you marked
`do_not_revert` or re-introduce a fixed bug — before the file changes. Other IDEs
read the same decisions as `AGENTS.md` guidance (advisory, not a hard block —
their edits never route through codevira's hook engine). Local-first, MIT, no
cloud, no vectors, no account. A production pipx install is ~66 MB.

**Works with:** Claude Code · Claude Desktop · Cursor · Windsurf · Google
Antigravity · OpenAI Codex · GitHub Copilot · any MCP-compatible AI tool.

<!-- demo coming soon: a short GIF of a blocked edit + cross-tool recall will go here -->

---

## The problem — four pains, every AI project

If you've coded with AI agents on one project for longer than a week, you've felt all four:

1. **Re-explaining your codebase every session.** Every new chat starts from
   zero. You spend the first ten minutes (and thousands of tokens) catching the
   AI up on your architecture and conventions — then do it again tomorrow.
2. **AI quietly undoing your careful decisions.** You debugged a tricky retry
   policy for three hours last week. Today's session "simplifies" it, because
   nothing remembered *why* the complexity existed. Now it's broken again.
3. **Cross-tool amnesia.** Plan in Claude Code, autocomplete in Cursor, run
   tests in Antigravity — three agents, three blind copies of your project
   state, nothing carried over.
4. **Token budget burned on re-discovery.** The agent reads the same dozen
   files every session before doing any real work. You pay for the same lookups
   over and over.

Codevira is a persistent memory layer that fixes all four — for every AI tool,
on every project, on your local machine.

---

## What using it looks like

The payoff is a loop: **record once → shared everywhere → enforced later.**

**1. You (or the AI) record a decision — one MCP call, ~50 tokens.**

```text
record_decision(
  decision="Use bcrypt for password hashing",
  context="md5 considered and rejected — rainbow-table risk. Re-examine only if NIST guidance changes.",
  tags=["auth", "security"],
  do_not_revert=true,
)
→ D000412 recorded and locked.
```

It lands in `<repo>/.codevira/decisions.jsonl` — human-readable, git-committed,
visible in `git diff`. Codevira regenerates a slim `AGENTS.md` contract from it.

**2. Weeks later, a fresh Claude Code session tries to swap bcrypt for md5.**

The `Edit` goes through Claude Code's `PreToolUse` hook first. Because the diff
touches the locked decision's subject, the hook **denies the tool call** and
hands the agent the original reasoning:

```text
✗ Edit blocked — decision_lock (do_not_revert)
  D000412: "Use bcrypt for password hashing"
  context: md5 rejected — rainbow-table risk.
  The file was not modified. Surface this to the human and re-decide deliberately.
```

The regression never reaches disk. (An orthogonal edit to the same file — one
whose diff doesn't touch the decision's subject — is *allowed* through and
downgraded to a warn. Precision, not paranoia.)

**3. You switch to Cursor the next morning — and it already knows.**

You never re-explained anything. Cursor reads the same repo `AGENTS.md` (and,
over MCP, can call `search_decisions("auth")`) and sees D000412 with its full
context. A decision recorded in one tool is visible to every tool. The hard
*block* is Claude Code only today; the shared *memory* is universal.

> The honest caveat: only Claude Code's `PreToolUse` hook hard-blocks, because
> only its edits route through codevira's engine. In Cursor / Windsurf / Codex /
> Copilot the decision is strong advisory context in `AGENTS.md`, not a physical
> veto.

---

## Quick Start — three commands

```bash
# 1. Install (production install: ~66 MB pipx venv, no ML deps)
pipx install codevira

# 2. Opt this project in (writes .codevira/, AGENTS.md, .gitignore)
cd ~/Projects/my-project
codevira init

# 3. Wire codevira into every AI tool detected on this machine
codevira setup
```

By default codevira keeps decision memory **per-machine** (not committed), so
unrelated projects never bleed into each other; `AGENTS.md` and `.gitignore` are
still committed. To share memory with teammates on the same GitHub repo, run
`codevira init --shared` — it keeps `.codevira/` git-tracked, and a built-in git
merge driver reconciles concurrent edits. Open any IDE — codevira's MCP server is
ready.

> **Opt-in tracking (v3.7.0).** `codevira init` is the explicit opt-in.
> Codevira tracks **only** projects you've `init`-ed; a project you merely open
> stays inert (its tools return a "run `codevira init`" hint and nothing is
> written), so `~/.codevira/projects/` never fills with projects you didn't
> choose. Existing tracked projects are grandfathered — zero migration. Set
> `CODEVIRA_AUTO_ADOPT=1` to track every project you open instead.

**Verify:**

```bash
codevira doctor          # 18 health checks, ✓/⚠/✗ + a fix command for each
codevira replay          # browse the decisions timeline
codevira sync            # regenerate AGENTS.md from current decisions.jsonl
```

**Try it.** In your AI tool, ask: *"Use `get_session_context` to brief me on
this project."* You get a ~500-token structured project state in one tool call
instead of the AI re-reading docs.

---

## What you get

* **One memory across every AI tool.** A decision logged in Claude Code is
  visible to Cursor, Windsurf, Antigravity, Codex, Copilot — all read the same
  `.codevira/decisions.jsonl` and generated `AGENTS.md` in your repo. No
  per-tool re-onboarding, no cloud sync.
* **Enforcement, not just notes (Claude Code).** Decisions you mark
  `do_not_revert` get a `PreToolUse` hook that refuses violating edits, plus an
  Anti-Regression guard that blocks re-introducing a previously-fixed bug. Every
  guard ships a per-policy warn/off env kill-switch and a global
  `CODEVIRA_ENGINE=0`. Other IDEs get the same decisions as advisory `AGENTS.md`.
* **One-command setup.** `pipx install codevira && codevira setup` detects
  installed AI tools via strong signals (binary on PATH + valid config file) and
  configures only what's actually installed. `--force` overrides a missed detect.
* **Local-first, no ML.** Everything lives in `<repo>/.codevira/*.jsonl` (git),
  with rebuildable caches under `~/.codevira/`. Decision search is pure
  keyword/BM25 (SQLite FTS5) — no vectors, no ChromaDB, no sentence-transformers,
  no torch, nothing phones home. ~1–2 MB of committed memory per project.
* **Frugal by design.** Tools return summaries by default; warm tool calls
  return in a few milliseconds; the server cold-starts in well under a second
  with no ML model to load.
* **Concurrent-safe under multi-IDE load.** Every write is a crash-safe atomic
  write behind a Posix `fcntl.flock` (Windows sentinel fallback), so two IDEs on
  one project don't race — verified by thread, subprocess, and adversarial chaos
  tests. Details in [Concurrency & safety](#concurrency--safety).

**Latest:** **v3.7.1** — a reliability release. Fixes a migration bug that could
strand a project's memory, several defects that bound a session to the **wrong
project**, and a cluster of decision-memory correctness bugs; makes IDE-config
writes non-destructive. Adds `codevira init --shared` (opt-in **team-shared
memory**) and a `PreToolUse` enforcement hook for **Antigravity** that surfaces
and can block decision-reverting edits. Upgrading is automatic — codevira
migrates on the first server start, no manual steps. All model-free, all local.
See the [CHANGELOG](CHANGELOG.md#371--2026-07-20).

---

## How It Works

Codevira is a [Model Context Protocol](https://modelcontextprotocol.io) server
that runs locally and gives any AI tool a structured, queryable memory of your
codebase.

```
┌─────────────────────────────────────────────────────────────────┐
│  IN THE PROJECT REPO (committed to git)          (selected)     │
│                                                                 │
│   AGENTS.md                  ≤5 KB slim contract, auto-generated │
│      ↑                                                          │
│   .codevira/                                                    │
│     decisions.jsonl          full text + metadata (append-only) │
│     digest.jsonl             slim summary for prompt injection  │
│     outcomes.jsonl           kept/reverted from git observation │
│     manifest.yaml            tag→ids, file→ids index (regen)    │
│     enforcement.yaml         which decisions hard-block         │
│     config.yaml              project settings                   │
│     sessions.jsonl           session events                     │
│     roadmap.yaml             phase tracking                     │
│     (also: skills / reflections / preferences / learned rules)  │
│                                                                 │
│   .codevira-cache/           gitignored, rebuildable            │
│     fts5.sqlite              FTS5 index over decisions.jsonl    │
│     hash-cache.db            file change detection              │
│     working.jsonl            intra-session scratchpad           │
│   (the code graph is a per-project SQLite db under ~/.codevira/)│
└─────────────────────────────────────────────────────────────────┘
                              ↑ MCP / hooks ↓
┌─────────────────────────────────────────────────────────────────┐
│  PIPX INSTALL (~66 MB venv, ~/.local/pipx/venvs/codevira)      │
│   codevira (CLI + MCP server)                                   │
│      - pure Python; no chromadb / sentence-transformers / torch │
│      - server cold-start well under 1 s; warm tool calls ~2 ms  │
└─────────────────────────────────────────────────────────────────┘
                              ↑ stdio MCP ↓
┌─────────────────────────────────────────────────────────────────┐
│  IDE (Claude Code / Cursor / Windsurf / Antigravity / Codex /…) │
│                                                                 │
│   UserPromptSubmit → codevira hook → relevance-gated inject     │
│   Edit / Write → PreToolUse → block if do_not_revert violated   │
│   PostToolUse → Post-Edit Graph Refresh (+ working-mem fanout)  │
│   Stop → Token Budget / Session-Log Enforcer                    │
└─────────────────────────────────────────────────────────────────┘
```

### Token-efficient by design

AI context windows are precious. Tools return **summaries by default** with
opt-in full data:

- `get_node(path)` — ~100 tokens by default (counts + flags); `full=true` for
  the full rules array.
- `get_impact(path)` — up to 10 affected files; `summary_only=true` for just
  counts (~80 tokens) before you dig deeper.
- `search_decisions(query)` / `list_decisions()` — top 5 truncated matches by
  default; `full=true` for verbatim text, `summary_only=true` for one-line
  summaries, then `expand(ids=[…])` to pull only the few full records you want.

**Shrink the tool surface itself.** The advertised `tools/list` is a fixed
per-session cost (~8K tokens for the full 51-tool surface). Set
`CODEVIRA_TOOL_PROFILE=lean` in the MCP server's `env` block to advertise only
the 12 daily-driver tools — a ~71% token trim of `tools/list`. Hidden tools
still work when called explicitly.

---

## MCP tool surface (deep dive)

**51 tools** are surfaced to AI clients via `tools/list` (a 52nd, `refresh_graph`,
is registered but hidden — humans invoke it via `codevira sync`). All 51 carry
MCP `ToolAnnotations`
(`readOnlyHint` / `destructiveHint=false` / `idempotentHint`): 29 are read-only
and can run without a confirmation prompt, and **no MCP tool is destructive** —
the only destructive ops (`reset` / `uninstall`) are CLI-only, never exposed over
MCP. Good to know for anyone wiring codevira into an autonomous agent.

The **lean 12** (`CODEVIRA_TOOL_PROFILE=lean`) are deterministic and worth
knowing verbatim, since they're what you keep:

```text
get_session_context · get_impact · get_node · get_roadmap · search_decisions
list_decisions · expand · record_decision · update_phase_status
complete_phase · update_next_action · write_session_log
```

### Reads — the memory surface

| Tool | Description |
|---|---|
| `get_session_context` | **THE "catch me up" call.** ~500 tokens: current phase, next action, recent decisions, top tags, last session brief. |
| `search_decisions(query)` | FTS5/BM25 over `decisions.jsonl`. Top 5 truncated by default; `full=true` / `summary_only=true`. `all_projects=true` searches every registered repo, tagging each result with its project. |
| `expand(ids=[…])` | Fetch full records for just the ids you care about — the summary-first complement to `search_decisions` / `list_decisions`. Read-only; in the lean 12. |
| `list_decisions` | Paginate / filter: `since_date`, `file_pattern`, `protected_only`, `tags`, `include_superseded`. |
| `list_tags` | All tags with decision counts. |
| `get_history(file_path)` | Recent decisions touching a file. |
| `check_conflict(decision_text)` | Surface duplicate / contradictory decisions BEFORE you write. |

### Writes — capturing decisions

| Tool | Description |
|---|---|
| `record_decision` | Capture a decision. `do_not_revert=true` triggers Claude Code `PreToolUse` enforcement; `symbol="login"` scopes the lock to one function/class. In v3.7.0 it **supersedes** a strong unprotected near-duplicate instead of appending a twin. |
| `supersede_decision(old_id, new_decision, reason)` | Retire an old decision, link to its replacement, keep the audit trail. |
| `mark_decision_outdated(decision_id, reason)` | **v3.7** — retire a decision that's simply no longer true (no successor) so it stops surfacing. Reversible via `set_decision_flag`. `do_not_revert` needs `force=True`. |
| `reaffirm_decision(decision_id)` | Re-confirm a soft-expired `do_not_revert` lock. |
| `set_decision_flag(decision_id, …)` | Toggle `do_not_revert` / tags / `is_outdated`. |
| `write_session_log` | Structured session record. |

### Roadmap

`get_roadmap` · `get_phase` · `add_phase` · `update_phase_status` ·
`update_next_action` · `complete_phase` · `defer_phase` · `bulk_import_phases`.

### Code graph

`get_node` (file metadata) · `get_impact` (blast radius) · `query_graph`
(function-level callers/callees/tests/dependents/symbols) · `get_signature`
(all public symbols) · `get_code` (source of one symbol) · `get_playbook`
(curated rules for `add_tool` / `add_service` / `add_schema` / `debug_pipeline`
/ `commit` / `write_test`). Plus the hidden `refresh_graph`.

### Memory subsystems (v3.1.0)

| Subsystem | Tools | What it covers |
|---|---|---|
| Working memory (4) | `working_add`, `working_get`, `working_promote`, `get_working_context` | Intra-session scratchpad, decay-scored (`importance × e^(−Δt/τ=6h) + 0.5·access_count`), capacity-bounded. Auto-populated by the `PostToolUse` fan-out. |
| Skill library (6) | `record_skill`, `get_skill`, `apply_skill_outcome`, `list_skills`, `supersede_skill`, `promote_skill_to_playbook` | Reusable procedures; FTS5 composite ranking (BM25 + tag-Jaccard + recency); auto-archive at 5 consecutive failures or 90 unused days (`do_not_revert` exempt). |
| Spatial (4) | `spatial_nearby`, `spatial_heat`, `spatial_neighborhood`, `spatial_affordances` | Code-as-space: activity heatmap, folder neighborhoods, what task types each area affords. |
| Consensus (5) | `consensus_check`, `consensus_status`, `consensus_propose_supersession`, `consensus_resolve`, `origin_of` | Tracks which IDE wrote each decision so cross-IDE contradictions surface. (Provenance is a cooperative signal — `CODEVIRA_IDE` is spoofable, not a security boundary.) |
| Reflections (3) | `reflect`, `get_reflections`, `list_reflections` | LLM-generated abstractions over recent decisions + sessions via MCP sampling. `reflect --from-sessions` folds local transcripts as candidates only (nothing auto-committed). |
| Preferences (2) | `distill_preferences`, `search_preferences` | Session-end distillation of your prompts into durable, user-scoped preferences in `~/.codevira/global.db`, visible from every project. |

### MCP Workflow Prompt

`onboard_session` — full project catch-up for new sessions; wraps
`get_session_context()`.

---

## Language support

| Feature | Python | TS/JS | Go | Rust | Others |
|---|:---:|:---:|:---:|:---:|:---:|
| Decision capture + search | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cross-IDE memory via AGENTS.md | ✓ | ✓ | ✓ | ✓ | ✓ |
| Roadmap / sessions | ✓ | ✓ | ✓ | ✓ | ✓ |
| Code graph + blast radius | ✓ | ✓ | ✓ | ✓ | — |
| `get_signature` / `get_code` | ✓ | ✓ | ✓ | ✓ | — |

Decisions / `AGENTS.md` / roadmap are **language-agnostic** — they work for any
language. **Code-graph and symbol tools** cover exactly Python (stdlib `ast`)
plus TS / TSX / JS / JSX / Go / Rust (bundled tree-sitter grammars — 4 pip
packages, since TSX ships inside the TypeScript grammar). For any other language
the AI `Read`s the file directly. The legacy 17-grammar `[all-languages]` pack
was removed in v2.2.0 to keep the install lean.

---

## Enforcement engine (deep dive)

Codevira ships **8 default engine policies** ("heroes"), each hooked to Claude
Code lifecycle events: `SessionStart`, `PreToolUse`, `PostToolUse`,
`UserPromptSubmit`, `Stop`.

| Policy | Event | What it does |
|---|---|---|
| **Decision Lock** | PreToolUse | Blocks an edit that touches a `do_not_revert` decision's subject. **Content-aware** (v3.5): a provably-orthogonal edit downgrades to a warn; a token-touching edit blocks. Strict file-level locking via `CODEVIRA_DECISION_LOCK_CONTENT_AWARE=0`. |
| **Anti-Regression** | PreToolUse | Blocks edits that look like reverts of previously-fixed bugs. Fix-history is scanned from `fix:` / `bug:` / `hotfix:` / `fixes #N` commit messages and self-freshens on read when git HEAD advances. *Caveat: heuristic is calibrated for small `Edit`/`MultiEdit` hunks; full-file `Write` reverts are deliberately exempt (future work).* |
| **Blast-Radius Veto** | PreToolUse | Blocks a signature-*removing/modifying* edit to a high-fan-in file (purely-additive signatures pass since v3.3). Shows the callers. |
| **Relevance Inject** | UserPromptSubmit | Injects ≤3 relevant decisions per prompt; 0 tokens when off-topic. |
| **Prompt Capture** | UserPromptSubmit + Stop | Records sanitized prompts for later preference distillation. |
| **Session-Log Enforcer** | SessionStart + Stop | Nudges (or blocks) a session that shipped commits without a `write_session_log`. |
| **Post-Edit Graph Refresh** | PostToolUse | Reindexes edited files in the background. |
| **Token Budget / telemetry** | Stop | Records outcome telemetry. |

**How verdicts combine.** The three edit guards compose into a single verdict —
**first block wins by priority** (Decision Lock 100 > Anti-Regression 80 >
Blast-Radius 50). The highest-priority block's message is what you see; the rest
are recorded as telemetry.

**Fail-open and opt-in.** The dispatcher never raises: each policy is wrapped in
try/except and returns *allow* on failure. `CODEVIRA_ENGINE=0` disables all
policies; each edit guard also has a per-policy `off|warn|block` env override
(`CODEVIRA_DECISION_LOCK_MODE`, `CODEVIRA_ANTI_REGRESSION_MODE`,
`CODEVIRA_BLAST_RADIUS_MODE`). And enforcement is **not** global across all
Claude Code projects — in any project you never ran `codevira init` on, the
hooks stay fully inert.

**Why "Claude Code only."** The hard-block path is Claude Code's real
`PreToolUse` hook. Edits from Cursor / Windsurf / Codex / Copilot go straight to
the filesystem — they never reach codevira's `PreToolUse` engine at all — so
those IDEs get the decisions as advisory `AGENTS.md` context instead.

---

## Concurrency & safety

Every on-disk write goes through `mcp_server/storage/atomic.py`: a crash-safe
atomic write (`mkstemp` + `fsync` + `os.replace`) behind a Posix `fcntl.flock`
(with an in-process `threading.Lock` first, and a Windows `O_EXCL` sentinel
fallback). Appends to the JSONL logs are line-atomic — concurrent appenders never
interleave bytes. Result: two IDEs hitting the same project don't race on
`manifest.yaml` / `roadmap.yaml` / `AGENTS.md`.

This is exercised by a 50-operation stress over a 10-thread pool, a 20-subprocess
cross-process stress (`spawn`), and an adversarial chaos harness
(`scripts/chaos_smoke.py` — 8 scenarios / 29 checks including SIGKILL during a
held lock, symlink traversal, malformed MCP payloads, corrupt-JSONL graceful
degradation, and read-only-directory hostility). See
[`docs/architecture.md`](docs/architecture.md) § "Concurrent-write safety".

---

## CLI

~26 user-facing commands; the daily-use ones:

| Command | What it does |
|---|---|
| `codevira init` | Opt this project in: `.codevira/` + AGENTS.md + .gitignore + git merge driver. Add `--shared` to commit memory for team sharing (default keeps it per-machine) |
| `codevira setup` | Detect installed AI tools + write MCP configs + Claude Code hooks |
| `codevira doctor` | 18 health checks (read-only; ✓/⚠/✗ + a fix command each) |
| `codevira status` | Index health + project state |
| `codevira projects` | List tracked projects with staleness; `projects archive <name>` drops one |
| `codevira index` | Build / refresh the code-graph cache |
| `codevira sync` | Regenerate AGENTS.md + manifest + digest from `decisions.jsonl` |
| `codevira repair-ids` | **v3.7** — detect/repair cross-engineer decision-id collisions (`--apply`; `--semantic` reports near-duplicates) |
| `codevira observe-git` | Classify past decisions as kept/modified/reverted from git history |
| `codevira replay` | Browse the decisions timeline (terminal / markdown / HTML) |
| `codevira search <query>` | Search decisions from the terminal (FTS5/BM25); `--all-projects`, `--json` |
| `codevira graph` | Render an interactive, offline HTML viewer of decision memory |
| `codevira export` / `import` | Back up / restore project memory + global learning across machines |
| `codevira clean` / `reset` | Remove orphaned data / destructive cleanup (auto-exports first) |
| `codevira uninstall` | Reverse every system write codevira made (preserves user content outside markers) |
| `codevira serve` | Start the single-project MCP HTTP server (stdio is the daily mode) |

Run `codevira <cmd> --help` for full flags. Uninstall with `codevira uninstall`
then `pipx uninstall codevira`.

---

## Production-stable vs known-limited

| Production-stable | Known-limited |
|---|---|
| Cross-IDE decision memory via in-repo JSONL | Hard `PreToolUse` enforcement is Claude Code only; other IDEs read `AGENTS.md` (advisory, not a hard block) |
| `do_not_revert` enforcement at the Claude Code hook | Symbol tools cover Python / TS / JS / Go / Rust; other languages → the AI `Read`s the file directly |
| FTS5/BM25 decision search | Real-time multi-machine sync — by design local-first; for team sharing, run `codevira init --shared` to commit `.codevira/` |
| Per-project + cross-machine project inventory (`global.db`) | No web UI — use the `codevira://decisions` MCP resource, or `codevira replay --format html` |
| 51 MCP tools + ~26 CLI commands + 8 engine policies | The HTTP server (`codevira serve`) is single-project per launch — for daily use, stick with stdio |
| Concurrent-safe storage (Posix `fcntl.flock` + Windows sentinel), thread + subprocess + chaos-tested | Windows sentinel fallback is verified in unit tests but not yet load-tested on real Windows |
| Anti-Regression on small `Edit`/`MultiEdit` hunks | Anti-Regression does not yet detect full-file `Write` reverts; accuracy depends on `fix:` commit hygiene |

---

## Background

Want the full story — why this was built, what didn't work, how it compares to
other memory tools? Read
[How I Built Persistent Memory for AI Coding Agents](docs/how-i-built-persistent-memory-for-ai-agents.md).

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

- **Bug?** [Open a bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md)
- **Feature?** [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md)
- **Security issue?** Read [SECURITY.md](SECURITY.md) — please don't use public issues for vulnerabilities.

## FAQ & Roadmap

Common questions on setup, usage, and troubleshooting: [FAQ.md](FAQ.md).
What's built, what's next, and the long-term vision: [ROADMAP.md](ROADMAP.md).
Full release history: [CHANGELOG.md](CHANGELOG.md).

**Upgrading.** It's automatic — codevira migrates your memory on the first
server start after an upgrade, with no manual steps, and your existing decisions
stay put. If an IDE then shows the wrong project, doesn't show codevira at all,
or memory looks missing, it's almost always a stale IDE-config entry rather than
lost data. Two fixes cover most cases: remove a stray or temporary entry with
`codevira untrack <path>` (or sweep dead ones with `codevira clean --ghosts`),
then run `codevira doctor` — it names the bound project and ships the exact fix
for each ⚠/✗. Full guides:
[IDE config hygiene](docs/troubleshooting/config-hygiene.md) and
[Antigravity](docs/troubleshooting/antigravity.md).

## Star History

If Codevira saves you tokens or sanity, a star helps other developers find it.

<a href="https://star-history.com/#sachinshelke/codevira&Date">
  <img src="https://api.star-history.com/svg?repos=sachinshelke/codevira&type=Date" alt="Star History Chart" width="600"/>
</a>

## License

MIT — free to use, modify, and distribute.
