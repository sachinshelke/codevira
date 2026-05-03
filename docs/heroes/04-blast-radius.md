# Hero 4 — Blast-Radius Veto

> The first real **policy hero** in v2.0. Engine plumbing (Week 1) and Pillar 1 hooks (Week 3) make this trivially small — Hero 4 is just a `Policy` plugin that calls `signals.impact(file)` in `PreToolUse`. The value isn't in the lines of code; it's in catching the silent migration disaster.

Sprint week: **Week 4**. Goal: ship a policy that blocks (or warns) when an AI tries to rename a function or change a public signature without realizing N other files depend on it.

End-of-week deliverable: this hero shipped + alpha.1 cut (Engine + Pillar 1 + Hero 4).

---

## Problem statement

LLMs love to "improve" code. They'll happily rewrite `def auth_token(user_id)` to `def auth_token(user)` because it reads better — without realizing 12 callers in 8 files pass `user_id` and now break.

Codevira already builds a structural call graph (`indexer.sqlite_graph`). The graph knows who calls what. We just need to consult it before the AI commits the change, and refuse the change if the blast radius exceeds a threshold AND the change touches a public signature.

---

## User pain (concrete example)

**Without Hero 4:**

```text
User: "Rename get_user_id to get_user across the codebase"
AI:   *Edits one file*
      *Refactors the function and signature*
      *Doesn't update the 12 callers because it didn't look*
User: *Pushes; CI fails on Tuesday morning; spends 30 min unwinding*
```

**With Hero 4:**

```text
User: "Rename get_user_id to get_user across the codebase"
AI:   *Calls Edit*
      → 🛑 Codevira blocks: "auth.py has 12 callers across 8 files. This
         change modifies the function signature on line 42. Either:
         (a) propose a deprecation+migration plan covering all 12 callers, or
         (b) confirm the rename is safe (no callers actually break) — pass
             `--unsafe` or set CODEVIRA_BLAST_RADIUS=warn."
AI:   *Reads the 12 caller files*
      *Proposes a multi-file MultiEdit covering all of them*
      *Or reports back to user: "this is bigger than you might think — here's the migration"*
```

The win: the AI surfaces hidden cost BEFORE the edit, instead of after.

---

## Mechanism

### Policy contract

```python
class BlastRadiusVeto(Policy):
    name = "blast_radius_veto"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    priority = 50  # mid-priority; runs after Decision Lock (priority=100)
                   # but before less-critical advisory policies

    def evaluate(self, event: HookEvent) -> PolicyVerdict:
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        impact = signals.impact(event.target_file)
        if not impact or not impact.get("found"):
            return PolicyVerdict.allow()  # graph not built; skip silently

        radius = impact.get("blast_radius", 0)
        if radius < BLOCK_THRESHOLD:
            return PolicyVerdict.allow()

        if not _change_touches_signature(event.proposed_diff):
            return PolicyVerdict.allow()

        return PolicyVerdict.block(
            message=f"...{radius} downstream files depend on {target}...",
            metadata={"blast_radius": radius, ...},
        )
```

The whole policy is < 100 LOC of real logic.

### Decision tree

```
PRE_TOOL_USE event arrives
│
├── Not Edit/Write/MultiEdit/NotebookEdit?  → ALLOW
├── No target_file?                         → ALLOW
│
├── signals.impact(target_file) returns nothing?  → ALLOW (graph
│                                                    not built;
│                                                    don't false-block)
│
├── blast_radius < block_threshold?         → ALLOW (small radius —
│                                                    not worth the
│                                                    user friction)
│
├── proposed_diff missing OR doesn't touch
│   a public signature?                     → ALLOW (body-only change;
│                                                    no caller breaks)
│
└── radius ≥ block_threshold AND signature changes
    │
    ├── Mode = "warn"?  → WARN with diagnostic
    └── Mode = "block"? → BLOCK with diagnostic
```

### How "touches a signature" is detected

Parse the AI's proposed diff (the `before` and `after` blocks). Extract every line matching a signature regex from both blocks. If the set of signature lines differs between before and after, the change touches a signature.

**v2.0-alpha languages supported:**
- Python: `^\s*(?:async\s+)?def\s+\w+` and `^\s*class\s+\w+`
- JavaScript / TypeScript: `^\s*(?:export\s+)?(?:async\s+)?function\s+\w+` and `^\s*(?:export\s+)?class\s+\w+`
- Go: `^\s*func\s+(?:\([^)]+\)\s+)?\w+`
- Rust: `^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+`
- Java/C#: `^\s*(?:public|protected|private)\s+(?:static\s+)?[\w<>,\s]+\s+\w+\s*\(` (best-effort)

If the language isn't recognized, we fall back to "any contiguous identifier near a `(`" — coarse but safer than a false negative.

**v2.1+ languages (deferred):** Ruby, PHP, Swift, Kotlin, Scala, OCaml.

### Configuration knobs

All accessed via `signals.config_for("blast_radius_veto")`:

| Setting | Default | Purpose |
|---|---|---|
| `mode` | `"block"` | One of `"off"`, `"warn"`, `"block"`. `"warn"` lets the edit through but emits a system message. |
| `block_threshold` | `5` | Minimum blast_radius to trigger the policy. Below this, allow without checking signatures. |
| `warn_threshold` | `3` | (When `mode="warn"` only) the radius at which warnings start. |
| `bypass_with_decision_id` | unset | Optional decision ID — if a decision with this ID exists and is locked, allow this exact target file without checking. Lets users mark "yes, I know auth.py has high impact, that's fine." |

Project-level overrides via `~/.codevira/projects/<slug>/config.yaml`:
```yaml
policies:
  blast_radius_veto:
    mode: warn
    block_threshold: 8
```

For v2.0-alpha, configuration is via env vars:
- `CODEVIRA_BLAST_RADIUS_MODE` = `off|warn|block`
- `CODEVIRA_BLAST_RADIUS_THRESHOLD` = integer

YAML config integration deferred to v2.1 (need a config-loader hero).

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for a non-Edit event | < 50 µs (pure conditional, no signal access) |
| `evaluate()` for an Edit event with cold graph | < 20 ms (graph load via signal cache) |
| `evaluate()` for an Edit event with warm graph | < 2 ms (cached impact lookup) |
| `_change_touches_signature` regex pass on 1 KB diff | < 1 ms |

The expensive part is `signals.impact()` (graph SQL query). The signal layer caches per-event, so subsequent policies asking the same question pay nothing.

If the graph is on a slow filesystem (NFS, network drive), p95 may exceed 50 ms. We accept this — falling back to "allow" on impact failures means worst-case the policy no-ops, never false-blocks.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Graph not built (`signals.graph` is None) | Allow — policy silently no-ops. Better to ship correct behavior on un-indexed projects than false-block. |
| `get_impact` returns `{"found": False}` (file not in graph) | Allow — same reason. |
| `proposed_diff` is None (Write replaces file) | If `tool_input["content"]` is available and the file is being created from scratch, allow (no callers yet). If the file exists and content is being replaced wholesale, treat the entire old content as `before` and new as `after`. |
| Multi-file edit (MultiEdit) | Hero 4 evaluates the FIRST target file. v2.1: per-file evaluation with verdict combining. |
| Edit only adds a new function (signature appears in `after` but not `before`) | Allow — adding a new function doesn't break callers. |
| Edit deletes a function (signature in `before` but not `after`) | Block — same harm shape as renaming (callers break). |
| Edit reorders parameters but keeps the same names | Block — semantically a signature change even if parameter names overlap. |
| Edit changes only docstring/whitespace inside the function body | Allow — body-only change, no signature impact. |
| Comment-only changes inside the def line (e.g. adding a type hint comment) | Allow — signature regex captures the def line, comparison shows identical sig lines. |
| Edit on a generated/autogenerated file (`*.pb.py`, etc.) | Allow — out of scope; the source-of-truth is elsewhere. (Future enhancement: detect via regex on path or file header.) |
| `proposed_diff` contains binary content | Allow + warn (binary diff isn't analyzable; fail-open is safer than fail-closed). |
| Threshold misconfigured (negative, non-integer) | Defensive clamp to default `5`. Log warning. |
| AI bypasses by setting CODEVIRA_BLAST_RADIUS_MODE=off in env | Out of v2.0-alpha threat model. The user controls their env. v2.1 may add a "this env var is being honored — confirm?" check. |

---

## Demo storyboard (10-second scene for HN/README)

1. **(0.0s)** Open Claude Code in a real project with `auth.py` (12 callers).
2. **(2.0s)** User: "Rename `auth_token(user_id)` to `auth_token(user)` for clarity."
3. **(4.0s)** Claude proposes the Edit.
4. **(5.0s)** Codevira blocks with: `🛑 auth.py has 12 callers across 8 files. This Edit modifies the function signature on line 42. Use the deprecation+migration pattern — propose a MultiEdit covering all callers, or pass --unsafe to override.`
5. **(7.0s)** Claude reads the 8 caller files via Grep.
6. **(9.0s)** Claude proposes a MultiEdit covering all 9 files.
7. **(10.0s)** End frame: "Codevira saved 30 minutes of CI debugging."

---

## Acceptance test list

10 scenarios that have to pass before Hero 4 ships:

1. **Non-Edit event allowed** — `Read` / `Bash` / `Glob` calls are passed through unchanged.
2. **Edit on file not in graph allowed** — fresh project with no graph.db; policy no-ops.
3. **Edit with low blast radius allowed** — `target_file` has 1 caller; below threshold; no check.
4. **Edit with high blast radius + body-only change allowed** — 12 callers but the diff doesn't touch any signature line.
5. **Edit with high blast radius + signature change blocked** — 12 callers, diff modifies the `def` line; verdict is `block` with a clear message including the radius and target.
6. **Edit with high blast radius + new function added allowed** — adding a `def new_thing(...)` to the file is fine, even if other functions in the file have many callers.
7. **Edit deletes a function with high callers blocked** — removing `def auth_token` should block when callers exist.
8. **`mode=warn` produces warn verdict instead of block** — same scenario as test 5 but with mode=warn yields `warn` not `block`.
9. **`mode=off` disables the policy entirely** — verdict is `allow` even when scenario 5's conditions hold.
10. **Performance: cold-graph evaluation < 50 ms p95 over 100 trials.**

Tests live in `tests/engine/test_blast_radius.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/__init__.py` | Package marker for the policy plugins directory |
| `mcp_server/engine/policies/blast_radius.py` | The `BlastRadiusVeto` policy implementation |
| `mcp_server/engine/policies/_signature_detect.py` | Pure-function helper: parse a diff, extract signature lines, compare. Tested independently. |
| `tests/engine/test_blast_radius.py` | 10 acceptance tests + adversarial / mutation regression tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Auto-register `BlastRadiusVeto()` at import time (analogous to how `demo_policy.maybe_register()` works, but unconditional). |

### No change

- `cli.py` — no new commands. Hero 4 is invisible at the CLI surface; users see it via Claude Code's hook output.
- `setup_wizard.py` — no change. Hero 4 ships through existing hook plumbing.

---

## QA gate (per cadence + Week-3 lesson)

Per the cadence matrix line for Week-4 [Hero 4 Blast-Radius]: ~2 hr Tier-1 per-hero. Plus alpha.1 → ~4 hr alpha QA.

But per **Week-3 R8 lesson #10**: "Specs override matrix minimums when they conflict. For any user-facing surface, default to running R1-R8 (or until two consecutive rounds find nothing material)."

Hero 4 IS user-facing — it's the first thing AI tools (and therefore users) will see Codevira do *visibly* (a block message). Apply the full 8-round discipline.

The R1-R8 gauntlet for Hero 4:

- **R1 #1 + #7** — code review + security audit (independent agent)
- **R2 #5 + #8** — integration completeness + type safety
- **R3 #15** — mutation testing (each fix has a regression test that fails on revert)
- **R4 #2 + #6** — adversarial vs the fixes + doc drift
- **R5 #3** — cross-module impact (does Hero 4 break Pillar 1 / engine / Hero-2/6 plumbing?)
- **R6 #4 + #9** — latency + concurrent stress
- **R7 #17 + #19 + #20** — crash recovery + Unicode + FS edge cases
- **R8 #13 + #11** — external schema (signature regexes vs real codebases) + live observation (real Edit JSON from Claude Code)

R8 is especially relevant for this hero — the signature regex is an EXTERNAL contract (with Python/JS/TS/Go/Rust language syntax). If the regex gets out of step with how those languages actually look, Hero 4 false-blocks or false-allows.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Signature regex misses a real signature change → false allow | Medium | Medium | Test on real codebases (Week-4 R8); accept some false negatives — better than false positives |
| Regex matches signature in a string literal / comment → false block | Medium | High | Strip strings + comments before regex; if too costly, just allow when uncertain |
| Graph stale (file modified since last index) → wrong impact count | Medium | Low | Codevira already runs a watcher; staleness is rare. Even when wrong, we err on the safer side (showing an outdated count is fine; the user can re-index) |
| Threshold too low → noisy false blocks → user disables policy | High | Medium | Start at 5 (safe-ish); collect feedback in alpha; tune in v2.1 |
| Multi-language detection drifts as languages evolve | Low | Low | Out-of-tree config — users can override regex via config. v2.1 will add tree-sitter–based detection for accuracy. |

---

## Out of scope (deferred)

- **Tree-sitter–based signature parsing.** Regex is fine for v2.0-alpha; tree-sitter integration is bigger work (~Hero 7+ priority).
- **Multi-file MultiEdit per-target evaluation.** v2.0-alpha evaluates the FIRST target only.
- **YAML config integration.** Env vars only for now; per-project YAML config when a config-loader hero ships (v2.1).
- **Per-language threshold overrides.** All languages share the same threshold today. Defer.
- **Suggestion mode** ("here's a deprecation+migration plan"). The block message can hint at the pattern but doesn't generate the plan. Hero 9 (Proactive Intent Inference) will eventually do this.

---

## Definition of done

- [ ] `BlastRadiusVeto` policy registered and shipping by default.
- [ ] All 10 acceptance tests pass.
- [ ] R1-R8 QA gauntlet clean (any P1+ findings fixed before merge).
- [ ] Performance bench p95 in tests/engine/test_blast_radius.py.
- [ ] Founder dogfooded for ≥48 hours on at least one real project (alpha.1 dogfood gate).
- [ ] `docs/v2-execution-log.md` Week-4 entry written.
- [ ] alpha.1 tag pushed.
