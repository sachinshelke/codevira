# v2.0-rc.2 — Dogfood bug fixes (4 bugs, 17 new tests)

**Released:** 2026-05-06
**Test status:** 2204 / 2204 passing (deterministic across 3+ full runs)

Honest dogfood after `pipx install codevira` of rc.1 onto a real project
(UDAP, Sachin's solo work) surfaced 7 bugs in the install + first-call
path. rc.2 closes the 4 most critical (the 2 showstoppers that broke
out-of-box install, plus 2 product gaps that confused the AI). The
remaining 3 are tracked for rc.3 / rc.4 / post-GA.

## Bugs fixed in rc.2

### 🚨 Bug 6 (SHOWSTOPPER): Claude Code MCP config written to the wrong file

`mcp_server/ide_inject.py:_claude_global_config_path()` was returning
`~/.claude/settings.json`. That's correct for **hooks / permissions / env**
but Claude Code reads `mcpServers` from `~/.claude.json` (the user-scope
JSON file at home root). Symptom: setup looked successful, hooks fired,
but `claude mcp list` was empty and codevira tools were invisible to the
AI. Required manual `claude mcp add --scope user codevira <path>` to
unblock.

**Fix:** two-tier strategy in `inject_global_claude_code()`:

1. **Preferred** — if `claude` CLI is on PATH, shell out to
   `claude mcp add --scope user codevira <cmd_path>`. Delegates to the
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
