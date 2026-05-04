# v2.0 Completion Plan — closing every gap from the master plan

This is the honest, exhaustive list of everything STILL pending vs the original v2.0 master plan, after Weeks 4-14 (10 heroes + RC gate) shipped.

## ✅ Already shipped

- **Heroes 1-10** (10/10) — all behavioral policies + Hero 8's browse surface
- **Pillar 1.1** `codevira setup` (`mcp_server/setup_wizard.py`)
- **Pillar 1.2** `codevira config` snippet generator
- **Pillar 1.5** Interactive `configure` (numbered-list prompts via plain `input()`)
- **Pillar 2.1** All 6 IDE nudge templates (`mcp_server/data/templates/`)
- **Pillar 2.3** Claude Code lifecycle hooks (5 scripts in `mcp_server/data/hooks/`)
- **Pillar 2.4** Anti-cross-project bleed (v1.8.1 fix in tree)
- **Pillar 2.5** MCP Apps integration (Hero 8's `codevira://decisions` resource)
- **Pillar 3.6** v1.8.1 crash fixes
- **Pillar 4.2** 30-second demo video (`docs/demo/codevira-demo.mp4`)
- **Week 14 RC gate** (28 tests, 695 / 695 total)
- **`codevira register`** — deprecated but kept working with redirect message

## ❌ Pillar 1 — UX install gaps

### 1.3 `codevira doctor` health check — **NOT SHIPPED**

Spec from master plan section 1.3:
> Server responsive? Watcher alive? Cert valid (if HTTPS)? IDE configs present + well-formed? Output: ✓/✗ per check + exact command to fix each ✗.

**Effort**: ~150 LOC `mcp_server/doctor.py` + CLI subcommand + ~10 unit tests. ~1 day.

**Why it matters**: First time something breaks (watcher silently failed, IDE config went stale, cert expired), users have NO diagnostic. They open GitHub issues that we then have to walk through.

### 1.4 Error-message audit (Pillar 1.4) — **NOT DONE**

Spec:
> Every `print(f"Error: ...", file=sys.stderr)` and `raise RuntimeError(...)` site gets a `"→ to fix: <command>"` suffix.

**Effort**: per-site grep + edit. ~½ day.

**Why it matters**: Currently errors point at symptoms, not solutions. Pattern locked in only at the v1.8.1 project-root guard.

### Newly identified: `[ui]` extra with `questionary`

Spec from master plan section 1.5:
> Optional `[ui]` extra → `questionary`. Default install unchanged.

**Status**: `pyproject.toml` has no `[ui]` extra; questionary not a dependency. Interactive prompts use plain `input()`.

**Effort**: ~30 LOC + add extra to pyproject + opt-in import paths. ~½ day.

**Why it matters**: Polish — `configure` and `setup` would feel modern (multi-select checkboxes, fuzzy search) instead of numbered-list prompts. Not blocking.

## ❌ Pillar 2 — Universality coverage gaps

### 2.2 `codevira agents` standalone CLI — **NOT WIRED**

Spec from master plan section 2.2:
> The unified command that emits all of the [nudge files]… Section-replaceable on regenerate. `--dry-run` shows what would be written; `--ide=<name>` for one tool.

**Status**: `mcp_server/agents_md.py` module exists with the generator logic. The standalone `codevira agents` subcommand is NOT wired in `cli.py`. The functionality is reachable via `codevira setup` (which generates everything) but not as a focused re-runner.

**Effort**: ~50 LOC subparser + dispatch handler. **~1 hour.**

**Why it matters**: Power users will want to regenerate ONLY the nudge files without re-running full setup (e.g., they edited templates locally, or new IDE installed mid-project). Today they have to re-run `setup`, which is heavier.

### 2.3 `codevira hooks install` standalone CLI — **NOT WIRED**

Spec:
> Wire into every IDE that exposes lifecycle events… Each handler shells out to `codevira` CLI; portable.

**Status**: Hook scripts exist (5 files in `mcp_server/data/hooks/`). Installer logic exists in setup_wizard. Standalone `codevira hooks install` subcommand is NOT wired.

**Effort**: ~40 LOC. **~1 hour.**

**Why it matters**: Same as 2.2 — focused re-runner for power users. Also useful when Cursor/Windsurf ship hooks (we want to add them without re-running full setup).

### Newly identified: `codevira test-ide <name>` smoke test — **NOT SHIPPED**

Spec from master plan files-affected section:
> `mcp_server/test_ide.py` — `codevira test-ide <name>` — smoke test for any specific tool (tier-1 verification or tier-2 user-driven check).

**Status**: file doesn't exist; subcommand not wired.

**Effort**: ~150 LOC + matrix per IDE. ~½ day.

**Why it matters**: Coverage matrix verification. Without this, we can't programmatically confirm "yes, codevira works in Cursor today" — we rely on user reports.

## ❌ Pillar 3 — Existing-issues backlog (5 deferred items)

| # | Item | Status | Effort |
|---|---|---|---|
| 3.1 | `crash_logger` 5MB cap + rotation | deferred from v1.8.1 | ~30 LOC; ~1 hr |
| 3.2 | Watcher restart circuit breaker (exp backoff) | deferred from v1.8.1 | ~80 LOC; ~½ day |
| 3.3 | Shared `_enable_wal_with_retry` util | duplicated in 2 files | ~40 LOC dedup; ~1 hr |
| 3.4 | 14 `except Exception: pass` audit | all 14 still present (cli=6, server=4, indexer=4) | per-site work; ~½-1 day |
| 3.5 | Hot-reload of `config.yaml` | not shipped | ~80 LOC + watcher; ~½ day |

**Verified count for 3.4**: I just ran `grep -c "except Exception: pass"` — exactly 14 sites still present (matches the master plan's count exactly). **This audit was never done.**

## ❌ Pillar 4 — HN-launch deliverables

### 4.1 README v2.0 rewrite — **PARTIAL**

**Current**: README has the wedge headline + "4 pains" framing. **No mention of the 10 heroes.** Doesn't reflect v2.0 capability set.

**Effort**: ~3 hours.

### 4.3 `docs/vs-other-memory-tools.md` — **NOT SHIPPED**

Spec: comparison vs Mem0, Zep, claude-mem, MemPalace, MemClaw, Väinämöinen, memsearch.

**Effort**: ~3 hours.

### 4.5 Recruit alpha testers — **MANUAL (you)**

Goal: 3 alpha testers running v2.0 for ≥2 days before HN.

## 🆕 Newly-identified gaps (from this audit)

These weren't on my original Week-14 pending list but I'm flagging them now:

### G1. `codevira register` deprecation timing
- Today: still works, prints deprecation. Plan: "removed in a future release." 
- **Decision needed**: when? v2.0.x? v2.1? Unaddressed.

### G2. Cross-tool E2E smoke test for the wedge
- **The wedge promise** is the heart of v2.0: same memory in Claude Code → Cursor → Windsurf → Antigravity.
- We have NO automated test that verifies this end-to-end. Week-14 RC tested signals + dispatch coherence, NOT actual cross-tool data flow.
- The North Star section of the master plan literally says "**The cross-tool-per-project test**" — and it's not automated.
- **Effort**: ~150 LOC harness that simulates 4 IDE clients reading the same project + asserts identical context. ~½ day.

### G3. v2.0 release notes / RELEASE_NOTES.md update
- Last entry was v2.0-alpha.2. We're now at full v2.0 (10 heroes done, RC gate clean). No release notes for the most recent 4 commits / 4 weeks.
- **Effort**: ~1 hour.

### G4. Founder dogfood checklist (DOGFOOD.md exists from Week 7)
- Was scoped for 48-hour pre-flight check. Now needs to be the broader "1 week of real use" gate before alpha tester recruitment.
- **Effort**: ~30 minutes to update the existing doc with the v2.0 hero set.

### G5. `codevira agents` template/canonical-block consistency check
- Today's universality wedge depends on every IDE getting THE SAME canonical instructions block. If `claude_md.tmpl` and `agents_md.tmpl` drift, the wedge breaks silently.
- We have NO test asserting "the canonical instructions appear in every rendered template."
- Week-14 H section had a permissive version of this; should be tightened.
- **Effort**: ~50 LOC test. ~1 hour.

## Summary by tier

### Tier A — block alpha tester ship (must do)

| Item | Effort | Why |
|---|---|---|
| 2.2 `codevira agents` standalone CLI | 1 hr | Wedge is incomplete without focused regen |
| 2.3 `codevira hooks install` CLI | 1 hr | Same |
| 1.3 `codevira doctor` | ~1 day | First-aid for users; Pillar 1 incomplete without it |
| G2 Cross-tool E2E test | ½ day | The North Star promise has no automated guard |
| G5 Template consistency test | 1 hr | Wedge fails silently if templates drift |
| 4.1 README v2.0 rewrite | 3 hr | Heroes section + sharpened wedge |
| G3 RELEASE_NOTES.md | 1 hr | Don't ship without notes |

**Tier A total: ~2 focused days**

### Tier B — polish before HN (should do)

| Item | Effort |
|---|---|
| 4.3 Differentiation page | 3 hr |
| 1.4 Error-message audit | ½ day |
| 3.4 14 `except Exception: pass` audit | ½-1 day |
| 3.1 crash_logger rotation | 1 hr |
| 3.2 Watcher circuit breaker | ½ day |
| 3.3 Shared `_sqlite_util` | 1 hr |

**Tier B total: ~2-3 days**

### Tier C — defer to v2.0.x

| Item | Why deferred |
|---|---|
| 3.5 config.yaml hot-reload | Nice-to-have; users can restart |
| `codevira test-ide` smoke test | Internal tooling; not user-facing |
| `[ui]` extra with questionary | Polish; current input() works |
| G1 `register` removal | Defer to v2.1 |

### Tier D — manual (you)

- Founder dogfood (1 week real use)
- Recruit 3 alpha testers
- HN submission

## Suggested execution order

**Day 1 (Tier A, easy wins first):**
1. `codevira agents` CLI subcommand (1 hr)
2. `codevira hooks install` CLI subcommand (1 hr)
3. G3 RELEASE_NOTES.md update (1 hr)
4. G5 Template consistency test (1 hr)
5. G2 Cross-tool E2E test (½ day)

**Day 2 (Tier A, larger items):**
6. `codevira doctor` (full day — health check + tests)
7. 4.1 README v2.0 rewrite (3 hr)

**Day 3-4 (Tier B):**
8. 4.3 Differentiation page (3 hr)
9. 1.4 Error-message audit (½ day)
10. 3.1-3.4 backlog (1.5-2 days)

**Day 5+ (Tier C + manual):**
- Defer Tier C
- Founder dogfood week
- Alpha tester recruiting
- v2.0-rc.1 tag → testers
- After clean: v2.0.0 GA + HN
