# Founder dogfood checklist — v2.0-alpha.2

Goal: install codevira on your real daily-use machine and use it for **48 hours of actual Claude Code work** to validate the alpha is genuinely production-grade. This is the gate before we go to alpha testers.

The QA discipline caught 18+ bugs across 8 weeks of development. Real usage will catch what the discipline missed — your job is to surface that residue.

---

## Pre-flight (5 min)

```bash
# Verify the tag you'll be running
cd ~/Documents/Projects/LogisticsOS/agent-mcp
git fetch --tags
git checkout v2.0-alpha.2
git log --oneline -1   # confirm: should be v2.0-alpha.2 commit

# Run the test suite once to confirm a clean baseline on YOUR machine
.venv/bin/pytest tests/engine/ tests/test_paths.py tests/test_setup_wizard.py -q
# Expected: 391/391 passed
```

If anything fails on your machine but passes mine: stop and report. That's a real-world bug we missed.

---

## Install on the real machine (10 min)

```bash
# Use a separate test project — DON'T dogfood on this codevira repo itself
mkdir ~/dogfood-test
cd ~/dogfood-test
git init
echo "[project]\nname = 'dogfood'" > pyproject.toml

# Install codevira from the alpha.2 tag
pipx install --force --editable ~/Documents/Projects/LogisticsOS/agent-mcp

# Run setup
codevira setup
# Expected output:
# - 🔍 Detected: Claude Code, Cursor, Windsurf, Antigravity (whatever you have)
# - 📋 Plan: 13-15 steps depending on detected IDEs
# - Proceed? [Y/n] y
# - ✓ Done in <5s
# - "Restart Claude Code to pick up hooks"

# Verify the artifacts landed
ls -la ~/.claude/hooks/codevira-*.sh    # should be 5 hooks, executable
cat ~/dogfood-test/CLAUDE.md             # should have <!-- codevira:start --> markers
cat ~/.claude/settings.json | jq .hooks  # should have hook entries
```

If any artifact is missing or malformed: that's a Pillar 1 bug. Report.

---

## 48-hour dogfood scenarios

### Scenario 1: First Claude Code session (10 min)

1. Open Claude Code in `~/dogfood-test`.
2. Ask: "What tools do I have available?"
3. Verify Claude lists `get_session_context`, `get_impact`, `search_decisions`, etc.
4. Ask: "Call `get_session_context` and show me what came back."
5. Read the response. Does it look like a useful session brief?

**Pass criteria:** Claude can call codevira tools and the responses are coherent. **Fail signal:** tool errors, "no graph found" errors, or empty responses where you'd expect data.

---

### Scenario 2: Trigger Hero 4 (Blast-Radius Veto) (15 min)

1. In `~/dogfood-test`, build a small graph manually OR run `codevira init` to bootstrap one (depending on what's wired).
2. Create a file `auth.py`:
   ```python
   def auth_token(user_id):
       return user_id
   ```
3. Create 3 caller files that import `auth_token`.
4. Re-index: `codevira index`
5. In Claude Code, ask: "Rename `auth_token(user_id)` to `auth_token(user)` in auth.py."
6. **Expect:** Hero 4 blocks with a message like:
   ```
   🛑 Blast-radius veto on auth.py: 3 downstream file(s) depend on this code...
   ```

**Pass criteria:** Block fires with the diagnostic. **Fail signal:** edit goes through silently OR codevira doesn't get invoked at all.

---

### Scenario 3: Trigger Hero 1 (Decision Lock) (10 min)

1. In Claude Code: "Record a decision that auth.py uses bcrypt and lock it: `do_not_revert=true`."
2. Confirm via `codevira` SQL or whatever surface lets you set `do_not_revert=1` on the auth.py node.
3. Then ask Claude: "Refactor auth.py to use argon2 instead of bcrypt."
4. **Expect:** Hero 1 blocks with the locked decision text:
   ```
   🔒 Decision-lock veto on auth.py: this file is marked do_not_revert
   with 1 locked decision(s)...
   ```

**Pass criteria:** Block surfaces the actual decision text. **Fail signal:** Hero 1 silently allows (this would mean Bug 1 or Bug 2 came back somehow).

---

### Scenario 4: Trigger Hero 5 (Cross-Session Consistency) (10 min)

1. In `~/dogfood-test`, run codevira's CLI to record a decision: "Tailwind, not Bootstrap — bundle size matters" on `styles/`.
2. Open Claude Code. New session.
3. Ask: "Add a styled Get Started button to the homepage hero."
4. **Expect:** Claude's first response should reference the Tailwind decision (codevira injected it via UserPromptSubmit hook). Look for phrases like:
   - "I notice you've decided on Tailwind..."
   - "Per the existing decision about bundle size..."
   - Or at minimum: the AI uses Tailwind classes, not Bootstrap.

**Pass criteria:** AI reflects the prior decision in its response. **Fail signal:** AI proposes Bootstrap unaware.

---

### Scenario 5: Hero 6 (Token Budget) (5 min)

1. After several sessions, run:
   ```bash
   codevira budget
   ```
2. **Expect:** Output showing the most recent session's injected/used totals + top wasted sources.
3. Run:
   ```bash
   codevira budget history --last 5
   ```
4. **Expect:** A table with the last 5 sessions.

**Pass criteria:** Output is parseable, numbers are non-zero (you've been using codevira), top wasted sources make sense. **Fail signal:** "no sessions recorded" (Stop hook isn't firing) or zero tokens (TokenMeter not instrumenting).

---

### Scenario 6: Hero 2 (Anti-Regression) (15 min)

1. In `~/dogfood-test`, do a small bug-fix exercise:
   ```bash
   git commit -am "fix: race condition in token cache — added lock"
   ```
2. Make a synthetic file that the fix touched.
3. Run `codevira fix-noted --scan-git` (the manual scan helper from Week 2).
4. Verify `codevira` records the fix:
   ```bash
   sqlite3 ~/.codevira/projects/<key>/graph/fixes.db "SELECT * FROM fixes;"
   ```
5. In Claude Code, ask: "Simplify the token cache — remove the locking, it's hot-path overhead."
6. **Expect:** Hero 2 blocks with:
   ```
   🛑 Anti-regression veto on token_cache.py: this edit appears to revert
   a previously-fixed bug...
   ```

**Pass criteria:** Block fires referencing the original fix commit. **Fail signal:** edit proceeds silently (the heuristic missed the keyword overlap).

---

## What to log during dogfood

For 48 hours, every time codevira fires (block, warn, inject, or noticeable absence), jot a one-line note:

```text
2026-05-04 14:30  Hero 4 blocked Edit on api.py     → correctly identified blast radius (8 callers)
2026-05-04 14:45  Hero 5 injected — surfaced FastAPI decision   → AI changed course
2026-05-04 15:10  No hero fired on a Read     → expected
2026-05-04 16:00  Hero 1 blocked Edit on db_schema.py    → false positive — file was unlocked
2026-05-04 17:30  codevira budget after session   → 12,400 injected, 6,200 used (50%)
                                                     get_node = top wasted source
```

After 48 hours, count:
- **Total fires:** how many policy actions across the period
- **Confirmed-correct fires:** policy did the right thing
- **False positives:** policy fired when it shouldn't have
- **False negatives:** policy SHOULD have fired but didn't
- **Bugs:** anything else that was wrong (UI, performance, crashes)

---

## Failure modes to watch for

These are the bug-shapes our QA caught — if you see new instances, they're real:

1. **Silent fail-open:** policy is registered + meant to fire but doesn't. Easy to miss if you don't try to trigger it.
2. **Schema drift:** SQL column names diverge from what the policy queries.
3. **Wiring miss:** policy registered but engine doesn't pass the right kwargs.
4. **Dead field:** flag declared but not enforced.
5. **Unicode / locale issues:** Japanese filenames, RTL text, emoji prompts.
6. **Path edge cases:** symlinks, case-insensitive FS (APFS default), deep nesting.
7. **Performance regressions:** any policy taking >50ms p95 in real use.

---

## After 48 hours

```bash
# Run the test suite again to confirm no degradation
.venv/bin/pytest tests/engine/ tests/test_paths.py tests/test_setup_wizard.py -q

# Run codevira doctor (if/when implemented) for a status report
codevira doctor 2>/dev/null || codevira status

# Capture token spend over the dogfood window
codevira budget history --last 50
```

Submit findings as:

1. A markdown summary of fires + outcomes
2. Specific bug reports for each false positive / false negative / crash
3. Performance numbers if anything felt slow
4. Anything UI / message-text / general-feel feedback

**If 48 hours pass with zero new bugs surfaced, the alpha is ready for ≥3 alpha testers.**

If even one major bug surfaces during dogfood: fix on `main`, tag `v2.0-alpha.3`, restart the dogfood clock.

---

## Honest expectation

Across 8 weeks of QA + 5 retrospective rounds + 2 cross-cutting integration audits, we caught 18+ bugs. The discipline is mature. But:

- **Real usage finds bugs the discipline missed.** That's why this gate exists.
- **The first 4 hours are the most likely to surface install/setup issues.** Watch carefully.
- **The first session through Claude Code is the most likely to surface wiring/schema issues.** Same.
- **48 hours of actual coding work is the only way to surface UX/false-positive-rate issues.** No QA round simulates this.

If the dogfood goes clean: alpha.2 was worth shipping, and we move to alpha testers. If it doesn't: we learn what the playbook still misses, codify the lesson (likely #19+), fix the bug, and ship alpha.3.
