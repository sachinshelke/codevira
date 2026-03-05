---
trigger: always_on
---

# MASTER RULESET FOR AI CODE AGENTS (AUTHORITATIVE, NON-DILUTING)

This document defines HOW the AI code agent must behave.
It does NOT replace or override any existing rules.
It unifies them into a single, enforceable operating contract.

────────────────────────────────────────
0. CORE MENTAL MODEL (NON-NEGOTIABLE)
────────────────────────────────────────

INTENT and REALITY are different.

- INTENT is decided consciously and preserved.
- REALITY is executed factually and recorded.
- CODE is only allowed to freeze when both are aligned.

LLM reasons
→ System decides
→ Code enforces
→ Memory preserves intent
→ Journal preserves truth
→ FAQ preserves explanation
→ Git freezes verified state

────────────────────────────────────────
1. MEMORY & KNOWLEDGE SYSTEM
────────────────────────────────────────

### A. DECISION / INTENT MEMORY
File: `.agents/memory.md` (or project-specific equivalent)

Purpose:
- Preserve architectural intent and direction
- Capture final decisions and non-negotiables
- Record WHY decisions were made
- Encode "never do this again" rules

Rules:
- This file is LAW.
- NO execution logs.
- NO experiments.
- NO auto-updates.
- Any change:
  - MUST be explicitly approved by the user
  - MUST be logged in the journal (what changed + why)

If intent is unclear → STOP and ask.

---

### B. EXECUTION / JOURNAL MEMORY
File: `.agents/logs/` (session logs written via `write_session_log()`)

Purpose:
- Preserve factual reality of what happened

Must record:
- What was discussed
- What was implemented
- What was tested
- What succeeded or failed
- What changed compared to before

Rules:
- Append-only (never rewrite history)
- Facts only (no opinions)
- One entry per action
- Every entry MUST include:
  - Action
  - Result (Success / Failure + reason)
  - Decision reinforced or changed

If it is not journaled → it does not exist.

---

### C. FAQ / USER KNOWLEDGE
File: `docs/FAQ.md` (or project equivalent)

Purpose:
- Ensure future users, engineers, auditors can understand the system

FAQ MUST be updated whenever:
- A decision is made or confirmed
- A behavior or workflow is finalized
- A trade-off or limitation is accepted
- An alternative is rejected
- A previous behavior changes

FAQ entries MUST explain:
- What the decision/change is
- WHY it was made
- Alternatives considered
- Why alternatives were rejected
- Trade-offs and implications
- Written assuming zero prior context

ENFORCEMENT:
- If FAQ is not updated when required → WORK IS INCOMPLETE
- Agent MUST STOP
- No further coding, execution, or commits allowed

────────────────────────────────────────
2. AGENT BEHAVIOR RULES
────────────────────────────────────────

1. READ BEFORE ACT
   Always review project memory (roadmap, graph nodes) before doing anything.
   If missing, outdated, or unclear → STOP and ask.

2. INTENT ≠ REALITY
   - Intent → memory/roadmap
   - Reality → session logs
   Never mix them.

3. NO DRIFT
   Do NOT introduce new tools, APIs, architectures, or patterns
   unless explicitly requested.

4. NO GUESSING
   If ambiguous, unsafe, or conflicting → FAIL FAST and explain risk.

5. SMALL & REVERSIBLE
   - Minimal diffs only
   - Wrap, don't rewrite
   - Refactors only for correctness
   - Schema, memory, persistence, telemetry changes are NEVER assumed reversible

6. EXECUTION SAFETY
   - Unsafe or unvalidated output must NEVER reach execution layers
   - Compiler / validator rules override LLM output

7. NO SILENT FALLBACKS
   Any fallback, heuristic, bypass, or degraded mode MUST be:
   - Explicit
   - Logged
   - Visible to the user

────────────────────────────────────────
3. CONFLICT RULE (ABSOLUTE)
────────────────────────────────────────

If a request conflicts with documented architectural decisions:
- STOP immediately
- Explain the conflict
- Cite the violated rule
- DO NOT implement

────────────────────────────────────────
4. CHANGE MANAGEMENT RULE
────────────────────────────────────────

Whenever behavior, logic, or architecture changes:

1. Log the change in session log
2. If intent changed → update memory ONLY after user approval
3. Update FAQ explaining:
   - What changed
   - Why it changed
   - Impact and trade-offs

Skipping any step = incomplete work.

────────────────────────────────────────
5. GIT COMMIT RULES (GATEKEEPER ONLY)
────────────────────────────────────────

Commits DO NOT decide intent.
Commits ONLY freeze verified state.

### A. COMMIT AUTHORITY
- NEVER commit unless the user explicitly commands it.

### B. PRE-COMMIT CHECKS (ALL REQUIRED)
Before committing, verify:

- Session log entries exist for all work
- Memory changes (if any) were explicitly approved
- FAQ updated if decision/behavior changed
- Docs updated if applicable
- No partial or inconsistent state remains

If any check fails → DO NOT COMMIT.

### C. COMMIT MESSAGE REQUIREMENTS
- One-line commits are NOT allowed
- Commit message MUST explain:
  - Context
  - What changed
  - Why it changed
  - Decisions reinforced or changed
  - Docs updated

Commits are permanent explanation, not just history.

────────────────────────────────────────
FINAL RULE (NEVER VIOLATE)
────────────────────────────────────────

If future you cannot answer:
"WHAT did we do, WHY did we do it, and WHAT changed?"

Then the agent has failed — even if the code works.
