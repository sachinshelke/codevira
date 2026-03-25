---
trigger: always_on
---

# GIT COMMIT RULES FOR AI CODE AGENTS (AUTHORITATIVE)

## CORE PRINCIPLES

1. AUTHORITY
- NEVER commit code unless the user explicitly issues a commit command
  (e.g., "commit my changes", "git commit").

2. ACCOUNTABILITY
- EVERY commit MUST be backed by corresponding session log entries in Codevira's history.
- If an action, change, or execution is not logged in Codevira → it MUST NOT be committed.

3. DOCUMENTATION IS PART OF THE CHANGE
- A change is INCOMPLETE until code, Graph Rules, Roadmap, FAQ, and docs are aligned.
- Commits represent **system truth**, not just code deltas.

────────────────────────
## 1. PRE-COMMIT GATE (MANDATORY)
────────────────────────

Before generating a commit message, staging files, or committing, the agent MUST verify ALL of the following:

### A. ARCHITECTURAL INTENT SAFETY (CRITICAL)
- The Roadmap (`roadmap.yaml`) and Graph Rules are LAW and MUST NOT be auto-updated.
- If a new architectural decision, constraint, or intent change is discovered:
  - STOP
  - PROPOSE the change explicitly to the user
  - WAIT for approval
- Only user-approved intent changes (via `complete_phase` or `update_node`) may be included in a commit.
- Any approved change MUST be reflected in the final session log entries.

### B. SESSION LOG FINALIZATION
- Codevira's session logs MUST include factual entries for all relevant work:
  - what was discussed
  - what was actually implemented (Evolution)
  - what succeeded or failed (Wrong Paths)
  - what changed from previous behavior
  - underlying logic (The Why)
- If the session log (via `write_session_log()`) is not prepared → STOP. No commit allowed.

### C. FAQ SYNCHRONIZATION (NON-NEGOTIABLE)
- If the commit reflects ANY of the following:
  - a decision
  - a confirmed behavior
  - a workflow change
  - a trade-off or limitation
  - a rejected alternative
- Then the FAQ MUST be updated BEFORE commit.

FAQ updates MUST explain:
- What the decision/change is
- WHY it was made
- Alternatives considered
- Why alternatives were rejected
- Trade-offs and implications

If FAQ is not updated → WORK IS INCOMPLETE → DO NOT COMMIT.

### D. DOCUMENTATION CONSISTENCY
- Update all affected documentation as applicable:
  - README.md
  - CHANGELOG.md
  - CLI / architecture / design docs
- Documentation updates are NOT optional.
- No stale or partial documentation is allowed in a commit.

### E. PROJECT STATE CONSISTENCY
- Ensure any tracked state files are updated and consistent.
- Partial or mismatched system state is forbidden.

If ANY check above fails → STOP and request clarification.

────────────────────────
## 2. COMMIT MESSAGE RULES (STRICT)
────────────────────────

- One-line or generic commit messages are NOT allowed.
- Every commit MUST have a detailed, multi-line message.
- Commit messages are treated as permanent system explanation.

### REQUIRED COMMIT MESSAGE FORMAT

Title:
- Clear, precise summary of the change

Context:
- What problem, discussion, or need triggered this change

What Changed:
- Concrete description of code, behavior, or configuration changes

Why:
- Explicit reasoning behind the change
- Trade-offs accepted

Decisions:
- Which existing decisions this reinforces
- OR which approved decisions were changed

Documentation:
- List of updated docs (memory, session logs, FAQ, README, etc.)

────────────────────────
## 3. FINAL COMMIT RULE
────────────────────────

If ANY of the following is true:
- Memory change not explicitly approved
- Session log missing or incomplete
- FAQ not updated when required
- Docs out of sync
- Commit authority not given

→ DO NOT COMMIT.

Ask. Clarify. Wait.

────────────────────────
## MENTAL MODEL (NON-NEGOTIABLE)
────────────────────────

Session logs record truth
Memory preserves intent
FAQ preserves explanation
Docs preserve understanding
Commits preserve integrity
