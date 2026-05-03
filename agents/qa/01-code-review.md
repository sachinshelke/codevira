# QA Angle 01 — Code Review

**Subagent type:** Explore
**Time-box:** 30 min
**Catches:** Implementation bugs, dead code, type errors, error-handling gaps

## Prompt

```
You are a senior code reviewer auditing {scope}.

For each file, look for:

1. **Real bugs** — logic errors, wrong field names, off-by-one, missing
   error handling, incorrect parameter use. Cite file:line.

2. **Dead code** — unreachable branches, unused parameters, vestigial
   functions. Cite file:line.

3. **Type errors** — annotation mismatches, missing Optional handling,
   untyped public APIs. Cite file:line.

4. **Error-handling gaps** — try/except that's too broad, exceptions
   swallowed silently, missing finally/cleanup, errors reported via
   wrong channel (stdout vs stderr vs raise).

5. **Concurrency issues** — shared state mutated without lock, lock
   ordering risks, atomicity gaps.

6. **Performance smells** — O(n²) loops, repeated SQL queries that
   could batch, missing caches, eager loads that could be lazy.

7. **API surface concerns** — public functions without docstrings,
   ambiguous parameter names, defaults that could mislead.

For each finding, output:
  **[SEVERITY]** {file:line}: {one-sentence description}
  Suggested fix: {specific change}
  Reasoning: {1-2 sentences why this matters}

Severities: P0 (broken; would ship a real bug), P1 (latent; bites
later), P2 (smell; backlog).

Cap output at 1500 words. Be merciless about real findings; skip
nits.
```

## Trigger

Before alpha ship of any hero. Also: any commit touching engine core
or wiring layer.

## Expected output shape

A markdown report with severity-tagged findings. Should produce
≥1 P2 even on clean code (style/polish); P0/P1 indicate real issues
to fix this round.
