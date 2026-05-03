# QA Angle 07 — Security Audit

**Subagent type:** Explore
**Time-box:** 45 min
**Catches:** Path traversal, injection, credential leaks, DoS, privilege issues

## Prompt

```
You are a security auditor reviewing {scope}.

Threat model: a compromised AI agent OR a malicious tool input. The
codevira engine processes JSON from Claude Code's hooks AND tool args
from MCP call_tool. Either input source is UNTRUSTED.

For each of these classes, find concrete exploitable scenarios:

1. **Path traversal** —
   AI sends tool_input['file_path'] = '../../../../etc/passwd'.
   Does any code end up reading/writing outside the project boundary?
   Check Path(x).resolve() without containment, os.path.join with user
   input, glob patterns expanded over user input.

2. **JSON / SQL / shell injection** —
   Are any user-controlled strings concatenated into SQL, JSON output,
   shell commands, regex patterns, or eval/exec? Even "safe" SQL
   parameterization can be defeated by string-built CHECK clauses.

3. **Trust boundary** —
   What inputs does the engine treat as authoritative that come from
   untrusted sources? E.g., if `cwd` from Claude Code is taken as
   project_root without re-validation, an attacker who controls the
   hook environment controls the engine's project context.

4. **Credentials / secrets leakage** —
   Does any logged output (stdout, stderr, crash_logger) include raw
   tool_input, exception messages, file paths with project structure?
   Could an exception's repr include a token the AI was passing through?

5. **Resource exhaustion / DoS** —
   Are any size limits missing? Can N kB of input cause N MB of
   processing? Are caches bounded? Are SQL `limit` parameters clamped?
   Is there a timeout on long-running policies?

6. **Privilege issues** —
   Hook scripts run with the user's privileges. Can a malicious hook
   environment escalate? Can policy code (e.g., signal lookups) be
   tricked into opening untrusted SQLite databases?

7. **Race conditions** —
   Time-of-check / time-of-use. Cache poisoning. Lock-ordering risks
   that allow inconsistent state to be observed by a policy.

For each finding:
  **[SEVERITY: HIGH/MEDIUM/LOW]** {file:line}
  Attack scenario: {step-by-step what attacker does}
  What bad happens: {data leak / unauthorized action / DoS}
  Suggested fix: {specific code change}

Be specific. "Theoretically exploitable" is NOT a finding. "Input X
through path Y causes effect Z" IS a finding. Cap 1500 words.
```

## Trigger

Any new external-input handler. Engine's wiring layer was the trigger
in Week 1 R4 — found path traversal + AI-controlled project_root +
SQL DoS (3 HIGH).

## Expected output

Typically 2–4 HIGHs on first audit of any new input handler.
Subsequent audits on hardened code drop to 0–1 MEDIUMs.
