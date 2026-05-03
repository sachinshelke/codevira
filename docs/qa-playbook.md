# Codevira QA Playbook — 22 Testing Angles

> Battle-tested catalog of QA angles. Originated from Week-1 engine sprint
> where 5 progressive rounds of QA caught 15 real bugs. Each round used a
> different angle. This playbook codifies the angles so every hero (1–10)
> goes through the same gauntlet before alpha.

## Why this exists

In Week 1 (engine sprint), the implementer's own tests passed 73-of-73 yet
**3 P0 bugs** shipped through. Different angles caught different bugs:

| Round | Angle | Bugs caught |
|---|---|---|
| R1 | Code review + spec drift | 3 P0 (correctness) |
| R2 | Adversarial against fixes + cross-module | 2 P1 + 3 P2 |
| R3 | Full-stack reality + integration completeness | 2 P1 + 1 P2 |
| R4 | Security audit | 3 HIGH (path traversal, AI-controlled inputs) |
| R5 | External schema (vs Claude Code's actual docs) | 4 protocol mismatches |

Lesson: **the implementer cannot fully QA their own work.** Independent
angles surface independent bugs. The earlier we run them, the cheaper.

## How to use this playbook

Per-hero workflow integrates QA at two points:

1. **Before code (Step 0)**: consult target system's *actual* contract.
   Skipping this is what made R5 necessary — Heroes 5/9 would have
   shipped silently broken because the implementer's mental model of
   Claude Code's hook schema was wrong.
2. **Before alpha ship**: run automatable angles. Block ship on any
   HIGH/P0 finding; document any MEDIUM/P1 as backlog.

When a hero ships, copy this checklist into its `docs/v2-execution-log.md`
entry. Mark each angle done/skipped/deferred with reasoning.

---

## The 22 Angles

### Tier 1 — Automatable via subagent (8 angles, ~30 min each)

These can be invoked directly by feeding the listed prompt to a fresh
agent (Explore subagent works well for code-reading; Plan for design
review).

| # | Angle | Subagent prompt template | Catches |
|---|---|---|---|
| 1 | **Code review** | "Review `<files>` for bugs, code smells, dead code, type errors, error-handling gaps. Cite file:line. Severity P0/P1/P2." | Implementation bugs |
| 2 | **Adversarial fix review** | "After my fixes to `<file>`, try to break each fix. What inputs make it misbehave? What edge cases does the fix miss?" | Fix-code bugs |
| 3 | **Cross-module impact** | "Does adding `<new module>` break or degrade existing behavior in `<existing modules>`? Walk through 3 user workflows pre/post." | v1.8.1 regressions |
| 6 | **Doc drift** | "Compare `<spec.md>` to actual code. List every claim where spec and code disagree. Severity by impact." | Stale docs / spec violations |
| 7 | **Security audit** | "Threat model: malicious AI sends crafted input. Check path traversal, JSON/SQL/shell injection, credential leaks, DoS, privilege escalation. Cite file:line + concrete attack." | CVE-class issues |
| 12 | **LLM red-team** | "You are an attacker. Find ways to exploit/break `<engine code>`. Be creative — timing attacks, error-message leaks, cache collisions, etc." | Novel attack vectors |
| 13 | **Multi-IDE schema** | "For `<IDE>`, fetch the official MCP/hooks docs. Compare every field name, every JSON shape, every required field to what we emit. Cite divergence." | Per-IDE protocol mismatches |
| 22 | **Competitor benchmark** | "Review claude-mem (or `<competitor>`) implementation. What do they do differently? What patterns should we adopt? What corners did we cut they didn't?" | Best-practice gaps |

### Tier 2 — Partially automatable (script + agent, 8 angles, ~30–60 min each)

Need a script to generate signal + an agent to interpret. Mostly
mechanical.

| # | Angle | How to run | Catches |
|---|---|---|---|
| 4 | **Full-stack reality / latency** | Time hook round-trips through binary; measure dispatch in-process. p50/p95/p99. | Real-world performance gaps |
| 5 | **Integration completeness** | grep for adapter calls — are they actually invoked? Static + behavioral test. | Missing wiring (R3 #1: MCP dispatch never wired) |
| 8 | **Type safety** | `mypy` + Python 3.9/3.10/3.11/3.12 import test. | Version-specific syntax errors |
| 9 | **Concurrent stress** | 10–100 threads × 100 events; measure errors + memory growth. | Races, deadlocks |
| 14 | **Upgrade simulation** | Set up v1.8.0 state (data + IDE configs); run v2.0 against it. | Migration breakage |
| 15 | **Mutation testing** | Run `mutmut` on engine modules; verify tests fail when code is mutated. | Coverage holes |
| 17 | **Crash recovery** | kill -9 mid-write; reopen; check corruption. | Persistence integrity |
| 21 | **Resource exhaustion** | 100 MB SQLite, 10K rows, 50 concurrent policies, 1000 sessions. | Scaling failures |

### Tier 3 — Manual / requires human judgment (6 angles, varies)

Cannot be fully automated; requires real environment or human eyes.

| # | Angle | How to run | Catches |
|---|---|---|---|
| 10 | **External schema (live)** | Fetch official docs; do field-by-field comparison. | Schema drift (R5: inject path silently broken) |
| 11 | **Live Claude Code observation** | Register hooks in real `~/.claude/settings.json`; run actual session; observe what JSON Claude Code sends at runtime. | Docs-vs-runtime divergence |
| 16 | **Long-haul soak test** | Run for hours/days under continuous synthetic load. | Memory leaks, FD exhaustion |
| 18 | **CLI UX (new user)** | Pretend to be a brand-new user. Read README. Try to install + use. Time to first AI value. | Confusing UX |
| 19 | **i18n / Unicode** | Paths with emojis, RTL text, non-Latin scripts. | Encoding bugs |
| 20 | **Filesystem edge cases** | APFS case-insensitivity, NFS, symlinks, read-only mounts. | OS-specific failures |

---

## Cadence — when to run what (THE schedule)

Two extremes to avoid:

- **All 22 every week** = 30-60% of dev time on QA. Solo founder
  burnout in ~6 weeks. Most angles don't apply to most heroes
  (i18n testing on Hero 4 catches nothing — it doesn't touch paths).
  Same code + same angle → same findings = no new signal.

- **All 22 at the end** = bugs compound across heroes. Hero 4's bug
  interacts with Hero 7's bug; debug time multiplies. R5 caught
  Heroes 5/9 silently broken in Week 1; discovering that in Week 13
  would mean re-doing alpha demos and writing apologies.

**The right answer is staged by phase + matrix by angle relevance.**

### The QA matrix

| Angle | Per hero (3-5d sprint) | Per alpha (every ~3 wk) | Pre-beta (Wk 13) | Pre-GA (Wk 14) |
|---|:---:|:---:|:---:|:---:|
| 01 Code review | ✅ | ✅ | ✅ | ✅ |
| 02 Adversarial fix | ✅ if hero had fixes | ✅ | ✅ | ✅ |
| 03 Cross-module | only if new module | ✅ | ✅ | ✅ |
| 04 Latency | — | ✅ | ✅ | ✅ |
| 05 Integration completeness | only if new wiring | ✅ | ✅ | ✅ |
| 06 Doc drift | ✅ | ✅ | ✅ | ✅ |
| 07 Security audit | only if new input handler | — | ✅ | ✅ |
| 08 Type safety | ✅ | ✅ | ✅ | ✅ |
| 09 Concurrent stress | — | ✅ | ✅ | ✅ |
| 10 External schema (live) | only if new IDE | — | — | ✅ |
| 11 Live Claude Code | — | ✅ on each alpha | ✅ | ✅ |
| 12 LLM red-team | — | — | ✅ | ✅ |
| 13 Multi-IDE schema | only if new IDE | — | ✅ | ✅ |
| 14 Upgrade simulation | — | — | ✅ | ✅ |
| 15 Mutation testing | — | — | — | ✅ |
| 16 Soak test | — | — | — | ✅ |
| 17 Crash recovery | — | — | ✅ | ✅ |
| 18 CLI UX (new user) | — | — | ✅ | ✅ |
| 19 i18n / Unicode | — | — | — | ✅ |
| 20 FS edge cases | — | — | — | ✅ |
| 21 Resource exhaustion | — | ✅ if scale-touching | ✅ | ✅ |
| 22 Competitor benchmark | — | — | ✅ | ✅ |

### Time budget per phase

| Phase | Cadence | Per-run time |
|---|---|---|
| Per hero (during sprint) | 3-5 relevant Tier 1 angles | **~2 hrs** (≤25% of a 1-day QA buffer in a 3-5 day sprint) |
| Per alpha (weeks 4, 7, 10) | Tier 1 full + Tier 2 selective | **~4 hrs** |
| Pre-beta (week 13) | Tier 1 + 2 + selected Tier 3 | **~1 day** |
| Pre-GA (week 14) | All 22 angles | **~2 days** |

**Total v2.0 QA spend: ~60 hours over 14 weeks** ≈ 1.5 weeks of work
spread across the release. ~10-15% of dev time — the standard real-
product ratio.

### v2.0 concrete schedule

```
Week 1 [Engine sprint]            5-round QA done (15 bugs fixed) — exceptional case
Week 2 [Engine continuation]      ~1 hr post-implementation Tier 1
Week 3 [Pillar 1 UX install]      ~2 hr Tier 1 + Tier 2 (#5 integration, #8 type)
Week 4 [Hero 4 Blast-Radius]      ~2 hr Tier 1; alpha.1 → ~4 hr alpha QA
Week 5 [Hero 1 Decision Lock]     ~2 hr Tier 1
Week 6 [Hero 5 Cross-Session]     ~2 hr + #11 live Claude Code observe
Week 7 [Hero 6 Token Budget]      ~2 hr; alpha.2 → ~4 hr alpha QA
Week 8 [Hero 2 Anti-Regression]   ~2 hr Tier 1 + #14 upgrade simulation
Week 9 [Hero 7 Live Style]        ~2 hr
Week 10 [Hero 10 AI Promotion]    ~2 hr; alpha.3 → ~4 hr alpha QA
Week 11 [Hero 9 Intent Inference] ~2 hr Tier 1 + #07 security (LLM input)
Week 12 [Hero 3 Scope Contract]   ~2 hr + #07 security (intent parser)
Week 13 [Hero 8 Decision Replay]  ~2 hr; pre-beta → ~1 day Tier 1+2+3 selective
Week 14 [Pillar 4 + GA]           ~2 days pre-GA: all 22 angles
```

### Why this matrix is right

**Per hero gets the cheapest, most-relevant subset.** Code review (1)
and doc drift (6) catch the implementer's blind spots; type safety (8)
catches Python-version regressions; the rest are conditional on what
the hero actually touches. This is ~25% of a hero's 1-day QA buffer
— affordable.

**Per alpha catches integration-level issues** that per-hero can't
see — concurrent stress (9) only matters when N policies are
registered together; latency (4) only matters under realistic load.
Alpha checkpoints batch these.

**Pre-beta adds the manual / time-intensive angles** that are too
costly to run weekly: live IDE observation (11), security re-audit
(7), upgrade simulation (14), mutation testing-equivalent coverage
checks. These shouldn't run on every hero but MUST run before users
see the build.

**Pre-GA is the safety net.** All 22 angles, no exceptions. If
something slipped through earlier phases, last chance to catch it.
This is also when you run angle 22 (competitor benchmark) — by GA
you have a working product to compare honestly.

### Per-hero log

Each hero's section in `docs/v2-execution-log.md` lists:
- Angles run (with link to subagent transcript or test output)
- Bugs caught + severity
- Items deferred to backlog with reasoning

If an angle in the matrix was skipped for that hero's sprint, log
**why** (e.g., "Hero 5 didn't touch Unicode paths so #19 not run").
Forces honest tradeoff documentation.

### Stopping within an angle

Even at a phase where the matrix says "run angle X," stop iterating
when:
1. 2 consecutive runs find nothing material (angle is mature for this code)
2. Bug class has shifted from "broken correctness" to "narrow edge case"
3. Findings would only land in v2.1+ (not blocking the current ship)

**Don't stop because "this is a lot of QA."** Each angle paid for
itself in Week 1 (15 bugs in 5 rounds). Cost of shipping a P0 to
production exceeds cost of running another angle.

---

## The agent-team mental model

Think of the 22 angles as 22 specialist team members. Each has a fresh
perspective. Real teams hire reviewers from different backgrounds for a
reason — the same logic applies here. Don't ask the implementer (or the
implementer's tests) to find bugs they have systematic blind spots for.

When you "send the work to QA," you're invoking the team:

```
For each hero:
  for angle in TIER_1 + (TIER_2 if hero affects perf/state) + (TIER_3 if pre-GA):
    invoke(angle.prompt, scope=hero.files)
    triage(findings)
    fix HIGH/P0
    document MEDIUM/P1 in backlog
    re-run angle to verify
```

This is the discipline that turned Week 1 from "implementer says it
works" → "5 independent angles confirm it works AND each found bugs the
others missed."

---

## Stopping criterion

Stop running fresh angles when:
1. **Two consecutive rounds find nothing material** (current QA discipline
   is mature for this hero).
2. **Discovery rate decays** (R1 found 3 P0 → R5 found 4 protocol mismatches;
   trend is bug class shifting from "broken correctness" to "narrow edge
   case" — you're past the danger zone).
3. **Time budget exceeded** (a hero gets ≤ 1 day of QA per its 3–5 day
   sprint; if Tier 1 + 2 take longer, the hero's design is too big).

**Don't stop because "this is a lot of QA."** Each round paid for itself
in Week 1. The cost of shipping a P0 to production exceeds the cost of
running another QA round.

---

## Lessons applied to the per-hero workflow

After Week 1's 5-round QA:

1. **Step 0 is non-negotiable.** Consult target system's actual docs
   before writing wiring code. Skipping this caused R5.
2. **The implementer's tests are systematically blind.** They test
   against the implementer's mental model, which is the same model
   the bugs live in. Independent angles are required.
3. **Different angles catch different classes.** Code review catches
   correctness; adversarial catches fix-bugs; security catches CVEs;
   schema-check catches protocol drift. None of these substitute for
   the others.
4. **Hands-on testing through the actual binary is worth more than
   in-process unit tests.** R3 and R5 used hands-on; both found bugs
   unit tests passed.
5. **Performance numbers must be measured, not trusted.** R3 found
   the 50ms p95 spec target was wrong (real number is ~67ms). R4
   found the fast path saves 15.6×. Both required hands-on measurement.

After Week 2's 4-round QA:

6. **The matrix minimum is the floor, not the ceiling.** The "per-hero
   ~1 hr Tier-1" line in the matrix is a *minimum*. When a sprint adds
   new persistence + new concurrent paths (as Week 2 did with JSONL
   persist + shared SQLite connection), default to running the
   Week-1-style R1-R4 progression. Week 2's R1 minimum found 1 P1; R3
   added concurrency stress and found another P1 (silent transaction
   race) plus a P2 (crash recovery). R4 added independent fresh-eyes
   and found 2 more P2s. Spending ~3 hours instead of ~1 caught 3
   bugs the minimum would have missed.
7. **A "fresh-eyes" round (R4 framing) finds different bugs than a
   "code review" round (R1 framing).** Even on the same code, an
   architecture-focused review with no bug-list bias surfaces design
   traps (`_connect()` foot-gun, missing WAL+timeout) that a
   correctness-focused review skips. Add R4 to per-hero defaults
   when the sprint touches concurrency primitives or process-level
   contracts.
8. **Verify findings before fixing.** Independent agents return false
   positives — Week 2 R1 had 2 of 4. A 30-second benchmark or a
   5-line line-citation check is enough to triage; never apply a fix
   on an unverified finding.
9. **Mutation testing is the only way to verify a regression test
   actually regresses.** Week 2 R5 caught a regression test that
   passed coverage AND passed on healthy code — but with the bug
   re-introduced (cap reverted to `readlines()`), the test STILL
   passed. Output-correctness assertions are not enough when the
   fix is about *resource bounds*; assert on the resource (peak
   memory, byte count read, syscalls issued) directly. Run a
   simple mutation pass — programmatically revert each fix, run
   the regression test, confirm it fails — before declaring a fix
   "covered." A test that doesn't fail when the bug returns is
   technical debt with a green checkmark on it.

After Week 3's 8-round QA:

10. **Initial "spec-mandated full sweep" is not optional for
    user-facing surfaces.** Week 3 first shipped with 3 rounds
    (R1+R2+R3) — matching the matrix minimum but ignoring the
    Pillar-1 spec's explicit "Tier 1, full sweep (all 8 angles)"
    requirement. R4-R8 caught 4 more findings, including a P2
    Claude Code matcher-missing issue worth seconds-per-session
    of waste. **Specs override matrix minimums when they conflict.**
    For any user-facing surface, default to running R1-R8 (or
    until two consecutive rounds find nothing material).
11. **External-schema verification belongs in a fresh-agent round
    of its own (#13), not folded into code review (#1).** Week 3's
    R1 agent didn't independently verify Claude Code / Cursor /
    Windsurf / Codex / Antigravity / Copilot schemas against
    current docs — it just code-reviewed our implementation. R8
    (separate agent, current-docs-aware) caught two real schema
    items neither R1-R7 nor the implementer (me) had spotted.
    Treat external-schema verification as its own round whenever
    a sprint touches more than one IDE.
12. **Live observation through the actual binary (#11) catches
    different bugs than in-process unit tests.** The hook scripts'
    fast-path behavior was tested by unit tests in Week 1 R3,
    but the realistic Claude Code stdin schema (with all the
    documented fields like `session_id`, `transcript_path`, etc.)
    was only verified via subprocess in Week-3 R8. Prefer
    subprocess + realistic JSON over mocks when verifying
    external integration points.
