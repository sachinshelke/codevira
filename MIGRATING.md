# Migrating to Codevira

## Upgrading to 3.7.0

**Just `pipx install --upgrade codevira`. There are no manual steps.** The
upgrade is automatic and non-breaking — codevira migrates itself.

### What happens automatically (you do nothing)

The first time your IDE launches codevira after the upgrade, a startup
self-heal runs. It's version/ledger-gated (each step runs at most once),
lock-protected (two IDE windows can't race), backup-first, and
failure-isolated (it can never block startup):

- **Pre-3.7 decision-id collisions are repaired.** If a shared repo's
  `decisions.jsonl` carried two records minted with the same id (one silently
  shadowed on read), codevira repairs it deterministically. `decisions.jsonl`
  is backed up first (`.bak-pre-v370`), and the rewrite goes through the same
  exclusive lock + atomic replace a normal write uses — **safe even while you're
  actively coding** (a concurrent `record_decision` is serialized, not lost).
- **The decision-log git merge driver is installed**, so future cross-engineer
  id collisions resolve automatically on `git merge` / rebase.
- **A stale per-project MCP entry is cleaned up** — but only when a single
  global entry already exists, so a per-project-only setup is never orphaned.

No data migration: existing `.codevira/decisions.jsonl` and
`~/.codevira/global.db` are read as-is; the on-disk schema is unchanged.

### Default-behavior changes worth knowing

- **Single user-scope MCP registration.** `codevira init` registers ONE
  user-scope `codevira` server that resolves the active project from your
  editor's workspace roots at runtime, instead of a per-project entry — so N
  projects no longer create N codevira entries. Re-running `init` now also
  removes any stale per-project entry it wrote in an earlier version, so you
  never end up with a duplicate. Opt back into per-project with `codevira init
  --per-project` (or `CODEVIRA_INIT_PER_PROJECT=1`).
- **Supersede-on-write.** `record_decision` now *supersedes* a strong,
  unprotected near-duplicate instead of appending a parallel twin (append-only
  and reversible — the audit trail is preserved). Protected (`do_not_revert`)
  near-duplicates are never auto-superseded. Restore the old append-a-twin
  behavior with `force=True` or `CODEVIRA_SUPERSEDE_ON_RECORD=0`.

New tool: `mark_decision_outdated(decision_id, reason)` — retire a decision
with no successor (protected decisions need `force=True`).

### Verifying / manual fallback (not normally needed)

`codevira doctor` gains a `merge_driver` check and a `decision_collisions`
check. If you ever want to force the collision repair by hand (the startup
heal does it for you), run `codevira repair-ids --apply`.

## Upgrading to 3.0.0

**v3.0.0 is a tool-surface contraction.** Existing `.codevira/`
data (decisions, sessions, roadmap, manifest, AGENTS.md) is
fully compatible — no on-disk migration needed. What changes is
the MCP tool surface your AI sees + a few CLI subcommands that
were renamed or removed.

### What to do after `pipx install codevira==3.0.0`

```bash
# 1. Force-reinstall to pick up the new wheel (3.0.0 dropped chromadb
#    transitive deps; pipx may need the explicit force).
pipx install --force codevira==3.0.0

# 2. Re-run setup. v3.0.0 hardened IDE auto-detection — re-detection
#    + re-registration keeps the per-IDE configs consistent with the
#    new behavior.
codevira setup -y

# 3. Verify health on every project you use codevira on.
cd /path/to/your-project
codevira doctor   # should report ≥11 checks pass
```

### What's removed in v3.0.0 (breaking)

The 2026-05-22 surface-cut audit deleted 21 MCP tools and 8 CLI
subcommands that nobody called in real-world usage. Full list in
`docs/surface-cuts-2026-05-22.md`. Highlights:

- **MCP tools deleted:** `start_changeset` / `update_changeset_progress` /
  `complete_changeset` / `list_open_changesets`,
  `record_preference` / `search_preferences` / `delete_preference`,
  `record_learned_rule` / `search_learned_rules` / `retire_rule`,
  `record_project_maturity` / `query_project_maturity`,
  `list_nodes` / `add_node` / `update_node` / `export_graph`,
  `record_decisions` (batch — use `record_decision` in a loop),
  `write_session_logs` (batch — same),
  `mark_decision_protected` (use `supersede_decision(..., do_not_revert=True)`
  for the same flip + an audit trail).
- **CLI subcommands deleted:** `heal` (use `reset`), `budget`,
  `agents`, `hooks`, `register`, `configure`, `report` (folded into
  `doctor`), `calibrate`.
- **Per-IDE nudge files collapsed.** v2.x wrote `CLAUDE.md` /
  `GEMINI.md` / `.windsurfrules` / `.cursorrules` etc. v3.0.0 writes
  exactly one nudge file: `AGENTS.md`. The per-IDE duplicates are
  preserved if they already exist (no auto-deletion) but `setup` no
  longer creates new ones.

If your CI / scripts depended on any of the removed tools / commands,
they need to be rewritten before upgrading.

### What's new (non-breaking)

- **`codevira uninstall`** — reverses every system write made by
  `init` / `setup`. Drops the MCP entry from `~/.claude.json`, deletes
  `~/.claude/hooks/codevira-*.sh`, strips codevira-tagged
  registrations from `~/.claude/settings.json`, removes per-project
  `.codevira/` + `.codevira-cache/` dirs, and strips the codevira
  marker block from each project's `AGENTS.md` (preserves user
  content outside the markers byte-for-byte). Optional
  `--keep-data` skips per-user `~/.codevira/`.

- **`codevira setup --force`** — escape hatch for the rare case
  where codevira's IDE detector misses an install (portable binary
  not on PATH, non-standard config location). Without `--force`,
  passing `--ide cursor` on a machine where Cursor isn't
  auto-detected raises a clear error instead of silently failing.

- **Storage layer concurrent-write safety** (RC audit hardening,
  rounds 2 + 3). Every on-disk write in the product surface now
  goes through `mcp_server/storage/atomic.py` — crash-safe atomic
  writes + Posix `fcntl.flock` (Windows sentinel-file fallback).
  Two `codevira` MCP server processes (Claude Desktop + Cursor
  running side by side) writing to the same project no longer race
  on `manifest.yaml` / `roadmap.yaml` / `AGENTS.md`. See
  `docs/plans/v3.0.0.md` for the full RC-audit story and
  `tests/storage/test_concurrent_writes.py` +
  `tests/storage/test_cross_process_writes.py` for the regression
  guards.

### Known limitations carried into v3.0.0

See [ROADMAP.md § Known limitations](ROADMAP.md) — short version:

1. The code graph still lives at `<data_dir>/graph/graph.db`
   (centralized) rather than the documented v3.0.0 spec location
   `<project>/.codevira-cache/graph.sqlite`. Runtime is correct;
   reconciliation is a v3.1 follow-up.
2. No decision-input sanitization (null bytes / 1 MB text /
   `../../etc/passwd` in `file_path` all accepted today).
3. Cross-process file locking tested on macOS + Linux CI;
   Windows sentinel-file path verified via unit-test simulation
   but not yet exercised on real Windows under load.

---

## Upgrading to 2.2.0

**v2.2.0 is a clean-break upgrade. No automatic migration from v2.1.x decisions.**

Why no migration: v2.2.0 moves decisions from `~/.codevira/projects/<key>/
graph.db` (binary SQLite per-user) into `<repo>/.codevira/decisions.jsonl`
(text per-repo, committed to git). Auto-migrating would silently rewrite
the user's repo + commit a binary-derived blob; we explicitly avoid that.

### What to do on each project after `pipx install codevira==2.2.0`

```bash
cd /path/to/your-project
codevira init             # scaffolds .codevira/, updates AGENTS.md + .gitignore
git add .codevira/ AGENTS.md .gitignore
git commit -m "Add codevira v2.2.0 in-repo memory"
```

That's it. Future `record_decision` calls write to `.codevira/decisions.jsonl`.

### Preserving v2.1.x decisions (optional)

If you want your v2.1.x decisions visible as a read-only reference:

```bash
codevira archive-legacy   # writes .codevira/legacy.jsonl (one-shot export)
```

This is NOT a migration — the entries don't enter the active decision
pool, don't appear in search, don't update AGENTS.md. They're just a
text record of what was in the old graph.db, so a teammate can grep
through them if needed.

### What's removed in v2.2.0

| Feature | v2.1.x | v2.2.0 |
|---|---|---|
| ChromaDB / sentence-transformers / torch | runtime deps | **removed** |
| `search_codebase` MCP tool | semantic code search | **removed** (agents grep + Read) |
| `codevira calibrate` CLI | tune semantic thresholds | **removed** (FTS5 has none) |
| `codevira heal --decisions` | backfill embeddings | **removed** (`codevira sync` rebuilds FTS5) |
| Decision IDs as integers | `1`, `2`, `3` | string `D000001`, `D000002` |
| Cross-session inject mode | `inject` / `off` | renamed to `relevance_inject` |
| Cross-session env vars | `CODEVIRA_CROSS_SESSION_*` | renamed to `CODEVIRA_INJECT_*` |

### What's new in v2.2.0

- **`codevira sync`** — regenerate AGENTS.md + manifest + digest + FTS5 from
  `.codevira/decisions.jsonl`. Manual / recovery path; every record_decision
  triggers regen synchronously by default.
- **`codevira observe-git`** — classify decisions as kept/modified/reverted
  from git history. Updates `digest.weight` so the relevance hook
  deprioritizes reverted decisions.
- **AGENTS.md auto-generated** with 5 KB cap, marker-bounded (preserves
  user content outside `<!-- codevira:begin -->` / `<!-- codevira:end -->`).
- **Per-project disk drops from 40-80 MB to ~1-2 MB**; pipx install drops
  from ~200 MB to ~50 MB; MCP startup drops from 1-3s to <100ms.

See `docs/plans/v2.2.0.md` for the full architectural plan + rationale.

---

## Migrating to Codevira 2.0 (historical — May 2026)

This section is historical reference for users upgrading directly from
1.x to 2.0 (without stopping at 2.1.x). v2.2.0 supersedes this guide
for current upgrade paths.

This guide walks through upgrading from Codevira 1.x to **2.0.0** (or
later 2.x). Codevira 2.0 is a substantial change: the memory layer becomes
**active** (intercepts every AI tool call) instead of passive (the AI looks
things up). Most upgrades are seamless — your existing data carries forward
— but a few defaults changed and one CLI command is on a deprecation path.

> **TL;DR — for the impatient.** `pipx install --upgrade codevira` picks
> up 2.0. Existing `~/.codevira/global.db` migrates safely (idempotent
> dedup runs once on first connect). Three default-behavior changes: `init`
> now indexes all source/config/docs extensions; `agents` only renders for
> detected IDEs; `register` is deprecated (use `setup`). No data loss.

---

## Table of contents

1. [Compatibility matrix](#compatibility-matrix)
2. [Step-by-step upgrade](#step-by-step-upgrade)
3. [What changed in 2.0 — the short list](#what-changed-in-20--the-short-list)
4. [Behavior changes that may surprise existing users](#behavior-changes-that-may-surprise-existing-users)
5. [Deprecations and the `register` removal timeline](#deprecations-and-the-register-removal-timeline)
6. [What's the same and just works](#whats-the-same-and-just-works)
7. [New 2.0 features you should know exist](#new-20-features-you-should-know-exist)
8. [Troubleshooting common upgrade issues](#troubleshooting-common-upgrade-issues)
9. [Rollback to 1.8.0 if needed](#rollback-to-180-if-needed)

---

## Compatibility matrix

| You're on | Upgrade path | Data migration | User action |
|-----------|--------------|----------------|-------------|
| **1.6.x** | `pipx install --upgrade codevira` | Auto on first run | Run `codevira setup` once |
| **1.7.x** | Same | Auto on first run | Run `codevira setup` once |
| **1.8.0** | Same (last 1.x on PyPI) | Auto on first run | Run `codevira setup` once |
| **1.8.1** | (was internal-only; never on PyPI) | Already 2.0-shaped | Run `codevira setup` once |
| **fresh install** | `pipx install codevira` | None | Run `codevira setup` |

If you previously installed the `2.0.0rc1` release candidate via
`pip install --pre codevira`, plain `pipx install --upgrade codevira`
moves you to the 2.0.0 final.

---

## Step-by-step upgrade

### 1. Install 2.0.0

```bash
# Recommended: pipx (isolated venv)
pipx install --upgrade codevira

# OR pip (in your project's environment)
pip install --upgrade codevira

# Verify
codevira --version    # codevira 2.0.0
```

### 2. Run setup once to refresh per-IDE configs

The setup wizard is the v2.0 successor to `register`. It detects every AI
tool installed on your machine, configures all of them with codevira, and
installs the lifecycle hooks that power the v2.0 hero policies.

```bash
codevira setup -y         # non-interactive; -y to skip the confirmation prompt
```

This writes:
- `~/.claude.json` (Claude Code MCP entry)
- `~/Library/Application Support/Claude/claude_desktop_config.json`
- `~/.codeium/windsurf/mcp_config.json`
- `~/.gemini/antigravity/mcp_config.json`
- `~/.claude/hooks/codevira-*.sh` + registration in `~/.claude/settings.json`
- Per-project nudge files: `CLAUDE.md`, `AGENTS.md`, `.cursor/rules/codevira.mdc`,
  `.windsurfrules`, `GEMINI.md`, `.github/copilot-instructions.md` (only the
  ones for IDEs detected on your machine — see [P1-1 in the changelog](CHANGELOG.md))

It is **idempotent** — re-runs only touch what changed. Safe to run anytime.

### 3. Verify health

```bash
codevira doctor       # 14 checks; expect all ✓ or ⚠ with actionable hints, 0 ✗
```

If `doctor` reports `⚠ ghost_projects N of M project dir(s) are ghosts`,
that's leftover state from earlier installs. Clean it up:

```bash
codevira projects --ghosts-only        # list them
codevira clean --ghosts                # remove them (preserves tracked projects)
```

### 4. (Optional) Restart your AI tool

Claude Code, Cursor, Windsurf etc. load the MCP server **once at session
start**. After upgrade, restart the AI tool so it picks up the 2.0
binary + the new lifecycle hooks.

---

## What changed in 2.0 — the short list

### Activated (new in 2.0)

* **10 hero policies** intercept every AI tool call (`Edit`, `Write`,
  `UserPromptSubmit`, `SessionStart`) and route through the engine.
  Policies block / warn / inject context as appropriate. Set
  `CODEVIRA_ENGINE=0` to kill-switch all policies in one env var.

* **Cross-tool universality.** One `codevira setup` command configures
  Claude Code, Cursor, Windsurf, Antigravity, OpenAI Codex, GitHub
  Copilot, Continue.dev, and Aider — each in the right config file with
  the right schema. No per-IDE script.

* **`codevira projects`** — the canonical "what does codevira know about
  this machine?" command. `--json` for scripting, `--ghosts-only` to find
  half-initialised data dirs.

* **`codevira hooks list / uninstall`** — admin commands so removing
  Claude Code lifecycle hooks no longer requires hand-editing
  `~/.claude/settings.json`.

* **`codevira insights` / `codevira replay`** — git-grounded outcome
  tracker. Shows which past decisions held up vs got reverted; surfaces
  emerging style preferences inferred from your post-edit corrections.

### Improved (existing tools, better behavior)

* **`search_codebase`** falls back to structural matches (filename +
  symbol substring) when the semantic index isn't built — instead of
  erroring out with "Reinstall codevira."

* **`get_node` / `get_impact` / `query_graph`** distinguish three failure
  modes ("no graph DB" / "graph empty" / "file not in populated graph")
  with the right `fix_command` per case.

* **`get_decision_confidence`** exposes counts so you understand WHY
  `total_decisions: 0` (most common cause: decisions recorded without
  `file_path=` can't be classified by the git-based outcome tracker).

* **`codevira doctor`** is now genuinely read-only. It snapshots
  `~/.codevira/projects/` at entry and removes any directories that
  appeared during the run, restoring the contract docs always promised.

---

## Behavior changes that may surprise existing users

**Three defaults changed in 2.0. None are destructive — all are
opt-out-able if you want the legacy behavior.**

### 1. `codevira init` indexes everything by default

Pre-2.0, a Python project got `file_extensions: ['.py']` and silently
dropped `.yaml`, `.md`, `.html`, `.json`, etc. Polyglot projects lost
half their files.

**Now:** the union of every common source / config / docs extension
(~75 total) is indexed by default. Pass `--single-language` for the
legacy single-language narrowing.

```bash
# new default — indexes .py + .ts + .yaml + .md + .html + .go + ...
codevira init

# legacy behavior (only the dominant language's extensions)
codevira init --single-language
```

**Impact on existing projects:** ZERO until you re-run `init`. Existing
`config.yaml` files keep working as-is. To get the new wide indexing on
an existing project, run `codevira configure` (interactive picker) or
`codevira configure --extensions .py,.ts,.yaml,.md` (explicit list).

### 2. `codevira agents` only renders for detected IDEs

Pre-2.0, `agents` rendered nudge files for every supported IDE
regardless of whether it was installed on your machine. Files appeared
in `.cursor/`, `.github/copilot-instructions.md`, etc., even if you
didn't use those tools.

**Now:** `agents` defaults to **detected IDEs only**. Pass `--ide=all`
to restore the legacy "render for everything" behavior.

```bash
# new default — only generates files for IDEs you actually have
codevira agents

# legacy behavior — generate for every supported IDE
codevira agents --ide=all
```

**Impact on existing projects:** existing nudge files aren't deleted.
But re-running `codevira agents` won't refresh nudge files for IDEs
you don't have installed. To force a refresh of all of them, use
`--ide=all`.

### 3. `codevira register` is deprecated; use `codevira setup`

`codevira register` was the v1.x command that injected MCP server
config into IDE config files. v2.0 introduces `codevira setup` which
does that **plus** installs the Claude Code lifecycle hooks **plus**
writes per-IDE nudge files — all in one prompt.

**Now:** `register` still works in 2.0.x and prints a deprecation hint.
**It will be removed in v2.1.** Switch to `setup` at your convenience.

```bash
# v1.x — still works in 2.0.x but deprecated
codevira register

# v2.0+
codevira setup
```

If you're scripting `register` in CI, plan to migrate before v2.1.

---

## Deprecations and the `register` removal timeline

| Version | `codevira register` |
|---|---|
| 2.0.x | Works; prints deprecation hint pointing at `codevira setup` |
| **2.1.x** | **Removed** — invocation errors with "use `codevira setup` instead" |
| 2.2.x+ | Gone for good |

There are no other deprecations in 2.0.0. See the [v2.1 roadmap
section](ROADMAP.md#-v21--honest-known-limitations-from-the-rc5-audit-2026-05-13)
for design-tension items being resolved next.

---

## What's the same and just works

These didn't change in 2.0 — your existing scripts / configs / data work
unchanged:

* **`~/.codevira/global.db`** schema unchanged. All your registered
  projects, learned preferences, learned rules, decisions — all still
  there. A one-shot dedup migration runs on first connect (idempotent;
  no-op on already-clean DBs).
* **`~/.claude/hooks/codevira-*.sh` shell scripts** unchanged. They
  invoke the codevira binary via `${HOME}/.local/bin/codevira`, so they
  pick up the 2.0 binary automatically once pipx finishes the upgrade.
* **`.codevira/config.yaml`** schema unchanged. Existing per-project
  configs work. (Optional new key: `cross_session_mode: off` to disable
  the per-prompt context-injection block — see [P0-F in the
  changelog](CHANGELOG.md).)
* **All MCP tool surfaces backward compatible.** Existing fields
  preserved in every response; new fields added (e.g., `fix_command`
  on error responses) but nothing renamed or removed.
* **`get_session_context`, `search_decisions`, `record_decision`,
  `get_roadmap`, `complete_phase`** etc. — all the same shapes you've
  been calling.

---

## New 2.0 features you should know exist

If you've been using codevira 1.x, here's what's worth trying once
you're on 2.0:

### `codevira projects`

```bash
codevira projects                    # human-readable table
codevira projects --json             # machine-readable
codevira projects --ghosts-only      # only incomplete project dirs
```

Shows every project codevira knows about, classified as `tracked` /
`ghost` / `orphan` / `stale`. Pairs with `clean --ghosts` for surgical
cleanup.

### `cross_session_mode` per-project opt-out

The Hero 5 (Cross-Session Consistency) policy injects a
"prior decisions you may want to consider" block on every
`UserPromptSubmit` — ~1 KB of relevant prior decisions. Useful for
some projects, noise for others.

```yaml
# .codevira/config.yaml
project:
  cross_session_mode: off       # disables the per-prompt injection
  cross_session_max_inject: 2   # OR keeps it but caps at 2 entries (default 5)
```

Or via env var: `CODEVIRA_CROSS_SESSION_MODE=off`.

### `codevira insights` and `codevira replay`

```bash
codevira insights              # 7-day summary of stable + reverted decisions
codevira insights --since 30d --top 10
codevira replay                # 30-day decisions timeline
codevira replay --format html --out timeline.html
```

Powered by Hero 8 (Decision Replay) and Hero 10 (AI Promotion Score).
For these to produce signal, decisions need to be recorded with
`file_path=...` so the git-based outcome tracker can classify them as
kept / modified / reverted across subsequent commits.

### `codevira doctor` health check

```bash
codevira doctor                # 14 checks
codevira doctor -v             # verbose: include details under each WARN/FAIL
```

Use this whenever something feels off. Each WARN/FAIL ships with a
concrete `fix_command` you can run.

---

## Troubleshooting common upgrade issues

### `claude mcp list` shows `✗ Failed to connect` for codevira after upgrade

**Cause:** the MCP server in any open Claude Code conversation was
loaded with the v1.x binary; replacing the on-disk binary disconnects
the running process.

**Fix:** restart Claude Code. New conversations get the 2.0 binary.

### `codevira doctor` reports ghost projects

**Cause:** leftover state from earlier installs (or from the v1.6
auto-init flow that didn't always complete its bookkeeping).

**Fix:**
```bash
codevira projects --ghosts-only        # see what's flagged
codevira clean --ghosts                # remove them
```

This preserves all your `tracked` projects and their indexes — only
removes the half-initialised ones.

### `pipx upgrade codevira` says "already at 1.8.0" instead of upgrading

**Cause:** pipx may have a stale cache or the wrong Python interpreter
on your system pointing at the old install.

**Fix:** force reinstall:
```bash
pipx install --force --upgrade codevira
# OR pin explicitly
pipx install --force codevira==2.0.0
```

### `codevira` no-args prints help instead of starting an MCP server

**Not a bug — that's a 2.0 fix.** Pre-2.0, `codevira` with no args
silently entered MCP-server stdio mode and exited with a cryptic
"No valid watched_dirs found — watcher not started". 2.0 detects an
interactive terminal and prints help instead. The MCP server still
starts when stdin is piped (i.e., when an AI tool spawns codevira
with stdio MCP transport).

### `codevira agents` no longer creates `.cursor/rules/codevira.mdc` (or some other IDE's nudge file)

**Cause:** v2.0 changed the default to render only for IDEs detected
on this machine. If Cursor isn't installed, the file isn't written.

**Fix:** explicit opt-in.
```bash
codevira agents --ide=all          # render for every supported IDE
# OR per-IDE
codevira agents --ide=cursor
```

### Tests / scripts that asserted on `search_codebase` returning `{"error": "Semantic index not found"}` now fail

**Cause:** v2.0 changed the contract. `search_codebase` now returns
`{"matches": [], "warning": "...", "fix_command": "codevira index"}`
when the semantic index isn't built — graceful structural fallback
instead of an error.

**Fix:** update the assertion to check for `warning` + empty `matches`,
not the `error` key.

### My playbook content (e.g., `get_playbook("commit")`) is empty / different

**Cause:** v2.0 changed playbook resolution to be **project-scoped
first**: `<data_dir>/playbooks/<task_type>/` then
`<project_root>/.codevira/playbooks/<task_type>/`, then bundled
defaults. Bundled defaults are Python-shaped — they're SKIPPED for
non-Python projects with a clear warning.

**Fix:** if your project is non-Python and you want playbooks, drop
your own templates into
`<project>/.codevira/playbooks/<task_type>/<your-rule>.md`. They take
precedence over bundled defaults.

---

## Rollback to 1.8.0 if needed

If 2.0 causes a real problem and you need to fall back:

```bash
# 1. Reinstall the last stable release
pipx install --force codevira==1.8.0

# 2. Re-register (1.x command — 2.0's `setup` writes config in the same
#    place but with the new schema. Reverting `setup` → `register` keeps
#    the IDE configs talking to the right binary.)
codevira register

# 3. Restart your AI tools so they pick up the 1.8.0 binary
```

Your `~/.codevira/global.db` is forward-compatible with both — the dedup
migration v2.0 runs is idempotent and doesn't change column shapes.

If rolling back, please [open an issue](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md)
describing what broke. Pre-release feedback is the entire point of the rc
cycle.

---

## Reference

- **Full v2.0.0 changelog:** [CHANGELOG.md](CHANGELOG.md#200--2026-05-14--first-public-20-release)
- **Long-form release notes:** [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **Roadmap (what's coming in v2.1):** [ROADMAP.md](ROADMAP.md)
- **Hero policy specs:** [docs/heroes/](docs/heroes/)
- **Bug reports:** [GitHub issues](https://github.com/sachinshelke/codevira/issues)
