# v2.0-alpha.1 — Persistent project memory + first policy hero

**Status:** Alpha. Built for early-adopter feedback, not production. Expect rough edges.

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
