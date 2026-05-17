---
name: development-discipline
description: |
  Use this skill before ANY code-writing tool call (Edit, Write, NotebookEdit)
  on this project. Triggers on phrases like "implement", "fix bug", "add
  feature", "refactor", "create function", "modify file", or whenever the
  user asks for a code change. Forces a 4-step CONTEXT → PURPOSE → REASON
  → CODE sequence with explicit checks at each step. Refuses to write
  code until all four are answered. Also covers reuse-first, single-source-
  of-truth, blast-radius-aware, minimal-diff, and test-as-evidence sub-rules.
---

# Development discipline — non-skippable sequence

When this skill triggers, you MUST complete steps 1–4 IN ORDER before
calling any Edit, Write, or NotebookEdit tool. Skipping steps produces
the kind of silent-failure bugs that shipped to PyPI as v2.0.0.

## The 4-step sequence

### Step 1 — CONTEXT (read before write)

Before any change, output:

- "I read `<file>:<lines>` and the relevant code does X."
- If editing: quote the exact lines being changed and what they do today.
- If creating new: list the 3 most similar existing files in the
  codebase that you considered.

If you haven't read the file you're about to edit, you cannot edit it.
**Read tool first, Edit tool second.** No exceptions.

### Step 2 — PURPOSE (rephrase the goal)

Output, before any code:

- "The user is asking for: <one sentence in your own words>."
- "Specifically, that means: <list 1-3 concrete outcomes>."
- "It does NOT mean: <list adjacent things you might wander into>."

The "does NOT mean" line is the discipline anchor. Forces explicit
scope. Without it, you'll opportunistically "improve" unrelated code
and ship a 47-file PR for a 3-line bug.

### Step 3 — REASON (rule out simpler paths)

Output:

- "Existing code that already does this or something close: <list>."
- "Approach chosen: <X>."
- "Approaches considered and rejected: <Y because Z>."
- "Files this will touch (target list): <N files, name them>."

If the touch list has >3 files, justify each one. Refactoring
opportunistically beyond the touch list is **out of scope**. If a new
file or dependency is needed, declare it here before you write it.

### Step 4 — CODE (minimal diff)

Only NOW may you call Edit/Write/NotebookEdit. Constraints:

- Only modify files in the Step 3 touch list. Adding files mid-task
  requires re-doing Step 3.
- No "while I'm here" cleanups in unrelated files.
- No reformatting that isn't part of the change.
- No new dependencies without explicit approval.

After the change:

- Output "Test that proves the fix: `<pytest cmd or test name>`."
- If no test exists, write one before declaring done.

## Sub-rules (apply throughout all 4 steps)

### Reuse-first

Before writing a new function, helper, or file:

1. Search the codebase first: `grep`, `Glob`, or codevira's
   `search_codebase` / `query_graph` MCP tools.
2. If an existing function does ~80% of what you need, refactor it
   or extend it — don't create a parallel implementation.
3. State explicitly: "Searched for `<term>`; found `<file>:<line>`
   which does Y. Either I'll reuse / extend / replace it because Z."

This rule prevents the configure-vs-index file-matcher duplication
bug (Bug A) where two parallel implementations diverged and produced
0-chunks silently.

### Single-source-of-truth

When you notice yourself writing logic that's similar to logic
elsewhere — STOP. Don't duplicate. Refactor the original to be
shared first, then call it from both places.

Pattern detection: if you're about to copy/paste even 5 lines,
or write a "this is similar to the X function but slightly
different" function, the discipline says refactor first.

### Blast-radius-aware

Before editing any file in the codevira repo:

1. Call `mcp__codevira__get_impact(file_path)` to see who calls/
   depends on this file.
2. If get_impact returns N>0 dependent files, list them and decide
   for each: "Does my change break this caller? Yes/no/unknown."
3. For "unknown", read the caller to find out.

This rule catches API-shape breaks before they ship. Skip it and
you'll learn about the break from a downstream user.

### Minimal-diff

- Don't refactor unrelated code while fixing a bug.
- Don't add features beyond the literal request.
- Don't "improve" things you weren't asked to improve.
- A bug fix should be the smallest diff that fixes the bug, plus a
  test that proves it.

### Test-as-evidence

After every fix:

1. Either write a new test that fails before the fix and passes
   after, OR
2. Identify an existing test that exercises the bug and demonstrate
   it now passes.

Declaring a fix "done" without a test is hand-waving. The release
gauntlet (G2) refuses to merge a PR that doesn't keep
`tests/e2e/test_first_contact.py` green.

### Dogfood codevira on codevira

When working IN the codevira repo specifically:

- Use `mcp__codevira__get_impact(file)` before editing — not grep.
- Use `mcp__codevira__search_codebase(q)` to find similar code —
  not Glob alone.
- Use `mcp__codevira__query_graph` to understand call relationships.
- Every new MCP tool follows existing patterns: summary by default,
  `full=true` for complete data, `fix_command` on errors.

If you won't dogfood codevira on codevira, you shouldn't expect
anyone else to use it.

## Anti-patterns this skill refuses

You may NEVER:

- Write code without reading the target file first.
- Create a new utility when grep / get_impact finds one that does
  ~80% of what you need.
- Make a "quick fix" that touches 10 files (always the wrong fix).
- Refactor something you "noticed was ugly" while doing the asked
  task.
- Add features beyond the literal request.
- Declare done without a test or runtime evidence.
- Skip Step 1 because "I remember this file from earlier in the
  conversation." Memory drifts. Re-read.

## Why this exists

v2.0.0 shipped with 23 silent-failure bugs (A–O) because the
pre-v2.1 workflow was: see request → write code → claim done. No
CONTEXT step, no PURPOSE confirmation, no REASON justification, no
test as evidence. This skill makes those four steps mandatory and
verifiable.

The release-readiness, open-source-quality, and epistemic-honesty
skills layer on top of this one. Without development-discipline,
the others are reinforcement without a foundation.
