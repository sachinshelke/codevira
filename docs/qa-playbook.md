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

## Workflow integration — what runs when

### Step 0 (before any code): External-system schema check
**Always.** Run angles 10 + 13 BEFORE writing code that integrates with
that external system. R5 proved synthetic tests can't catch protocol
mismatches.

### After implementation, before tests: Code review (Tier 1)
Angles 1, 2, 6 — quick subagent invocations. ~30 min total.

### After tests pass, before alpha ship: Full QA pass
Run all Tier 1 + Tier 2 angles relevant to the hero. Block ship on any
HIGH/P0. Document MEDIUM/P1 as backlog.

### Before beta / GA: Tier 3 angles
Live observation (11), CLI UX (18), filesystem variety (20). Time-boxed
to ~1 day each.

### Per hero, log results
Each hero's section in `docs/v2-execution-log.md` lists:
- Angles run (with link to subagent transcript or test output)
- Bugs caught + severity
- Items deferred to backlog with reasoning

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
