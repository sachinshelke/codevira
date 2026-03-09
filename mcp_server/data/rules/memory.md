---
trigger: always_on
---

# AI CODE AGENT — AUTHORITATIVE MEMORY & EXECUTION RULES

## 1. PRIMARY AGENDA: TOTAL RECALL
The agent is the custodian of "Decision Lineage." You MUST capture the full spectrum of the evolution:
- **The Evolution:** What was suggested vs. what was actually built.
- **The "Wrong" Paths:** Explicitly log rejected ideas and failed attempts to prevent "re-discovery" of bad solutions.
- **The "Why":** The underlying logic and trade-offs.
- **The "What":** Precise technical changes and final outcomes.

---

## 2. THE MEMORY QUADRAPOD (MANDATORY)

### A. THE LAW: `.agents/memory.md` (Intent & Constraints)
* **Purpose:** The "Constitution." Permanent architectural rules.
* **Content:** Final decisions, invariants, non-negotiable constraints, and "Never Again" rules.
* **Rule:** This file is LAW. Any change requires explicit user approval and a corresponding session log entry.

### B. THE TRUTH: `.agents/logs/` (What, Why, and Wrong)
* **Purpose:** A factual, append-only audit trail via session log YAML files.
* **Format for EVERY Session Log:**
    - **Date:** ISO date
    - **Context:** What was discussed or requested.
    - **Action:** What was attempted or implemented.
    - **Rationale (The WHY):** Why this specific path was chosen.
    - **The "Wrong" Path:** What didn't work, what was rejected, and WHY (Mistake Log).
    - **Outcome:** Success/Failure + Resulting state.

### C. THE KNOWLEDGE: `docs/FAQ.md` (The Explanation)
* **Purpose:** High-level summary for users/future sessions.
* **Rule:** Must be updated whenever a behavior is finalized or a design choice is justified. Work is INCOMPLETE until the FAQ explains the "Why" to a reader with zero prior context.

### D. THE CONFIGURATION: `config/` or project settings (The Behavior)
* **Purpose:** The "Controls." Defines ALL runtime behavior, limits, and capabilities.
* **Rule:** Code is generic; Configuration is specific. No behavioral change requires code modification. All magic numbers, timeouts, and logic switches MUST live here.

---

## 3. OPERATIONAL RULES

1. **READ BEFORE ACT:** Always review `.agents/roadmap.yaml` and relevant graph nodes first. If missing or conflicting, STOP and ask.
2. **NO SILENT LOSS:** Do not omit "wrong" attempts. If code failed or an idea was bad, log it in the session log to prevent future repetition.
3. **CONFLICT RULE:** If a request conflicts with documented architectural decisions, STOP immediately, cite the rule, and explain the risk. Do not implement without an explicit "Amendment."
4. **NO DRIFT:** Do not add tools, APIs, or patterns unless explicitly requested.
5. **SMALL & REVERSIBLE:** Prefer minimal diffs. Wrap existing logic rather than rewriting.
6. **FAIL FAST:** If a task is ambiguous or unsafe, do not guess. Stop and explain the risk.
7. **CONFIG OVER CODE:** Never hardcode. If it varies, it belongs in the config. Check `config/` before writing logic.

---

## 4. COMPLETION CRITERIA
A task is NOT finished until:
1. **Session Log Updated:** Reflects the discussion, the logic, and the errors.
2. **Memory Updated:** (Only if architectural intent changed).
3. **FAQ Updated:** Explains the final decision and why alternatives were rejected.
4. **Configuration Validated:** New behavior is defined in config, not hardcoded.
5. **Validation:** Code is checked against the original "Intent" in memory.

---

## MENTAL MODEL (NON-NEGOTIABLE)
Reasoning (Conversation) → Intent (Memory) → Behavior (Configuration) → Action (Session Log) → Explanation (FAQ) → Enforcement (Code)
