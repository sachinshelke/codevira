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
→ Graph / Roadmap preserves intent
→ Session Logs preserve truth
→ FAQ preserves explanation
→ Git freezes verified state

────────────────────────────────────────
1. AUTOMATED MEMORY & KNOWLEDGE SYSTEM (CODEVIRA)
────────────────────────────────────────

### A. ARCHITECTURAL MEMORY (INTENT)
Tools: `update_node()`, `add_phase()`, `complete_phase()`, `update_next_action()`
Storage: `.codevira/graph/` and `.codevira/roadmap.yaml`

Purpose:
- Preserve architectural intent and direction via Graph Rules.
- Capture final decisions as `key_decisions` in Roadmap phases.
- Record "never do this again" invariants as `rules` on specific file nodes.

Rules:
- Graph Rules and the Roadmap represent the "Project Law."
- Any architectural change:
  - MUST be explicitly approved by the user.
  - MUST be persisted via `update_node` (new rules) or `complete_phase` (decisions).
  - MUST be logically linked in the session log.

---

### B. EXECUTION MEMORY (TRUTH)
Tool: `write_session_log()`
Storage: `.codevira/logs/` (YAML)

Purpose:
- Preserve factual reality of what happened in each session.
- Prevent "token re-discovery" in future sessions.

Must record:
- The Evolution: What was suggested vs. what was actually built.
- The "Wrong" Paths: Rejected ideas and failed attempts.
- The "Why": Underlying logic and trade-offs.
- The "What": Precise technical changes.

Rules:
- Every session MUST end with a `write_session_log` call.
- If it is not logged in the codevira history → it does not exist.

---

### C. FAQ / USER KNOWLEDGE
File: `docs/FAQ.md`

Purpose:
- Human-readable explanation of complex behaviors and non-obvious trade-offs.

Update FAQ whenever:
- A behavior or workflow is finalized.
- A technical limitation is accepted.
- A previous decision is reversed.

ENFORCEMENT:
- If behavior changed but FAQ is not updated → WORK IS INCOMPLETE.
- Agent MUST STOP.
- No further coding, execution, or commits allowed.

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
