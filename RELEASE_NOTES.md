# v3.7.0 — Opt-in tracking, fresh memory, shared repos, one registration

**Released:** 2026-07-16
**Test status:** 3075 / 3075 passing

Four things users asked for, all model-free and local:

- **Opt-in project tracking.** codevira now tracks **only** projects you
  `codevira init`. A project you merely open in your editor stays **inert** —
  its tools return a "run `codevira init`" hint and nothing is written — so
  `~/.codevira/projects/` stops filling with projects you never chose (the #1
  dogfood complaint). Existing tracked projects are grandfathered in with
  **zero migration**; `CODEVIRA_AUTO_ADOPT=1` restores the old
  track-everything behavior.
- **Memory stays fresh.** `record_decision` now *supersedes* a strong,
  unprotected near-duplicate (gated on symmetric jaccard) instead of appending
  a stale twin; `get_session_context` hides superseded / outdated /
  outcome=reverted decisions; and `mark_decision_outdated(id)` retires a
  decision that's no longer true (reversible). A `do_not_revert` decision is
  never silently retired — `force=True` is required.
- **Two engineers, one repo.** Decision-id collisions from cross-branch merges
  are now visible (`read_merged` warns), deterministically repairable
  (`codevira repair-ids [--apply] [--semantic]` — a true fixed point, so it
  can't oscillate; malformed lines preserved), and auto-resolved by a git
  **merge driver** installed by `codevira init` (with a `doctor` gap-check for
  fresh clones).
- **One MCP for all projects.** `codevira init` registers **one** user-scope
  server by default (resolves the active project from workspace roots);
  `--per-project` opts out. The project-root pin is now per-request.

Also: the fix-history scan self-freshens on read; `NotebookEdit`/camelCase
edits no longer log `<unknown>`; the D000118 intra-process id-drift is fixed.

Full detail in [CHANGELOG.md](CHANGELOG.md#370--2026-07-16).

> Upgrading? `pipx install --upgrade codevira`. See [MIGRATING.md](MIGRATING.md).

---

# v2.0.0 — First public 2.0 release

**Released:** 2026-05-14
**Test status:** 2395 / 2395 passing (+1091 net new since v1.8.0)
**Previous PyPI release:** v1.8.0 (2026-04-23)

The 2.0 release closes a gap that's been a year in the making: codevira
moves from "memory layer for one developer in one IDE" to "active
guardian for every AI coding tool you use, on every project, on your
local machine." Five internal iterations (rc1..rc5 in dev tags) plus
a same-day public RC cycle (`2.0.0rc1`) of dogfood + audit +
product-credibility work consolidate into 2.0.0.

> Migrating from 1.x? See [MIGRATING.md](MIGRATING.md).

## What's new since v1.8.0

### Active guardian engine (10 heroes)
* Lifecycle hook engine intercepts every AI tool call (Edit, Write,
  prompt submit, session start) and routes it through a registered set
  of policies — making the persistent memory layer **active** instead
  of passive.
* All ten heroes shipped: Decision Lock (Hero 1), Anti-Regression
  Memory (2), Scope Contract Lock (3), Blast-Radius Veto (4),
  Cross-Session Consistency (5), Token Budget Live View (6), Live
  Style Enforcement (7), Decision Replay (8), Proactive Intent
  Inference (9), AI Promotion Score (10).

### Cross-tool universality
* `codevira setup` — one-prompt installer that detects every AI tool
  on the machine (Claude Code, Cursor, Windsurf, Antigravity, OpenAI
  Codex, GitHub Copilot, Continue.dev, Aider) and configures all of
  them at once.
* `codevira hooks install / list / uninstall` — admin commands for
  Claude Code lifecycle hooks; surgical install + clean removal.
* Per-IDE nudge files (CLAUDE.md, AGENTS.md, .cursor/rules/*.mdc,
  .windsurfrules, GEMINI.md, .github/copilot-instructions.md) all
  generated from one canonical template, so behaviour is consistent
  across every AI tool the developer uses.

### Indexing — "all the code", honestly
* `codevira init` defaults to indexing **every common source / config /
  docs extension** (.py, .js, .ts, .go, .rs, .yaml, .toml, .md, .html,
  .sql, .proto, … ~75 total) instead of narrowing to one dominant
  language. Polyglot projects no longer silently lose .yaml/.md/.html.
* `--single-language` flag preserves the legacy single-language
  narrowing for users who want it.

### CLI completeness
* `codevira projects` — single source of truth for "what projects does
  codevira know about on this machine?" with `tracked / ghost / orphan
  / stale` classification. `--json` for scripting; `--ghosts-only` for
  cleanup pairing with `clean --ghosts`.
* `codevira clean --ghosts` — surgical removal of incomplete project
  data dirs without touching tracked projects.
* `codevira inspect`-style reporting via `status --global` now agrees
  in count with `projects` and `clean` (single shared inventory helper).
* `codevira doctor` — now genuinely read-only; snapshots the projects
  dir at entry and removes any new dirs at exit, restoring the contract
  the docs always promised.
* `codevira insights` / `codevira replay` — outcome-tracker pipeline
  now classifies file-less decisions via mention-extraction (regex over
  the decision/context text), so `get_decision_confidence` produces
  signal even when decisions weren't recorded with `file_path=`.

### Tool-surface improvements
* `search_codebase` returns a structural fallback (filename + symbol
  substring matches from the graph DB) instead of erroring when the
  semantic index isn't built. Fix command surfaced explicitly.
* `get_node` / `get_impact` / `query_graph` distinguish three error
  states: "no graph DB" / "graph empty" / "file not in populated
  graph" — each with the correct `fix_command`.
* `get_decision_confidence` exposes `decisions_in_db_total` and
  `decisions_eligible_for_outcomes` plus a four-state interpretation
  so users understand WHY their `total_decisions` may be zero.
* Playbooks are now project-scoped (read from `<data_dir>/playbooks/`
  or `<project>/.codevira/playbooks/`); bundled Python defaults are
  skipped with a clear warning when project language ≠ Python.

### Honest UX
* Per-prompt `cross_session` injection (the ~1 KB "prior decisions"
  block) now has a per-project opt-out via
  `.codevira/config.yaml: project: { cross_session_mode: off }` —
  no longer env-var-only.
* Setup zero-step plans warn instead of silently reporting "Already
  up to date".
* Silent argument clamps in `replay --since`, `insights --since`, and
  `insights --top` now print visible warnings.
* `register` deprecation now names the removal version (v2.1).
* README repositioning: the "92% token reduction" claim is qualified
  with honest scope, per-prompt cost, and amortization curve.

### Doctor
* New checks: `claude_mcp_visibility`, `codeindex_freshness`,
  `semantic_search_health`, `ghost_projects`. Total: 14 checks per
  run. Each WARN/FAIL ships with the exact `fix_command`.

### Data integrity
* `register_project` now uses `ON CONFLICT … COALESCE(excluded.git_remote,
  projects.git_remote)` — subsequent registrations can't silently
  clear the `git_remote` column. Bug 20 dedup invariant holds across
  re-registration cycles.
* Auto-init self-heal runs SYNCHRONOUSLY in the calling thread of
  every CLI invocation — daemon thread death no longer leaves ghost
  data dirs in `~/.codevira/projects/`.
* One-shot dedup migration on every `GlobalDB.__init__` collapses
  legacy duplicate rows by `git_remote`.

### Setup
* macOS Apple Silicon fork-safety patch (auto-applied at indexer
  import) — eliminates the segfault on first `codevira index`.
* Default install includes ChromaDB + sentence-transformers (no
  `[search]` extra needed for semantic search).
* `codevira clean` for full uninstall; `codevira --version` / `-V`
  for standard CLI version reporting.

## Tests

**2395 passing, 1 skipped, 0 failed** — deterministic across multiple
full runs.

## Upgrade

```bash
pipx install --upgrade codevira
codevira --version       # codevira 2.0.0
codevira doctor          # 14 checks
```

## Known limitations (tracked in v2.1 roadmap)

* `get_signature` / `get_code` are Python-only. For TypeScript / Go /
  Rust use `Read` directly. Multi-language tree-sitter wiring is
  v2.1 work.
* `record_decision` is one round-trip per decision (~800 B overhead).
  A `record_decisions_batch` API is v2.1 work.
* CLI naming overlap (`init` vs `setup` vs `register` vs `configure`)
  + `--project-dir` global flag vs `--project PATH` per-subcommand
  flag duplication: design-call items, v2.1 cycle.

## Internal-iteration history

The five internal rc1..rc5 dev tags consolidated here for visibility
(none of these were ever published to PyPI):

* **internal rc1** (2026-05-05): build-phase close-out — 10 heroes
  shipped; cross-tool universality wedge locked.
* **internal rc2** (2026-05-06): 4 dogfood bugs — Claude Code +
  Claude Desktop install paths, AI tool discoverability of the
  `do_not_revert` flag.
* **internal rc3** (2026-05-08): 14 bugs — macOS fork-safety
  segfault, roadmap drift, 4 FK races, project-scope MCP file path,
  3 new doctor checks, hook resilience.
* **internal rc4** (2026-05-13): 4 audit bugs — `status --global` UI
  typos, duplicate `global.db.projects` rows, ghost auto-init
  directories, setup prompt silent-fail.
* **internal rc5** (2026-05-14): 38 audit-driven fixes across CLI
  surface + product credibility + indexing default — see commit
  history for the full ledger.

---

# v2.0-rc.3 — Second dogfood pass: 14 bugs across native, wedge, schema, hooks, and CLI (consolidated, 143 new tests)

**Released:** 2026-05-08
**Test status:** 2330 / 2330 passing (deterministic; +126 from rc.2)

This rc consolidates five internal iteration rounds (originally tagged rc.3 → rc.7 in dev) into a single user-visible release, since none of the intermediate versions were ever installed by an end user. Each bug below was caught either by real dogfood (Sachin's UDAP and AgentStore projects) or by re-audit of the same bug shape elsewhere in the codebase.

## Bug ledger — 14 closed in rc.3

### Native + first-install (P0 dealbreakers)

#### 🚨 Bug 7 — macOS native segfault on first `codevira index`

**Symptom**
```
Loading weights: 100%|█████████| 103/103 [00:00<00:00, 3299.17it/s]
zsh: segmentation fault  codevira index
resource_tracker: leaked semaphore objects: {'/loky-...'}
```

**Root cause** — sentence-transformers loads its tokenizer via HuggingFace's Rust crate; chromadb's default embedding function imports torch; loky/joblib runs a multiprocessing pool. On macOS Apple Silicon, fork() inside any of these crashes inside `+[__NSCFConstantString initialize]` because libdispatch wasn't initialised fork-safely.

**Fix** (`indexer/_fork_safety.py`, auto-applied at indexer-package import time, BEFORE any chromadb/torch import):
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` — bypasses the libdispatch crash
- `TOKENIZERS_PARALLELISM=false` — avoids the Rust threadpool fork race
- `OMP_NUM_THREADS=1` — sidesteps libomp/libiomp duplicate runtimes
- `multiprocessing.set_start_method("spawn", force=True)` — defensive

Linux/Windows: no-op. **Tests:** `tests/test_fork_safety.py` (14).

### Wedge + memory primitives (real product gaps)

#### 🎯 Bug 8 — Roadmap drift: codevira goes stale silently (the wedge-killer)

**Symptom** — Open AgentStore after 4 days. Claude Desktop reads files cold and gives an 85%-accurate state. Codevira responds with a phase from May 2 — frozen at the seed state. 7 weeks of work since, never logged.

**Root cause** — the AI in those sessions never called `update_phase_status` / `complete_phase` / `record_decision`. Reads stayed fresh, writes went unused, the wedge promise ("the project remembers what you did") quietly broke.

**Fix** — three layers:
1. **`mcp_server/roadmap_drift.py`** — at SessionStart, compare codevira's claimed phase timestamp vs `git log --since`. Drift fires if `days_since > 3` OR `commits_since > 5`. Best-effort, never blocks.
2. **AI-Promotion-Score policy update** — the SessionStart "## Codevira insights" block now leads with "⚠ Roadmap drift detected" + recent commit subjects.
3. **Stronger nudge templates** — explicit "before you respond to the user with the final result of meaningful work, call ONE of these write tools — non-negotiable, not optional" in `canonical_block.md` plus a drift-response playbook.

**Tests:** `tests/test_roadmap_drift.py` (26).

#### 🛠 Bug 3 — No MCP tool to retire stale learned rules

**Symptom** — UDAP had 3 rules with confidence 1.00 pinned to `src/control/cli/`. The user was about to delete that directory; rules would fire false positives forever. AI noted "no MCP tool retires rules."

**Fix**
- Schema: `learned_rules.retired_at` + `retired_reason` columns (idempotent ALTER, auto-applied on existing graphs)
- DB: `retire_learned_rule(rule_id, reason)` + `unretire_learned_rule(rule_id)`. `get_learned_rules()` filters retired by default; pass `include_retired=True` for audit.
- New MCP tool: `retire_rule(rule_id, reason)`
- `get_learned_rules()` now emits `id` per rule so the AI can find what to retire.

**Tests:** `tests/test_retire_rule.py` (13) — including SessionStart regression (retired rules must NOT surface in `top_signals.rules`).

#### 🛡 Bug 2 — Decision-level `do_not_revert` (Hero 1's missing write path)

**Symptom** — User: "log this as a decision via codevira and mark `do_not_revert=true`." AI: "no MCP write tool accepts that flag on a decision — only on a file via `update_node`." Correct. Hero 1's positioning ("AI cannot undo your protected decisions") had no canonical write path.

**Fix**
- Schema: `decisions.do_not_revert` column (idempotent ALTER)
- DB: `record_decision(decision, file_path, context, do_not_revert)` returns `{decision_id, session_id}`. `set_decision_protection(decision_id, do_not_revert)` flips an existing decision. `search_decisions()` SELECT now includes `id` + `do_not_revert`.
- New MCP tools:
  - `record_decision` — lightweight per-decision capture with optional `do_not_revert=true`
  - `mark_decision_protected` — flips an existing decision by id
- `update_node` description tightened to point AIs at `record_decision` for decision-level protection (vs file-level).

**Tests:** `tests/test_record_decision.py` (18).

### Schema robustness (FK race fixes)

These four bugs share the same shape: an `INSERT OR REPLACE` whose FK constraint can fail when a parent row is deleted by a concurrent transaction. Pre-rc.3 the watcher would crash with `IntegrityError: FOREIGN KEY constraint failed` (Sachin's crash log recorded **67** such crashes in v1.8.1; v2.0 inherited the bug). All four fixed with the same `WHERE EXISTS` subquery pattern: silently drop rows referencing missing parents instead of crashing.

#### 🚨 Bug 9 — `add_call_edge` FK race (67 crashes recorded)
`call_edges.{caller_id, callee_id}` → `symbols(id)`. Concurrent reindex deletes a symbol mid-flight → crash. **Fix:** `INSERT ... SELECT ... WHERE EXISTS`. **Tests:** `tests/test_call_edge_fk_safety.py` (6).

#### 🚨 Bug 13 — `add_edge` FK race
Same shape; `edges.{source_id, target_id}` → `nodes(id)`. **Tests:** `tests/test_fk_safety_extended.py::TestAddEdgeFKSafety` (3).

#### 🚨 Bug 14 — `add_symbol` FK race
`symbols.file_node_id` → `nodes(id)`. **Tests:** `TestAddSymbolFKSafety` (2).

#### 🛡 Bug 15 — `record_outcome` FK race
`outcomes.session_id` → `sessions`. Same fix; outcome silently dropped beats engine crash. **Tests:** `TestRecordOutcomeFKSafety` (2).

### Install / config correctness

#### 🚨 Bug 16 — Project-scope Claude MCP wrote to wrong file (same shape as Bug 6 at user scope)
`codevira init`'s per-project flow wrote MCP config to `<project>/.claude/settings.json`. That file is for project-scope hooks/permissions/env, NOT mcpServers. Result: the project-committed `.mcp.json` was never created.

**Fix** (`mcp_server/ide_inject.py::_claude_config_path`): now returns `<project>/.mcp.json` (canonical project-scope MCP file). Added regression test that fails if `_inject_claude` ever writes to settings.json again.

**Tests:** `tests/test_fk_safety_extended.py::TestClaudeConfigPathProjectScope` (3) + updated `test_ide_inject.py::TestInjectClaude` (2).

### Doctor coverage gaps (catch the regression class earlier)

#### 🛡 Bug 10 — `codevira doctor` doesn't verify Claude Code MCP visibility
rc.1 shipped a showstopper where codevira was silently invisible to Claude Code. Doctor reported all-green. Without an end-to-end check, that whole regression class slips through. **Fix** — `check_claude_mcp_visibility` shells out to `claude mcp list`. PASS only when codevira shows ✓ Connected. FAIL with the exact `codevira setup -y` / `claude mcp add` fix command otherwise. **Tests:** 4.

#### 🛡 Bug 11 — `codevira doctor` doesn't detect stale codeindex from older codevira version
AgentStore had a v1.8.1-era `codeindex/` that contributed to the segfault. Doctor never warned. **Fix** — `check_codeindex_freshness` WARNs if the freshest mtime is >14 days old, prints `rm -rf <path> && codevira index`. **Tests:** 3.

#### 🛡 Bug 12 — `codevira doctor` silently ignores degraded semantic search
Both UDAP and agent-mcp showed `ChromaDB Chunks: 0`. Users only noticed when search felt weak. **Fix** — `check_semantic_search_health` WARNs on missing or <100 KB codeindex; prints `codevira index`. **Tests:** 3.

### Hook resilience

#### 🚨 Bug 18 — stale codevira binary blocks the user's prompt

**Symptom**
```
UserPromptSubmit operation blocked by hook:
[bash /Users/sachin/.claude/hooks/codevira-user_prompt_submit.sh]:
usage: codevira [-h] [--project-dir PATH] {init,index,...,clean} ...
codevira: error: argument command: invalid choice: 'engine'
(choose from init, index, status, report, serve, register, configure, clean)
```

When an OLDER codevira (pre-v2.0) was on PATH while the installed hook scripts were v2.0 templates, argparse exited nonzero on every hook invocation and Claude Code surfaced "operation blocked by hook" — the user couldn't even submit a prompt.

**Root cause** — all 5 hook scripts used `exec "${CODEVIRA}" engine handle <Event>` which propagates the binary's exit code unchanged. Stale binaries exit 2 from argparse; Claude Code reads exit 2 as "block."

**Fix** — capture-stdout pattern across all 5 hook scripts:
- Run codevira in a subshell, capture stdout
- If stdout is empty or doesn't start with `{` → emit `{"continue": true}` and exit 0 (no-op)
- If stdout is valid-looking JSON → forward verbatim AND propagate the engine's exit code (so legitimate exit-2 blocks still work — critical for PreToolUse / Hero 1)

**Tests:** `tests/test_hook_resilience.py` (26) — every hook × 4 failure modes (binary missing, stale-binary argparse error, garbage stdout, valid-JSON happy path) + a dedicated test that PreToolUse still exits 2 when the engine legitimately blocks (so Hero 1 doesn't get silently disabled) + 5 kill-switch tests.

### CLI UX

#### Bug 17 — `codevira --version` / `-V` flag missing
Every Python CLI exposes `--version`. **Fix** — `argparse` `action="version"` reading from `mcp_server.__version__`. Single source of truth. **Tests:** `tests/test_cli_version.py` (3).

## Tests

**2330 passing, 1 skipped, 0 failed** — deterministic across multiple full runs.

Net new coverage since rc.2:
- `tests/test_fork_safety.py` (14)
- `tests/test_roadmap_drift.py` (26)
- `tests/test_retire_rule.py` (13)
- `tests/test_record_decision.py` (18)
- `tests/test_call_edge_fk_safety.py` (6)
- `tests/test_fk_safety_extended.py` (10)
- `tests/test_cli_version.py` (3)
- `tests/test_hook_resilience.py` (26)
- `tests/test_doctor.py` extended (10)
- 2 dispatch tests updated for new `include_retired` arg

## Upgrade

```bash
pipx install --force --pip-args "--index-url http://localhost:8080/simple/" codevira
codevira --version       # codevira 2.0.0rc3
codevira doctor          # 13 checks; expect 0 fail
```

## Verification on UDAP / AgentStore

```bash
# AgentStore (was the segfault site)
cd ~/Documents/Projects/Agentic/AgentStore
codevira index           # should NOT segfault on first load (Bug 7)
codevira doctor          # claude_mcp_visibility, codeindex_freshness, semantic_search_health all PASS or actionable WARN

# UDAP (was the rule-staleness + decision-protection site)
cd ~/Documents/Projects/LogisticsOS/UDAP
codevira doctor

# In a fresh Claude Code conversation:
#   "Use record_decision to log 'use Postgres for cortex metadata' with do_not_revert=true"   (Bug 2)
#   "Use search_decisions for 'database'"                                                      (verify do_not_revert: true surfaces)
#   "Use get_learned_rules"                                                                    (verify each rule has an id)
#   "Use retire_rule rule_id=X reason='src/control/cli/ deleted'"                              (Bug 3)
#   Quit + new conversation → "Use get_session_context"                                        (verify drift_warning + retired rules dropped — Bug 8 + Bug 3)
```

# v2.0-rc.2 — Dogfood bug fixes (4 bugs, 17 new tests)
   official tool that owns the file format.
2. **Fallback** — direct cooperative merge of `~/.claude.json` (preserves
   all 60+ other top-level keys: `oauthAccount`, `projects`, `userID`,
   etc.). Atomic via tempfile + os.replace per existing
   `_write_json_safe`.

**Tests:** 5 new tests in `TestClaudeCodeCliShellOut` cover both branches
+ failure modes + key preservation. Plus `TestClaudeGlobalConfigPathIsCorrect`
explicitly pins the path to `~/.claude.json` so a future "fix" back to
`settings.json` (which seems intuitive — "settings file holds settings")
breaks loudly.

### 🚨 Bug 6b: Claude Desktop detected but never planned

`setup_wizard._mcp_config_path_for()` had no `claude_desktop` branch.
Even though Claude Desktop was detected by the wizard's preview
("Detected: Claude Code, Claude Desktop, Windsurf, Antigravity"), it
was silently dropped from the plan. The Claude Desktop injector existed
(`_inject_claude_desktop`, line 334) but was never wired through global
mode.

**Fix:** added `inject_global_claude_desktop()` in `ide_inject.py` and
wired both the path-resolver (`_mcp_config_path_for`) and the dispatcher
(`_execute_mcp_config`) in `setup_wizard.py` to handle `claude_desktop`.
Writes `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) / `%APPDATA%/Claude/...` (Windows) with stdio config (no `cwd`,
no `url` — Claude Desktop constraints).

**Tests:** 3 in `TestClaudeDesktopGlobalInject` (file shape, fallback,
preservation) + 2 in `test_setup_wizard.py` (planning + dispatcher
contract).

### 🟡 Bug 1: `update_node` description didn't mention `do_not_revert`

The `do_not_revert` field was buried in the inputSchema's inner
`changes.description`. AIs scanning tool surfaces during dogfood missed
it entirely — the dogfood Claude Code session refused to log a
protection request claiming "None of the Codevira write tools accept a
do_not_revert flag."

**Fix:** outer `update_node` description now explicitly mentions
`do_not_revert` and tags it as the Hero 1 / Decision Lock mechanism.

**Tests:** 2 contract tests in `TestUpdateNodeDescriptionContract`.

### 🟡 Bug 5: `complete_phase.key_decisions` invisible to `get_session_context`

`complete_phase(phase_number, key_decisions=[...])` writes to the
roadmap store, NOT the `decisions` table. So `get_session_context()`
(which reads decisions from the table only) showed zero recent decisions
even after a phase was completed with 4 key_decisions recorded. Fresh
sessions had no way to learn what was just decided.

**Fix:** `get_session_context()` now also queries the roadmap's
recently-completed phases and surfaces their `key_decisions` in a new
field `recent_phase_decisions`, tagged with `source: "phase_completion"`.
The existing `recent_decisions` (from sessions table) gets
`source: "session"` so the AI can distinguish them. Capped at 5 entries
to stay within the ~500-token budget.

**Tests:** 4 in `TestGetSessionContext` covering happy path, cap,
empty completed_phases, and source tagging on the existing decisions.

## Tracked for rc.3 / rc.4 / post-GA

- **Bug 3** (rc.3): No MCP tool to retire stale `learned_rules`. Sachin's
  UDAP project has 3 high-confidence rules pinning tests to deleted
  `src/control/cli/` paths; they'll fire false positives after the
  Week-2 commit.
- **Bug 2** (rc.4 / GA): No decision-level `do_not_revert` flag — only
  per-file via `update_node`. Master plan's Hero 1 positioning implies
  decision-level. Needs schema migration on `decisions` table.
- **Bug 4** (post-GA backlog): `search_codebase` doesn't read
  `pyproject.toml::project.scripts` for ranking. Wrappers rank above
  real entry points.

## How to upgrade

```bash
# Republish to local PyPI (Sachin's setup)
cd ~/Documents/Projects/LogisticsOS/agent-mcp
.venv/bin/python -m build
twine upload --repository-url <local-pypi-url> dist/codevira-2.0.0rc2*

# On the consuming machine
pipx upgrade codevira

# Wipe rc.1's manual workaround + the wrong settings.json entry
claude mcp remove codevira -s user 2>/dev/null
# (also remove the dead "mcpServers.codevira" block from
# ~/.claude/settings.json — the hooks block is correct, leave it)

# Re-run setup — should now do the right thing automatically
codevira setup -y

# Verify (was broken on rc.1)
claude mcp list | grep codevira
# expect: codevira: <path> - ✓ Connected
```

## Test status

2204 / 2204 passing (rc.1's 2187 + 17 new). Deterministic across 3+
consecutive full-suite runs. One flaky test
(`test_starts_daemon_thread`) is pre-existing and unrelated — passes
in isolation.

---

# v2.0-rc.1 — Release candidate: 10/10 heroes + universality wedge locked

**Released:** 2026-05-05
**Test status:** 735 / 735 passing across all suites

The build phase of v2.0 is complete. All 10 heroes from the master plan
shipped. The cross-tool wedge promise has automated guards. Pillar 1
(`doctor`) and Pillar 2 (`agents`, `hooks install`) commands are wired.

## All 10 heroes shipped

| # | Hero | Type | Week shipped |
|---|---|---|---|
| 4 | Blast-Radius Veto | PreToolUse blocker | 4 |
| 1 | Decision Lock | PreToolUse blocker | 5 |
| 5 | Cross-Session Consistency | UserPromptSubmit injector | 6 |
| 6 | Token Budget Live View | Stop / PostToolUse | 7 |
| 2 | Anti-Regression Memory | PreToolUse blocker | 8 |
| 7 | Live Style Enforcement | PostToolUse warner | 9 |
| 10 | AI Promotion Score | SessionStart injector + `codevira insights` CLI | 10 |
| 9 | Proactive Intent Inference | UserPromptSubmit injector | 11 |
| 3 | Scope Contract Lock | UserPromptSubmit + PreToolUse (off-by-default) | 12 |
| 8 | Decision Replay | MCP resource + `codevira replay` CLI | 13 |

## New CLI commands in v2.0

- `codevira setup` — detect every AI tool, configure all (replaces `register`; `register` deprecated but still works)
- `codevira doctor` — health check with ✓/⚠/✗ + exact fix commands (Pillar 1.3)
- `codevira agents [--ide IDE] [--dry-run]` — regenerate per-IDE nudge files (Pillar 2.2)
- `codevira hooks install` — install Claude Code lifecycle hooks (Pillar 2.3)
- `codevira budget` — token-spend per session (Hero 6)
- `codevira insights [--since 7d]` — stable / reverted decisions + emerging patterns (Hero 10)
- `codevira replay [--query Q] [--format html|md|terminal]` — browse decision timeline (Hero 8)

## New MCP resources

- `codevira://decisions` — full decision timeline as HTML
- `codevira://decisions/<query>` — URL-decoded substring filter

## Test coverage

| Suite | Tests | What it covers |
|---|---|---|
| `tests/engine/` | ~580 | Per-hero unit + integration QA rounds (Weeks 9-13) |
| `tests/e2e/test_v2_release_candidate.py` | 28 | RC gate (Week 14) — 8 sections incl. coexistence, stress, failure-mode |
| `tests/e2e/test_cross_tool_universality.py` | 4 | **The North Star promise** — same memory in every tool |
| `tests/test_doctor.py` | 21 | Pillar 1.3 health-check coverage |
| `tests/test_cli_agents.py` | 16 | Pillar 2.2 + 2.3 + **wedge consistency** (template drift catches) |
| `tests/test_cli_replay.py` | 9 | Hero 8 CLI subprocess |
| `tests/test_cli_insights.py` | 2 | Hero 10 CLI subprocess |

## 8 bugs caught + locked in via regression tests

Honest bug ledger across the build phase:

| Bug | Caught at | Survived | Shape |
|---|---|---|---|
| 1 | Week-5 R8 redo | 5 weeks | `signals.decisions` SQL column drift |
| 2 | Week-5 R8 redo | 5 weeks | runner missed signals kwarg |
| 3 | Week-7 mutation M9 | 7 weeks | `enabled_by_default` flag was dead |
| 4 | Week-9 integration QA | 0 weeks | Hero 7 silent on Write tool |
| 5 | Week-11 user-prompted QA | 0 weeks | Hero 9 path-traversal escape |
| 6 | Week-11 user-prompted QA | 0 weeks | Hero 9 empty Blast radius section |
| 7 | Week-11 deep re-audit | 2 weeks | Hero 5 SQL parameterization |
| 8 | Week-11 deep re-audit | 1 week | CLI `--project` Bug-8 parity |

After Week 11, the deep-audit-from-start discipline kicked in and zero
new bugs surfaced through Weeks 12-14.

## Performance budget (verified in stress test)

- Dispatch p95 with all 9 PreToolUse-eligible policies + 100 decisions
  / 500 outcomes / 50 fixes: < 200ms
- `build_timeline` with 100 decisions: < 200ms
- `codevira doctor` total runtime: < 2s

## Known gaps (deferred to v2.0.x)

These were on the master plan but explicitly deprioritized to focus on heroes:

- `codevira test-ide <name>` smoke test
- Pillar 3 backlog: crash_logger rotation, watcher circuit breaker, shared `_sqlite_util`, 14 silent-exception sites, config.yaml hot-reload
- `[ui]` extra with `questionary` (interactive prompts work via plain `input()`)
- Pillar 1.4 error-message audit ("→ to fix:" suffix everywhere)

See `docs/v2-completion-plan.md` for the full pending list.

## Upgrading from alpha.x

```bash
pipx upgrade codevira
codevira setup    # IF you used `codevira register` before, switch to setup
                  # — register only injects MCP config; setup ALSO writes
                  # nudge files + lifecycle hooks (the wedge promise)
codevira doctor   # verify everything's healthy
```

## What's next before v2.0 GA

- Founder dogfood (1 week real codebase use)
- Recruit 3 alpha testers
- README v2.0 rewrite (heroes section + sharpened wedge headline) — in progress
- Differentiation page vs Mem0/claude-mem/MemPalace — in progress
- Pillar 3 backlog cleanup (defer to v2.0.x if not blocking)
- Tag v2.0.0 GA + HN submission

---

# v2.0-alpha.2 — Three more heroes + Bug 3 caught

**Released:** 2026-05-04

Builds on alpha.1.1's bug fixes with three new policy heroes shipping. Plus one more silent fail-open bug (Bug 3) caught and fixed during Week-7 mutation testing.

## What's new in alpha.2

### 🔒 Hero 1 — Active Decision Lock (now actually wired)

When the AI tries to Edit a file marked `do_not_revert` in the graph, codevira refuses the edit and surfaces the locked decisions:

```text
🔒 Decision-lock veto on auth.py: this file is marked do_not_revert
with 1 locked decision(s).

Locked decisions:
  • #142: 'bcrypt over argon2 — see issue #142' (locked 2025-04-13)

To proceed safely:
  1. Surface the decision to the user. They locked it for a reason.
  2. Confirm + unlock via codevira's CLI, OR set
     CODEVIRA_DECISION_LOCK_MODE=warn (warns instead of blocks)
     or =off (disables this policy).
```

Configuration: `CODEVIRA_DECISION_LOCK_MODE` = `off` / `warn` / `block` (default `block`).

### 🧠 Hero 5 — Cross-Session Consistency

When you submit a prompt mentioning a topic, codevira proactively surfaces past decisions on related topics — before the AI responds:

```text
[your prompt]: "Add a styled Get Started button to the homepage hero"

[codevira injects, before the AI's first turn]:
   ## Prior decisions you may want to consider
   - 2025-04-13 — [styles/] Tailwind, not Bootstrap — bundle size
   - 2025-04-08 — [components/] Use class:hover, not @apply hover:...
   If your current request conflicts with any of these, surface
   the conflict to the user before proceeding.
```

Configuration: `CODEVIRA_CROSS_SESSION_MODE` = `off` / `inject` (default `inject`); `CODEVIRA_CROSS_SESSION_MAX_INJECT` = 1-20 (default 5).

### 📊 Hero 6 — Token Budget Live View

Codevira persists every session's token spend at session end (Stop hook). Read it back via the new `codevira budget` CLI:

```text
$ codevira budget
  Session abc-123  (2026-05-04 14:30)
  ────────────────────────────────────────────────────────────
  Injected:        8,247 tokens
  Used:            4,102 tokens
  Efficiency:        49.7%

  Top wasted sources:
    get_node                       2,400 injected, 1,920 wasted (80%)
    search_decisions               1,200 injected,   600 wasted (50%)

$ codevira budget history --last 5
  Last 5 sessions:
  Date                 Session                   Injected      Used   Eff
  2026-05-04 14:30     abc-123                      8,247     4,102  50%
  2026-05-04 11:15     def-456                      3,200     2,800  88%
  ...
```

Configuration: `CODEVIRA_TOKEN_BUDGET_MODE` = `off` / `persist` (default `persist`).

### 🚨 Bug 3 caught: dead `enabled_by_default` field

`Policy.enabled_by_default = False` was supposed to opt a hero out of default registration. It was declared on the base class and documented since Week 1, but `register_default_policies` never checked it. **Any hero that needed to ship as opt-in would silently auto-register.**

Caught by Week-7 mutation testing (M9): flipping the flag had ZERO behavioral effect. Fixed in this release. Same shape as Bugs 1 + 2 in alpha.1.1 — "declared but never integrated → silent fail-open."

Pattern now codified as **Lesson #18:** any contract field that doesn't have a code path enforcing it is a future silent-fail-open bug.

## What's NOT in alpha.2

These are still deferred and shipping in subsequent alphas (Weeks 8-13):
- Hero 2 (Anti-Regression Memory) — Week 8
- Hero 7 (Live Style Enforcement) — Week 9
- Hero 10 (AI Promotion Score) — Week 10
- Hero 9 (Proactive Intent Inference) — Week 11
- Hero 3 (Scope Contract Lock) — Week 12
- Hero 8 (Decision Replay) — Week 13

## Quality

- **368/368 tests green** (was 278 at alpha.1; +90 net new regression tests across the three new heroes + retrospective audits)
- **3 production bugs caught + fixed** since alpha.1 (Bugs 1, 2, 3 — all silent-fail-open)
- **Three new playbook lessons** (#15 real-DB integration, #16 every-hero dispatch test, #17 honest self-assessment) plus #18 emerging
- **All four heroes** (1, 4, 5, 6) now have:
  - End-to-end dispatch+real-graph regression tests
  - Behavioral spies on signal/persistence calls
  - 9-10 mutations each, all caught
  - Real CLI subprocess tests where applicable

## Upgrading from alpha.1.x

```bash
pipx upgrade codevira
codevira setup --yes  # idempotent re-run picks up the fixes
```

No data migration. The fixes are pure code changes; existing `decisions.db`, `fixes.db`, `token_budget.jsonl` work unchanged.

---

# v2.0-alpha.1.1 — Critical bug fixes for alpha.1

**Released:** 2026-05-04 (one day after alpha.1)

If you installed `v2.0-alpha.1` between 2026-05-03 and 2026-05-04, **upgrade immediately**: alpha.1 shipped with two silent fail-open bugs that made all three policy heroes (Decision Lock, Blast-Radius Veto, Cross-Session Consistency) silently no-op against real projects. They never blocked, warned, or injected — even when they should have.

## The two bugs

### Bug 1: `signals.decisions()` SQL column-name mismatch

The signals layer's SQL referenced `d.timestamp` but the actual column is `d.created_at`. The exception was silently swallowed by the layer's broad `except Exception`, so `signals.decisions(locked_only=True)` returned `[]` on every call. **Hero 1 (Decision Lock) was fail-open against any real graph since its Week-5 ship.**

### Bug 2: Engine runner never passed `signals` to policies

The runner built `signals` correctly but called `policy.evaluate(event)` without passing it as a kwarg. Heroes 1, 4, and 5 use `evaluate(event, signals=None)` — with `signals=None`, every hero's stage-2 check fired immediately and returned `allow`. **All three heroes silently no-op'd through `dispatch()`** — the only path Claude Code hooks + MCP `pre_call` use.

## Why per-week QA missed them for 5 weeks

- Every per-week test used `_FakeSignals` instead of a real `SQLiteGraph` → Bug 1 never fired
- Every per-week test passed `signals` manually → Bug 2 never fired
- Both bugs only manifest in production paths that weren't being exercised

The user's question — "have you done QC seriously?" — is what surfaced this. The retrospective added 17 regression tests so this class of bug can't survive again.

## Other fixes in alpha.1.1

- Hero 4 + Hero 5 retrospective audits closed 12 additional test gaps (mutation testing exposed weak fakes that ignored filter args; behavioral spies added).
- Cross-hero SQL audit verified no other column-name bugs lurking.
- Three new playbook lessons (#15-#17) codify the missing discipline.

## Test status

350/350 tests green (was 278 in alpha.1).

## Upgrading from alpha.1

```bash
pipx upgrade codevira
codevira setup --yes  # idempotent re-run picks up the fix
```

No data migration needed; the fixes are pure code changes.

---

# v2.0-alpha.1 — Persistent project memory + first policy hero

**Status (superseded by alpha.1.1 — see above):** Alpha. Built for early-adopter feedback, not production. Expect rough edges.

## What's in alpha.1

This is the first public preview of v2.0 — Codevira's biggest architectural change since v1.0. Four weeks of work, 22 commits, integration-tested across the full stack.

### 🛠 Pillar 1: One-prompt setup

```bash
pipx install codevira
codevira setup
```

That's it. Codevira detects every AI coding tool you have installed and configures all of them in a single prompt — MCP server entries, Claude Code lifecycle hooks, and per-IDE nudge files. **No more multi-step `init → register → configure` dance.**

Tools configured automatically (when detected):
- **Claude Code** (MCP config + lifecycle hooks + `CLAUDE.md`)
- **Cursor** (MCP config + `.cursor/rules/codevira.mdc` with YAML frontmatter)
- **Windsurf** (MCP config + `.windsurfrules`)
- **Antigravity / Gemini CLI** (MCP config + `GEMINI.md`)
- **OpenAI Codex CLI** (`AGENTS.md` — Linux Foundation standard)
- **GitHub Copilot** (`.github/copilot-instructions.md`)
- **Tier-2 fallback**: any MCP-compatible tool that reads `AGENTS.md`

Idempotent: re-run any time. If nothing changed, it tells you so.

```text
codevira setup
  Codevira setup — myproject
  ────────────────────────────────────────────
  Detected: Claude Code, Cursor, Windsurf, Antigravity

  Plan (15 steps):
    • Add codevira to Claude Code MCP config (merge: ~/.claude/settings.json)
    • Install Claude Code SessionStart hook → codevira-session_start.sh
    ...
  Proceed? [Y/n] y
  ✓ Done in 0.3s. 15 changes; 0 already current.
  Restart Claude Code to pick up the new lifecycle hooks.
```

### 🔒 Hero 4: Blast-Radius Veto

The first **policy hero** — codevira's engine actively intervenes when AI tries to do something risky.

When the AI attempts to edit a file with N downstream callers AND the change modifies a public signature, Codevira surfaces the cost **before** the edit lands:

```text
🛑 Blast-radius veto on auth.py: 12 downstream file(s) depend on this code,
and your edit modifies a public signature.

Signature changes detected:
  modified: def auth_token(user_id):  →  def auth_token(user):

Affected files (top 3):
  • api/handlers.py
  • middleware/auth.py
  • tests/test_auth.py
  ... and 9 more

To proceed safely:
  1. Read the affected files (Grep / Read) and propose a
     MultiEdit covering all of them, OR
  2. Override with CODEVIRA_BLAST_RADIUS_MODE=warn (warns instead of blocks)
     or =off (disables this policy).
```

Languages with signature-detection: **Python, JS/TS, Go, Rust, Java, C#**.

Configuration via env vars:
- `CODEVIRA_BLAST_RADIUS_MODE` — `off` / `warn` / `block` (default `block`)
- `CODEVIRA_BLAST_RADIUS_THRESHOLD` — min callers to trigger (default `5`)

### 🧰 Engine subsystem (invisible but foundational)

A pluggable policy engine intercepts:
- Claude Code lifecycle hooks (PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop)
- MCP tool dispatch (every tool the AI calls)

Heroes 1-10 will all register `Policy` plugins against this engine. Hero 4 ships first; Heroes 1, 2, 3, 5, 6, 7, 8, 9, 10 follow in v2.0-alpha.2 through v2.0.

### 📦 Behind the scenes (Week 2 plumbing)

These don't have user-visible UI yet but they ship in alpha.1 so the corresponding heroes can use them later:

- **Git fix-detection** (`scan_git_log`) — scans commit history for `fix:` / `bug:` / `hotfix:` / `fixes #N` patterns. Future Hero 2 (Anti-Regression Memory) will use this to block re-introduction of fixed bugs.
- **Token-budget persistence** — every AI session's injection/usage gets logged to `~/.codevira/projects/<key>/logs/token_budget.jsonl`. Future Hero 6 (Token Budget Live View) reads this for `codevira budget history`.

## Performance

| Operation | Measurement |
|---|---|
| `codevira setup` end-to-end (4 IDEs) | ~0.3 s |
| Engine `dispatch` (in-process) p99 | 0.022 ms |
| Claude Code hook full round-trip p95 | 67 ms (10 ms in fast-path mode) |
| Hero 4 `evaluate()` p99 | 0.022 ms |

Hot paths are essentially free. The engine adds zero perceivable latency.

## Quality

- **278/278 tests** in `tests/engine/` + `tests/test_paths.py` + `tests/test_setup_wizard.py` passing.
- **Per-week QA discipline:** 5-8 progressive rounds × 4 weeks = ~28 independent rounds.
- **Integration QA:** 9 cross-cutting rounds (I1-I9) on top.
- **15+ bugs** caught + fixed across QA, including 1 HIGH security (symlink traversal), 2 HIGH UX (path mismatch + idempotency reporting), 1 HIGH atomicity (Ctrl-C corruption protection), and 11+ P1/P2 issues.
- **Mutation testing** verifies regression tests actually catch reverted fixes.

Full QA discipline + lessons codified in [`docs/qa-playbook.md`](./docs/qa-playbook.md).

## What's NOT in alpha.1

These are explicitly deferred and will land in subsequent alpha releases:

- **Heroes 1, 2, 3, 5, 6, 7, 8, 9, 10** — alpha.2-alpha.4 (Weeks 5-13)
- **`codevira setup` per-project mode** (`--project-only` flag) — v2.1
- **Multi-process safety** for `token_budget.jsonl` — single-writer-per-project for now
- **YAML config** for hero policies — env vars only in alpha (e.g. `CODEVIRA_BLAST_RADIUS_*`)
- **Tree-sitter signature parsing** — regex-based detection in alpha (sufficient for 6 mainstream languages)
- **`codevira setup --uninstall`** — manual cleanup for now

## Known limitations (alpha)

- **No founder dogfood gate yet.** Code is QA-clean but hasn't run on the maintainer's daily machine for 48 hours yet. Alpha testers should treat this as an early preview.
- **Performance numbers are dev-machine-only.** macOS APFS / M-series. Not benchmarked on Windows / Linux / NFS.
- **Pre-existing test pollution** in some unrelated suites (graph_generator, test_cli) — not Week-1-through-4 work; baseline since v1.8. Doesn't affect production behavior; tracked for v2.0 GA.
- **Live observation through real Claude Code** is verified at the schema level (subprocess + realistic JSON), not by an actual Claude Code session yet. That happens during dogfood.

## Upgrading from v1.8.x

Run `codevira setup`. It detects existing `~/.claude/settings.json` (or other IDE configs) and merges cleanly:
- Old codevira MCP entry → updated to new command
- Other tools' MCP entries → preserved verbatim
- Hooks → added (v1.8 didn't have them)

The deprecated `codevira register` still works but prints a deprecation notice. It will be removed in v2.0 GA.

## Tester checklist

If you're trying alpha.1, here's what would help most:

1. **Install + `setup`**: does it complete in <60 seconds on your machine?
2. **Open Claude Code** in a real project: does Codevira show in the MCP tools list?
3. **Trigger Hero 4**: edit a high-impact file and rename a function. Does Codevira block with a useful diagnostic?
4. **Multi-IDE**: open the same project in Cursor or Windsurf. Same memory available?
5. **Idempotency**: run `codevira setup` twice. Does the second run report "already up to date"?

Bug reports → GitHub issues with the `alpha.1` label. Include `codevira doctor` output (or the equivalent — `codevira setup --dry-run` shows the install state).

## Acknowledgments

This release was built through a 4-week sprint with a disciplined QA process: every week, every hero, every fix went through multiple progressive QA rounds with independent agents, mutation testing, and integration verification. The result is unusual for an alpha release — most of the bugs that would normally surface during dogfood already surfaced during QA.

The remaining gates (real founder dogfood + alpha testers) are about validating that the QA discipline missed less than expected. Honest expectation: 1-3 real-world bugs in the first 30 days. The codified playbook (`docs/qa-playbook.md`) means any of those become *new lessons*, not repeating ones.

— v2.0-alpha.1, 2026-05-04
