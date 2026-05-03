# QA Angle 03 — Cross-Module Impact

**Subagent type:** Explore
**Time-box:** 30 min
**Catches:** Regressions in OTHER subsystems caused by the new code

## Prompt

```
You are reviewing whether {scope} (the NEW code) breaks or degrades
any EXISTING functionality. Pre-{scope} behavior is the baseline.

Walk through 3 user workflows mentally, comparing pre vs post:

1. **First-time install** —
   pipx install codevira → codevira register → codevira configure
   - What's the diff in observable behavior?
   - Does any new module load eagerly that wasn't there before?
   - Any new failure modes added to first-call latency?

2. **Existing user upgrade** —
   v1.8.0 user runs pipx upgrade codevira → opens their IDE
   - Does existing data migrate cleanly?
   - Does existing IDE config still work?
   - Any breaking change to public APIs?

3. **Daily-use** —
   AI agent calls existing tools (get_node, get_impact, search_decisions)
   - Has the dispatch path slowed?
   - Has the response shape changed?
   - Any new error path that didn't exist before?

For each potential regression:
  **[IMPACT]** {file:line}: {what changes}
  Severity: {how badly does it bite the user}
  Mitigation: {fix or document}

Specifically check:
- Module-level imports — is anything new imported eagerly?
- Module-level state — does new code mutate global state existing
  modules depend on?
- Shared resources — files, sockets, locks, caches now contended?
- Existing tests — do any pass only by accident now?
- Crash/error paths — does new code change what gets logged where?

Cap at 1000 words.
```

## Trigger

Any architectural addition (new module, new daemon, new dispatch path).
Not needed for pure bug fixes within an existing module.

## Expected output

Often clean (0 findings) when new code is well-isolated. When it finds
something, it's typically a hot-path slowdown or a global-state hazard.
