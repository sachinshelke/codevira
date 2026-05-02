# Codevira v2.0 — Master Plan

> Single source of truth for the v2.0 release. Per-hero detailed specs live in `docs/heroes/`. Running progress log: `docs/v2-execution-log.md`.

---

## Purpose (locked)

> **Codevira: per-project memory that follows you across every AI coding tool you use.**

Three reinforcing wedge legs:

1. **Per-project persistent memory** — scoped to each project; no cross-project bleed.
2. **Decision guidance** — `do_not_revert` flags steer the AI; protected from accidental undo.
3. **Cross-tool bridging within a project** — Claude Code → Cursor → Windsurf → Antigravity all see the same project memory.

Plus one invariant: **token efficiency is non-negotiable.** ~500-token catch-up via `get_session_context()`. We are not MemPalace (4-8K).

What codevira is NOT:
- ❌ Universal cross-project memory (cross-project intelligence exists in `global.db` but is **not** the headline)
- ❌ Single-IDE plugin (claude-mem)
- ❌ Cloud / SaaS service
- ❌ Heavyweight memory engine

---

## Why v2.0 (not v1.9)

Originally scoped as v1.9 (minor bump: hotfix + UX polish). Scope grew to include:

- 10 hero capabilities + new "AI agent guardian" engine architecture
- Universal multi-tool coverage (7 tier-1 IDEs + tier-2 AGENTS.md fallback)
- Lifecycle hooks integration
- v1.8.1 production crash hotfix work folded in

That's a **major architecture + capability release**. Semver-correct: **2.0**. Marketing-correct: signals "this is big, take a fresh look."

The in-tree v1.8.1 commits stay as real history; no public v1.8.1 tag ships. Next release is v1.8.0 → v2.0.

---

## North Star — what "done" looks like

**The cross-tool-per-project test.** User is working on Project A. Opens Claude Code → asks "what did I decide about retries?" → answer from Project A's decision log. Closes Claude Code, opens Cursor on the SAME project → same question → SAME answer. Switches to Windsurf → same answer. Antigravity → same answer.

Then opens Project B in any tool — gets Project B's memory, not Project A's. **Per-project, multi-tool. Not universal.**

**60-second cold install:**

```bash
pipx install codevira
codevira setup    # detects every AI tool installed, configures all of them
```

---

## The 10 Hero Capabilities (centerpiece of v2.0)

All 10 share one engine. Building it once makes adding policies cheap (~100-300 LOC each). Engine is invisible; heroes are the visible outcomes.

### The shared engine (Hero 0 — built first)

```
┌──────────────────────────────────────────────────────────┐
│  codevira hook intercept layer                           │
│  • PreToolUse  (block / warn / allow before tool runs)   │
│  • PostToolUse (log / verify / learn after tool runs)    │
│  • SessionStart / UserPromptSubmit / Stop                │
└────────────────────┬─────────────────────────────────────┘
                     │ shared signals (built once, used by all 10)
   ┌─────────────────┼──────────────────────────────────┐
   ▼                 ▼                                  ▼
 graph + impact  decision log    fix history (NEW)
 (existing)     (existing)       git + flag-based
   ▼                 ▼                                  ▼
 style prefs    token budget meter (NEW)   scope contract (NEW)
 (existing)     instrumentation             intent parser
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│  Policy plugin API (each hero is a policy plugin)        │
└──────────────────────────────────────────────────────────┘
```

### The 10 Heroes (all ship in v2.0 GA)

| # | Hero | What it does (one line) | Engine surface used |
|---|---|---|---|
| 1 | **Active Decision Lock** | Block AI from undoing locked decisions. | PreToolUse + decision log |
| 2 | **Anti-Regression Memory** | Bugs you fix once, stay fixed. | PreToolUse + fix history |
| 3 | **Scope Contract Lock** | Asked for one fix? Get exactly one fix. | UserPromptSubmit + intent parser + PreToolUse |
| 4 | **Blast-Radius Veto** | Block edits that affect too many callers. | PreToolUse + impact engine |
| 5 | **Cross-Session Consistency** | Active recall of past decisions on related topics. | UserPromptSubmit + search_decisions |
| 6 | **Token Budget Live View** | See and optimize what your AI memory costs. | All tool responses + new accounting layer |
| 7 | **Live Style Enforcement** | AI writes in YOUR style, not generic-good. | PostToolUse + preferences engine |
| 8 | **Decision Replay** | Click any decision to see context (MCP Apps UI). | Tool response → MCP App resource |
| 9 | **Proactive Intent Inference** | Pre-fetch relevant context based on user prompt. | UserPromptSubmit + LLM intent classifier |
| 10 | **AI Promotion Score** | Adaptive learning visible to user; weekly insights. | Outcome tracker + new scoring + insights CLI |

---

## Universal multi-tool coverage matrix

| AI tool | Nudge file we generate | Lifecycle hooks | Tier |
|---|---|---|---|
| Claude Code | `<project>/CLAUDE.md`, `~/.claude/CLAUDE.md`, AGENTS.md fallback | ✅ SessionStart, PostToolUse, UserPromptSubmit, Stop | 1 |
| Cursor | `<project>/.cursor/rules/codevira.mdc` | ❌ (auto-detect when added) | 1 |
| Windsurf | `<project>/.windsurfrules` and `<project>/.windsurf/rules.md` | ❌ (auto-detect) | 1 |
| Antigravity / Gemini CLI | `<project>/GEMINI.md` and `<project>/AGENTS.md` | ❌ (auto-detect) | 1 |
| OpenAI Codex CLI | `<project>/AGENTS.md` | ❌ | 1 |
| GitHub Copilot | `<project>/.github/copilot-instructions.md` | ❌ | 1 |
| Claude Desktop | (no project file — connector via Settings UI) | ✅ same hooks via `~/.claude/` | 1 |
| **Continue.dev / Aider / Roo Code / Cline / any MCP-compatible tool** | `<project>/AGENTS.md` only | n/a | **2** (auto via Linux Foundation standard) |

`codevira test-ide <name>` smoke-test command lets users (or us) verify any specific tool end-to-end.

---

## Pillars (in addition to the 10 heroes)

### Pillar 1 — Smooth UX install/setup
- `codevira setup` (replaces multi-step register/init/configure)
- `codevira config` (snippet generator — no auto-injection of IDE configs)
- `codevira doctor` (health check; per-tool config + connection verification)
- `codevira hooks install` (Claude Code lifecycle hooks; future-proof for other IDEs)
- `codevira agents` (universal nudge-file generator from canonical block)
- `codevira test-ide <name>` (smoke test for any tool)
- Better error messages with "→ to fix: <command>" lines
- Checkbox UI for `configure` (questionary opt-in via `[ui]` extra)

### Pillar 3 — Existing-issues backlog (audit findings)
- `crash_logger` size cap + rotation
- Watcher restart circuit breaker (exponential backoff)
- `_enable_wal_with_retry` shared util in `indexer/_sqlite_util.py`
- Silent exception swallowing audit (14 sites)
- Hot-reload of `config.yaml`
- Tier-1 v1.8.1 crash fixes preserved (10 guarded sites + `clean --orphans`)

### Pillar 4 — HN-launch deliverables (cut variable if anything slips)
- README rewrite — per-project + multi-tool first
- 30-second demo video (cross-tool continuity moment)
- `docs/vs-other-memory-tools.md` differentiation page (vs Mem0, Zep, claude-mem, MemPalace, etc.)
- Alpha tester recruitment + feedback loop
- HN warm-up + submission

---

## Execution model — one hero at a time, focused planning per hero

This master plan is intentionally high-level on the heroes. **Detailed per-hero specs live in `docs/heroes/NN-<hero-name>.md`**, written *just before* each hero is built — not all upfront.

**Why:** focus, learning, reviewability, no premature commitment. Heroes 1-3 ship and inform Hero 4's spec.

### Documentation structure

| Path | Purpose |
|---|---|
| `docs/v2-master-plan.md` | This file. High-level v2.0 vision + sequencing. |
| `docs/heroes/00-engine.md` | Shared engine spec — written first, blocks everything else. |
| `docs/heroes/01-decision-lock.md` | Hero 1 detailed spec. Written just before Hero 1 starts. |
| `docs/heroes/02-anti-regression.md` | Hero 2 detailed spec. |
| ... | one per hero, written just-in-time |
| `docs/v2-execution-log.md` | Running log: what shipped, what changed, what we learned. |

### Per-hero planning loop (×10 + engine)

For each hero, before any code:

1. **Spec in `docs/heroes/NN-name.md`** (under 500 lines):
   - Problem statement (1 paragraph)
   - User pain it solves (concrete example)
   - Mechanism (which hook, signals, when block/warn/allow)
   - Configuration knobs
   - Edge cases (false-positive handling)
   - Demo storyboard (10-second scene)
   - Acceptance test list (5-10 scenarios)
2. **Quick spec review** — sleep on it once before coding.
3. **Code** — feature branch `hero/NN-name`.
4. **Tests** — pytest scenarios from acceptance list.
5. **Founder dogfood** — install the hero into your own daily Claude Code work for ≥48 hours.
6. **Alpha release** — bundle into next alpha (every 2-3 weeks).
7. **Update `docs/v2-execution-log.md`** with what you learned.
8. Move to next hero.

---

## Sequencing (dependency-ordered)

```
Wk 1-2  Engine (Hero 0 — shared infrastructure)
Wk 3    Pillar 1 (UX install) — alpha.1 must be installable
Wk 4    Hero 4 — Blast-Radius Veto       ← simplest, uses existing get_impact
                                           [v2.0-alpha.1]
Wk 5    Hero 1 — Decision Lock           ← extends existing do_not_revert
Wk 6    Hero 5 — Cross-Session Consistency ← uses UserPromptSubmit + search_decisions
Wk 7    Hero 6 — Token Budget Live       ← instrumentation work
                                           [v2.0-alpha.2]
Wk 8    Hero 2 — Anti-Regression Memory  ← needs new git fix-detection helper
Wk 9    Hero 7 — Live Style              ← extends preferences engine
Wk 10   Hero 10 — AI Promotion Score     ← extends outcome_tracker + rule_learner
                                           [v2.0-alpha.3]
Wk 11   Hero 9 — Proactive Intent        ← needs LLM classifier (research-heavy)
Wk 12   Hero 3 — Scope Contract          ← needs intent parser (highest risk)
Wk 13   Hero 8 — Decision Replay         ← MCP Apps UI work
                                           [v2.0-beta]
Wk 14   Pillar 4 — HN deliverables       ← README, demo video, comparison
                                           [v2.0 GA + HN launch]
```

At every alpha checkpoint: review competitive landscape (Anthropic native memory? competitor copy?). If material change, replan.

---

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Anthropic ships native Claude Code memory mid-flight | Medium | Existential | Universality wedge (multi-IDE) and decision protection are still ours; alpha cadence keeps us shipping signal. If imminent, accelerate Heroes 1+2 to alpha.1. |
| 14 weeks of building blind | High | Medium | Public alpha builds every 2-3 weeks; alpha tester feedback loop. |
| Solo founder energy at 14 weeks | Medium | High | One-thread focus. Per-hero spec → code → dogfood → ship pattern. Take Sundays. |
| Scope creep within heroes | High | Medium | Each hero capped by its `docs/heroes/NN-name.md` spec; review pre-coding. |
| rootUri/lifecycle-hook unreliability across IDEs | Medium | Medium | Tier-1 are tested; tier-2 falls back to AGENTS.md only. `codevira test-ide` smoke test surfaces problems early. |
| Real users hit a path we missed | Medium | Medium | Alpha tester program; private feedback before HN GA. |
| Hero 3 (Scope Contract) intent parser produces false positives | High | Medium | Ship as opt-in by default (`scope.strict false`). User-driven enable. Iterate on real usage. |
| Hero 9 (Intent Inference) needs LLM call | Medium | Low | Use a small/cheap model (Haiku/local). Allow user to disable. |

---

## Success metric (founder agreed)

**Self-honesty: am I personally using codevira daily on my own projects 60 days post-v2.0 GA?**

If yes → real product, keep building.
If no → wind down or pivot.

Stars and active-user counts are interesting but secondary. Founder daily-use is the ground truth.

---

## Sources (research informing this plan)

- [The 2026 MCP Roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)
- [Claude Code Hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Code Memory docs](https://code.claude.com/docs/en/memory)
- [AGENTS.md (Linux Foundation)](https://agents.md/)
- [MCP Apps SEP-1865](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/)
- [Memory tool comparison](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- [Source-code comparison: Väinämöinen vs MemPalace vs claude-mem](https://dev.to/vainamoinen/vainamoinen-vs-mempalace-vs-claude-mem-a-source-code-level-comparison-of-ai-agent-memory-systems-4bk4)
- [DeployHQ: AI coding config files guide](https://www.deployhq.com/blog/ai-coding-config-files-guide)
- [Symfony AI: MCP instructions field is widely ignored](https://github.com/symfony/ai/issues/1662)
- [MCP Server Best Practices 2026 (CData)](https://www.cdata.com/blog/mcp-server-best-practices-2026)
