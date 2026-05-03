# Pillar 1 — Frictionless setup (`codevira setup`)

> The first 60 seconds determine whether someone keeps codevira installed.
> Today: a user has to run `init`, then `register`, then read several `configure` flags, then learn which AI tool wants which JSON key, then restart their IDE. That's a four-step setup spread across three commands and a forum post.
> Pillar 1 collapses that into **one prompt**: `codevira setup`.

This spec is the contract for Week 3 implementation (per master-plan sequencing). It's deliberately small — Pillar 1 is plumbing, not a hero. The 10 heroes ride on top of this plumbing being correct.

Sprint week: **Week 3**. Goal: any new user goes from `pipx install codevira` to "Claude Code session sees codevira tools" in **under 2 minutes** with zero documentation reading.

---

## What it is

A single command — `codevira setup` — that:

1. Detects every AI tool on the machine.
2. Asks the user *once* what they want set up (single prompt, sensible defaults).
3. Writes/merges the right artefacts for each detected tool (MCP config, lifecycle hooks, AGENTS.md/CLAUDE.md/etc.).
4. Prints a short summary of what changed and what the user should do next ("restart Claude Code now").
5. Is **idempotent** — re-running it after an upgrade or a fresh project re-syncs without churn.

It replaces the existing user-visible install path:
- `codevira init` (per-project init) → still exists; `setup` calls it under the hood.
- `codevira register` (writes IDE configs) → deprecated with a redirect message; behaviour folded into `setup`.
- `codevira configure` (interactive config tweaks) → kept for advanced users; not part of first-run.

---

## Why now (Pillar, not Hero)

Pillar 1 is the price of entry for everything else in v2.0:

- The 10 heroes only fire when **lifecycle hooks** are wired into Claude Code. If `setup` doesn't install hooks, Heroes 1-5 + 9 silently no-op.
- Universal multi-tool coverage (the v2.0 wedge) requires writing nudge files for **every** AI tool the user has — not just Claude. Without `setup`, the user has to hand-edit Cursor / Windsurf / Antigravity / Codex / Copilot configs themselves.
- Founders/maintainers get DM'd setup-confusion questions weekly. Each one is a churn signal.

The engine work (Weeks 1-2) is invisible to users. Pillar 1 is the first piece they touch, and it has to feel premium.

---

## User pain it solves (concrete example)

**Today (v1.8):**

```text
$ pipx install codevira
$ cd ~/myproject
$ codevira init
✓ Initialized
$ codevira register
✓ Configured Claude Code (~/.claude/settings.json)
✓ Configured Cursor (~/.cursor/mcp.json)
[user opens Claude Code — nothing happens because hooks aren't installed]
[user reads README — finds out they need to set up hooks separately]
[user reads docs — finds out they should also write CLAUDE.md]
[user gives up]
```

**v2.0 with Pillar 1:**

```text
$ pipx install codevira
$ cd ~/myproject
$ codevira setup
🔍 Detected: Claude Code, Cursor, Windsurf
📋 Plan:
   • Add codevira to Claude Code, Cursor, Windsurf MCP configs (global, all projects)
   • Install Claude Code lifecycle hooks (proactive memory)
   • Write CLAUDE.md, AGENTS.md, .cursor/rules/codevira.mdc, .windsurfrules in this project
Proceed? [Y/n] y
✓ Configured 3 IDEs
✓ Installed 5 Claude Code hooks → ~/.claude/hooks/codevira-*.sh
✓ Wrote 4 nudge files to ~/myproject
🎉 Done in 4.2s. Restart Claude Code to pick up hooks. Cursor/Windsurf are live.
```

One prompt. Three lines of preview. One Y/n. Done.

---

## Mechanism

### Command surface

```text
codevira setup [--yes] [--dry-run] [--ide IDE]
                [--no-hooks] [--no-nudge-files] [--no-mcp]
                [--global / --project-only]
```

| Flag | Behaviour |
|---|---|
| (none) | Auto-detect IDEs, prompt once with `[Y/n]`, write everything. |
| `--yes` | Skip the prompt (CI / scripted installs). |
| `--dry-run` | Print the plan; touch nothing. |
| `--ide claude` | Only configure the named IDE (repeatable). |
| `--no-hooks` | Skip Claude Code lifecycle hook installation. |
| `--no-nudge-files` | Skip CLAUDE.md / AGENTS.md / .cursor/rules / .windsurfrules / GEMINI.md / copilot-instructions.md generation. |
| `--no-mcp` | Skip writing MCP server entries (just hooks + nudge files). |
| `--global` | Force global-mode MCP entries (default if cwd ≠ project). |
| `--project-only` | Force per-project MCP entries (overrides default). |

Defaults are aggressive — assume the user wants the full experience. Power users can subset.

### What `setup` actually does (in order)

```python
def cmd_setup(*, yes: bool, dry_run: bool, ide: list[str] | None,
              hooks: bool, nudge_files: bool, mcp: bool,
              global_mode: bool | None) -> int:
    # 1. Resolve project_root via existing get_project_root() — no behaviour change.
    project_root = _resolve_project_root_for_setup()

    # 2. Detect AI tools
    detected = detect_installed_ides_v2(project_root)   # extends existing detect_installed_ides
    detected = filter_by_user_choice(detected, ide)     # honour --ide flag

    # 3. Build the plan — pure data, no side effects
    plan = build_setup_plan(
        project_root=project_root,
        detected=detected,
        install_mcp=mcp,
        install_hooks=hooks,
        write_nudge_files=nudge_files,
        global_mode=global_mode,
    )

    # 4. Show the plan, ask for consent
    print_plan(plan)
    if dry_run:
        return 0
    if not yes and not _confirm("Proceed?"):
        return 1

    # 5. Execute, capturing per-step outcome
    results = execute_plan(plan)

    # 6. Print summary + next steps (per-IDE specific tips)
    print_summary(results)
    return 0 if results.all_succeeded() else 2
```

### Detection — what counts as "installed"

Extend the existing `mcp_server/ide_inject.py:detect_installed_ides` to also detect:

- **OpenAI Codex CLI** — `which codex` or `~/.codex/` directory
- **GitHub Copilot** — presence of `.github/copilot-instructions.md` already, OR `gh extension list` includes `gh-copilot`, OR `which copilot`
- **Continue.dev** — `~/.continue/` directory
- **Aider** — `which aider`
- **Roo Code / Cline** — VS Code extension manifest under `~/Library/Application Support/Code/User/globalStorage/`

Tier-1 (existing) detection is preserved verbatim — Pillar 1 must not regress v1.8 behaviour.

### Plan = pure data

A `SetupPlan` is a dataclass:

```python
@dataclass(frozen=True)
class SetupStep:
    kind: Literal["mcp_config", "hook", "nudge_file"]
    ide: str               # "claude", "cursor", "windsurf", ...
    target_path: Path      # what we'll write
    target_path_existed: bool
    will_merge: bool       # True if appending/merging into existing content
    preview: str           # what changes (≤500 chars)

@dataclass(frozen=True)
class SetupPlan:
    project_root: Path
    project_name: str
    steps: list[SetupStep]
```

Building the plan is pure (no I/O writes); executing it is the only step that mutates the filesystem. This is the `dry_run` foundation — `--dry-run` runs steps 1-4 only.

### Execution — fail-soft, never fail-loud

Each step is wrapped in a try/except. If step 7-of-12 fails (say, Cursor's mcp.json is malformed), we:

1. Log the failure (don't crash).
2. Skip that step.
3. Continue with steps 8-12.
4. In the summary, report `✓ 11 of 12 steps succeeded` plus a one-line diagnosis for the failure with a fix-it command (`codevira doctor` or `codevira config --ide cursor`).

The whole point of v1.8.1 was: when something goes wrong, **tell the user what to do next, don't just crash**. Pillar 1 inherits that bar.

### Idempotency contract

Running `codevira setup` twice in a row must:

- Not duplicate MCP server entries (existing `_merge_mcp_config` already handles this).
- Not append duplicate hook lines (use markers: each generated file/section has `<!-- codevira:start -->` / `<!-- codevira:end -->` or shell `# codevira:start` / `# codevira:end`).
- Detect "already up to date" and surface that in the summary (`6 of 12 steps were already correct, no changes needed`).
- Cleanly handle a *partial* prior run (e.g. user ran `setup --no-hooks` last week, runs `setup` today → only the new hook steps execute).

### Nudge-file generation (canonical-block pattern)

Single source of truth: `mcp_server/data/templates/canonical_block.md`. Every per-IDE nudge file is generated by:

1. Loading the canonical block (≤200 lines).
2. Wrapping it in IDE-specific framing:
   - CLAUDE.md / AGENTS.md / GEMINI.md / `.windsurfrules` → markdown wrapper
   - `.cursor/rules/codevira.mdc` → MDC frontmatter + content
   - `.github/copilot-instructions.md` → markdown
3. Inserting the block between markers in the target file:
   - Existing file with markers → replace block content only
   - Existing file without markers → append block at end
   - Missing file → create file with block

Implementation lives in `mcp_server/agents_md.py` (new). Pillar 2 (Week 3) builds on this for the multi-tool coverage matrix.

### Hook installation

For Claude Code (the only IDE with hooks today):

1. Copy the 5 shell scripts from `mcp_server/data/hooks/` to `~/.claude/hooks/codevira-<event>.sh`.
2. `chmod +x` each.
3. Update `~/.claude/settings.json` to register each hook under its event slot using existing JSON-merge helpers (`_merge_mcp_config` extended to also merge hook config).
4. Each hook script already has the R3 fast-path (`CODEVIRA_ENGINE=0` exits in <5ms).

If `~/.claude/settings.json` already has user-defined hooks for an event, codevira **prepends** its own hook to the existing list — no overwrite. Removing codevira hooks is `codevira setup --uninstall-hooks` (deferred to a follow-up, not Week 3).

---

## Configuration knobs

| Setting | Default | Where | Purpose |
|---|---|---|---|
| `setup.auto_detect_ides` | `true` | global config | Off → user must use `--ide` to enumerate |
| `setup.preferred_mcp_mode` | `global` | global config | `global` writes `~/.claude/settings.json`; `project` writes `<project>/.claude/settings.json` |
| `setup.write_nudge_files` | `true` | global config | Pre-decision for `--no-nudge-files` |
| `setup.write_agents_md` | `true` | global config | AGENTS.md fallback for tier-2 IDEs |
| `setup.canonical_block_path` | bundled | env override | Lets users customize the canonical content |
| `CODEVIRA_SETUP_NONINTERACTIVE` | unset | env | When set, equivalent to `--yes` (for CI) |

All knobs live in the existing `config.yaml` system. No new config file.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Run `setup` outside any git/python project | `is_invalid_project_root` guard fires → friendly error: "Run this from inside your project directory." Exit 1. |
| Run `setup` from `$HOME` | Same as above (v1.8.1 hardening preserved). |
| User has no AI tools installed | Print: "No supported AI tools detected. Install Claude Code, Cursor, Windsurf, or Antigravity, then re-run." Exit 0 (not an error). |
| User declines the prompt | Exit 0 cleanly with "No changes made." |
| Existing `~/.claude/settings.json` is malformed JSON | Skip Claude Code step, print exact error + fix command. Continue with other IDEs. |
| Existing CLAUDE.md has no codevira markers but has user content | Append block at end with markers. **Don't replace** any existing user content. |
| Existing CLAUDE.md has codevira markers from old version | Replace content between markers; preserve everything outside. |
| Project file path > 200 bytes (Week-2 deep-path bug) | `_sanitize_path_key` truncates with hash — no ENAMETOOLONG. |
| Disk full / permission denied mid-step | Step fails, summary shows it, other steps continue. |
| User has antiquated Claude Desktop config | Detect old schema, attempt migration, if it fails → skip + suggest manual `codevira config --ide claude_desktop`. |
| User's IDE is open while we run setup | Write succeeds (atomic replace via existing helpers); user gets "restart Claude Code" hint in summary. |
| Concurrent `codevira setup` invocations | Best-effort: first writer wins for any given file; idempotency contract means second run is a no-op. (No file lock — too much complexity for a one-time install.) |
| User in a polyrepo (project_root has 5 sub-repos) | `setup` runs once for the outer root — that's the project. Sub-repos inherit unless user runs `codevira setup` inside one. |
| `--ide unknown_tool` | Exit 1: "Unknown IDE 'unknown_tool'. Supported: claude, cursor, windsurf, antigravity, claude_desktop, codex, copilot." |
| `--dry-run` after a failed real run | Plan rebuilds from scratch; shows what would happen now (post-failure state). No carry-over. |
| User on Windows | Hook scripts skipped (Claude Code hooks are bash). MCP config + nudge files still work. Summary tells them: "Hooks not installed (Windows). Cursor/Windsurf still benefit from nudge files." |

---

## Performance budget

| Operation | Target p95 | Why |
|---|---|---|
| Detect IDEs | < 200 ms | One `which` and a few `Path.exists()` per IDE |
| Build plan | < 50 ms | Pure data; no I/O |
| Print plan | instant | Just a few lines |
| Execute plan (typical: 3 IDEs detected) | < 3 s | ~12 file writes, all atomic |
| Total wall-clock | **< 5 s** | The "60-second cold install" promise |

Verify with a `tests/test_setup_wizard.py::test_setup_under_5s` budget test.

---

## Demo storyboard (10-second scene for HN/README)

1. **(0.0s)** Terminal: `pipx install codevira` (already done — frame starts here).
2. **(0.5s)** `cd ~/myproject`
3. **(1.0s)** `codevira setup`
4. **(1.5s)** Output: `🔍 Detected: Claude Code, Cursor, Windsurf, Antigravity`
5. **(2.5s)** `📋 Plan:` — 4 lines summarising what's about to happen
6. **(3.0s)** `Proceed? [Y/n]` — user hits `y`
7. **(7.0s)** Stream of `✓ ` lines (one per step, ~12 of them)
8. **(8.5s)** `🎉 Done. Restart Claude Code to pick up hooks. Cursor / Windsurf / Antigravity are live.`
9. **(9.5s)** Open Claude Code → ask "what tools do I have?" → Claude lists codevira tools.
10. **(10.0s)** End frame.

That's the demo we record once and re-use across README, HN post, demo video.

---

## Acceptance test list

The 10 scenarios that have to pass before Pillar 1 ships:

1. **Cold install on a fresh user account** — `pipx install codevira` → `codevira setup --yes` → exits 0 in <5s, all steps green. (Automated via Docker test.)
2. **Idempotent re-run** — Run `setup --yes` twice; second run reports "already up to date" for all steps; no diffs in any target file.
3. **Partial detect** — Only Claude Code installed → setup configures Claude only, summary clearly notes "Cursor/Windsurf not detected (skipped)".
4. **`--dry-run` produces no writes** — Compare directory hash before/after `setup --dry-run`; identical.
5. **`--no-hooks` skips hook installation** — `~/.claude/hooks/codevira-*.sh` not present after run.
6. **Malformed existing config doesn't crash setup** — Plant `~/.claude/settings.json` with `{ broken json` → setup skips Claude step, completes others, exit code 2 (partial).
7. **Existing CLAUDE.md user content preserved** — Plant CLAUDE.md with custom user prose → `setup --yes` → custom prose still present, codevira block added with markers.
8. **`--ide cursor` only touches Cursor** — Other IDE config files unchanged.
9. **Bad project_root rejected** — `cd /tmp && codevira setup` → exits 1 with the v1.8.1 error message format.
10. **Wall-clock < 5 s** with all 4 tier-1 IDEs detected (smoke test on CI runner).

Tests live in `tests/test_setup_wizard.py` (new). Use `tmp_path` + monkey-patched `Path.home()` to isolate from the real filesystem.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/setup_wizard.py` | `cmd_setup` orchestrator + `SetupPlan` / `SetupStep` dataclasses + `build_setup_plan` / `execute_plan` |
| `mcp_server/agents_md.py` | Canonical-block renderer + per-IDE nudge file writers (used by setup, also Pillar 2) |
| `mcp_server/data/templates/canonical_block.md` | Single source-of-truth nudge content |
| `mcp_server/data/templates/claude_md.tmpl` | CLAUDE.md wrapper template |
| `mcp_server/data/templates/agents_md.tmpl` | AGENTS.md template (Linux Foundation format) |
| `mcp_server/data/templates/cursor_rules.mdc.tmpl` | `.cursor/rules/codevira.mdc` template |
| `mcp_server/data/templates/windsurfrules.tmpl` | `.windsurfrules` template |
| `mcp_server/data/templates/gemini_md.tmpl` | GEMINI.md template (Antigravity / Gemini CLI) |
| `mcp_server/data/templates/copilot_instructions.tmpl` | `.github/copilot-instructions.md` template |
| `tests/test_setup_wizard.py` | Acceptance tests (10 scenarios above) |

### Modified

| Path | Change |
|---|---|
| `mcp_server/cli.py` | Add `setup` subcommand wiring; deprecate `register` with redirect message |
| `mcp_server/ide_inject.py` | Extend `detect_installed_ides` to cover Codex, Copilot, Continue.dev, Aider |
| `mcp_server/data/hooks/*.sh` | (Already done in Week 1 — fast-path + dispatch wired). No change. |
| `README.md` | Update install snippet to `pipx install codevira && codevira setup` (deferred to Pillar 4) |

### Deprecated (kept working with warning)

| Path / surface | Status |
|---|---|
| `cmd_register` | Prints `[deprecated] Use 'codevira setup' instead. Running register-equivalent now...` and continues. Removed in v2.1. |

---

## QA gate (before merging Pillar 1)

Per the cadence matrix in `docs/qa-playbook.md`, Pillar 1 needs:

- **Tier 1, full sweep** (all 8 angles): code review, doc drift, type safety, contract tests, error-message audit, dependency check, perf bench, behaviour preservation.
- **Tier 2, scripted** (acceptance test list above + the Docker cold-install smoke test).
- **Tier 3, manual founder dogfood ≥ 24 hours** — install on the founder's actual daily-use machine; use it for real Claude Code work; log any friction in `docs/v2-execution-log.md`.

The Tier 1 + Tier 2 sweep gates the merge. Tier 3 dogfood gates the alpha release.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| User's existing `~/.claude/settings.json` has weird custom MCP entries that don't match our merge schema | Medium | Medium | Use existing battle-tested `_merge_mcp_config`; add fuzz tests for malformed inputs |
| User's CLAUDE.md is an enormous personal file that takes ms to parse for marker insertion | Low | Low | Cap parse to first/last 64 KB; markers are at the boundaries anyway |
| Setup writes CLAUDE.md but user's project policy bans committing AI-tool config | Medium | Low | Print explicit `.gitignore` recommendation in summary; add a `--gitignore` flag for users who want us to also append the nudge-file paths to `.gitignore` |
| Antigravity / Cursor change their config schema mid-flight | Medium | Medium | We already handle one schema change per IDE per year; new helpers added when detected; non-tier-1 IDEs are best-effort by definition |
| Hook installation breaks user's existing Claude Code hooks | Low | High | Always **prepend** to existing event lists, never replace; cover with an integration test that plants a user hook first |
| `codevira setup` is run on a machine without a writable `~/.claude/` (corp-locked) | Low | Medium | Setup detects and reports: "Cannot write to ~/.claude (read-only). Use `--no-hooks` or talk to your IT admin." |
| Founder's own daily-use Claude Code session breaks during Pillar 1 dogfood | Medium | Medium | Pillar 1 implementation lives on a feature branch; founder dogfoods using a side project, not the main CodeVira repo, until merged |

---

## Out of scope (deferred)

- **Uninstall** (`codevira setup --uninstall`) — not in v2.0; deferred to a v2.1 patch. Workaround: hand-edit the markers out, or `pipx uninstall codevira`.
- **Per-IDE customization** of the canonical block — single block for v2.0; per-IDE overrides come if/when users ask.
- **`codevira doctor`** — a separate command; specced in master plan, not in this Pillar 1 doc.
- **Web wizard / GUI installer** — not happening. CLI-first product.

---

## Definition of done

- [ ] `codevira setup` exists, end-to-end, exits 0 on a fresh machine in <5s.
- [ ] All 10 acceptance tests pass.
- [ ] Tier-1 QA sweep (all 8 angles) green.
- [ ] Founder dogfooded for ≥ 24 hours on a real project; no blockers.
- [ ] `cmd_register` redirects with the deprecation message.
- [ ] `docs/v2-execution-log.md` Week-3 entry written.
- [ ] PR description includes a 30-second screen-recording of `setup` running on a fresh project.
