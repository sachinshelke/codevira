# Founder dogfood checklist

> **Note for readers other than the founder.** This is an internal QA
> checklist. It began as the dogfood run for v2.0.0rc1 (PyPI, 2026-05-14)
> and is maintained against the current release — run it against whatever
> is in the tree. If you're a user looking to upgrade from v1.x, see
> [MIGRATING.md](MIGRATING.md).

**Goal**: install codevira on your real daily-use machine and use it for **1 week of actual coding** across **at least 2 different AI tools** (e.g. Claude Code + Cursor) on the same project. This validates the universality wedge end-to-end and surfaces real-world residue the automated suite can't find.

**Time commitment**: 30 min setup + your normal dev work for 7 days + 30 min wrap-up. No extra time required during the week — codevira runs in the background.

The QA discipline caught 8 production bugs across the build phase. Real usage will catch what the discipline missed — your job is to surface that residue.

---

## Pre-flight (5 min)

```bash
cd ~/Documents/Projects/LogisticsOS/agent-mcp
git log --oneline -1   # note the commit you are dogfooding

# Confirm clean test baseline on YOUR machine (full suite)
.venv/bin/pytest tests/ -q
# Expected: 0 failed. The pass/skip counts grow with the suite — the
# baseline is that nothing FAILS, not that it matches a number recorded
# here. `make test-unit` is the faster subset (excludes e2e + integration).
```

If anything fails on your machine but passes mine — stop and report. That's a real-world bug we missed (env / OS / Python version delta).

---

## Install on a real test project (10 min)

```bash
# Don't dogfood on this codevira repo. Use a real project of yours.
cd ~/path/to/some-real-project   # any project with .git + pyproject.toml / package.json / etc.

# Install codevira from the head of this working tree
pipx install --force --editable ~/Documents/Projects/LogisticsOS/agent-mcp

# One-prompt setup (Pillar 1.1)
codevira setup
# Should detect every AI tool you have installed and write nudge files
# + lifecycle hooks for each. Look at the output: 5+ tools detected
# means you're real-world, not synthetic.

# Verify (Pillar 1.3)
codevira doctor
# Expect 8-9 ✓ checks, possibly 1-2 ⚠ for things that genuinely don't apply.
# Any ✗ → stop and investigate before continuing.
```

---

## During the week (no extra time — your normal coding)

Just code. The engine's 7 active policies run automatically. **Don't disable anything.** If something blocks you legitimately:

```bash
# Switch any single hero to advisory:
export CODEVIRA_DECISION_LOCK_MODE=warn        # Hero 1
export CODEVIRA_ANTI_REGRESSION_MODE=warn      # Hero 2
export CODEVIRA_BLAST_RADIUS_MODE=warn         # Hero 4

# Or the nuclear kill switch (disables ALL heroes globally):
export CODEVIRA_ENGINE=0
```

**Note when you reach for these.** Each one is a UX failure we should fix.

---

## Daily checkpoints (1 min/day)

End of each day, jot 1-line notes:

```
Day 1: [what hero behavior surprised you, helpfully or otherwise]
Day 2: [...]
```

---

## Trigger scenarios to actually try (do at least 3 of 4)

These are the real-world tests our automated suite can't simulate:

### 1. Cross-tool universality (the WHOLE WEDGE)

Open the project in **Claude Code**, ask the AI:
> "What did I decide about [some-architecture-thing] last week?"

If you have prior decisions logged (record one with `record_decision` MCP tool), the AI should cite them. Then close Claude Code, open the same project in **Cursor** (or Antigravity). Ask the same question.

**Expected**: same answer surfaces. **If different / missing**: the wedge is broken on your real install — file a critical bug.

### 2. Decision lock (Hero 1)

```bash
# Lock a real decision via the MCP tool, e.g.:
# (in any AI tool) "Use the codevira tool to lock a decision saying:
#   'we use bcrypt — see ticket #142' in auth.py with do_not_revert=true"
```

Then ask the AI to "switch to argon2" in `auth.py`. Expected: blocked with the locked reason cited.

### 3. Anti-regression (Hero 2)

Find a recent fix commit in your project. Ask the AI to "improve" the file the fix touched. Hero 2 should warn or block if the AI's diff reverts the fix region.

### 4. Replay (Hero 8)

```bash
codevira replay --query auth --format html --out /tmp/timeline.html
open /tmp/timeline.html
```

Should render a clean HTML timeline of decisions about auth-ish things. Plus check the MCP resource in Claude Desktop: type `@codevira show decisions` (Claude Desktop UI).

---

## End of week wrap-up (30 min)

```bash
# Snapshot the decisions timeline
codevira replay --since 7d > /tmp/replay-week.txt

# Re-run doctor (the crash_log_size check surfaces accumulated crash logs)
codevira doctor
```

Open `/tmp/budget-week.txt` and `/tmp/insights-week.txt`. Assess:

- [ ] Did codevira save tokens? (Look at the budget breakdown.)
- [ ] Did codevira surface decisions you'd forgotten?
- [ ] Did any hero block you incorrectly? (How many times did you reach for `mode=warn` or `mode=off`?)
- [ ] Did the wedge work — did Cursor / Antigravity see the same memory as Claude Code?
- [ ] Any UI / message / wording you'd change?
- [ ] Any feature you missed?

Write up the answers as a 1-page note. **That note is the gate** to recruiting alpha testers.

---

## Decision rule for shipping → alpha-tester batch

**Ship**:
- 0 critical bugs (data loss, crash that requires reinstall, wedge broken)
- ≤ 2 friction issues that can ship as known issues in alpha-tester onboarding
- You'd recommend codevira to someone you respect

**Don't ship yet**:
- Critical bug found
- > 5 friction issues stacked up
- You wouldn't recommend it

If you don't ship: triage → fix → re-dogfood for 2 days → re-evaluate.
