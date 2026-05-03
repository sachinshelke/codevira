# QA Angle 06 — Documentation Drift

**Subagent type:** Explore
**Time-box:** 20 min
**Catches:** Spec violations + stale docs that mislead future implementers

## Prompt

```
Compare {spec_file} (the SPEC) against the implementation in {code_files}.

For EACH explicit claim in the spec, verify the code matches. Be picky.

Format each finding as:
  **[DRIFT]** Spec says: "{exact quote}" — Code does: "{actual behavior}"
  Severity: spec-violation / acceptable-deferral / cosmetic
  Suggested action: change code OR update spec — and which?

Walk through these spec sections:
1. Architecture diagram — does the file layout match?
2. Public API — every claimed function/class signature/field?
3. Behavior contracts — every "MUST", "SHALL", "always", "never"?
4. Performance budgets — measured vs. claimed?
5. Edge cases listed in spec — each handled in code?
6. Error handling — spec describes which exceptions are caught/raised?
7. Configuration knobs — every documented option actually wired?
8. Examples — every code example in spec runs as documented?

For each drift:
- Is it spec-violation (code is wrong)?
- Acceptable deferral (spec called it future work and code matches present)?
- Cosmetic (paraphrase difference)?

Cap at 1500 words. Prioritize spec-violations; mention cosmetic only
as a list at the end.
```

## Trigger

Whenever the spec OR the code changes meaningfully. Catches the case
where one drifts from the other and nobody notices.

## Expected output

Active sprints often surface 2–5 drifts; mature features should be
~0. R3's R3-E doc-drift check found `_MAX_CHANGE_BYTES` = 100 KB code
vs. 10 MB spec — exactly this angle's signal.
