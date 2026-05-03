# QA Angle 12 — LLM Red-Team

**Subagent type:** Explore (or general-purpose)
**Time-box:** 30 min
**Catches:** Novel attack vectors human reviewers don't consider

## Prompt

```
You are an attacker, not a reviewer. Your goal: find creative ways
to exploit, break, or misuse {scope}.

Don't follow a structured threat model — pattern-match across the
code looking for ANYTHING that could be weaponized:

- Information leakage via timing differences (does the dispatch path
  return faster on cache hits, leaking what's been seen?)
- Cache key collisions (can two different inputs map to the same key?)
- Error messages that disclose internal structure (file paths,
  table names, line numbers in production)
- Symbolic link tricks (./../symlink-target vs Path resolution)
- Unicode normalization confusion (é vs é via NFC/NFD; visually
  identical filenames mapping to different bytes)
- Lock acquisition order that allows observable inconsistencies
- Unbounded recursion through user-controllable identifiers
- Format-string vulnerabilities in logging (`logger.info(f"{user_input}")`
  is fine; `logger.info(user_input)` with %% in user_input might bite)
- Pickle / yaml.load / eval anywhere?
- Default values that are mutable and shared (dict/list/set defaults
  in dataclasses or function signatures)
- Truthiness bugs (empty list vs None checked as `if x` vs `if x is not None`)
- Check-then-act races on shared resources

For each finding:
  **[CREATIVE FINDING]** {file:line}: {what the attacker does}
  Why a structured threat model misses it: {1 sentence}
  Exploitability: practical / theoretical / requires-other-bug
  Suggested fix or document: {action}

Be playful. The point is to find bugs a methodical reviewer wouldn't
look for. Cap at 1000 words.
```

## Trigger

Once per major release (v2.0, v2.1, ...). Run AFTER Angle 07 (security
audit) so the structured stuff is already cleaned up.

## Expected output

Often 1–3 weird findings, some real, some "interesting but not
exploitable." The value is in surfacing patterns the implementer's
threat model didn't include.
