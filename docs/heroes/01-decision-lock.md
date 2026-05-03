# Hero 1 — Active Decision Lock

> "No, you can't undo that — we explicitly decided this six months ago and you don't have the context."

The second policy hero in v2.0. When the AI proposes an Edit/Write to a file that has architectural decisions marked `do_not_revert`, Codevira refuses the edit until the user (not the AI) explicitly unlocks. The point isn't to make the AI's life hard — it's to surface that **the user already thought about this** before the AI silently rewrites it.

Sprint week: **Week 5**. Goal: a policy that's smaller than Hero 4 (no signature detection, no per-language regex), reuses the engine machinery proven in Hero 4, and ships into v2.0-alpha.2.

---

## Problem statement

Architectural decisions decay over time. The reasoning that led to "we use Tailwind, not Bootstrap" or "auth uses bcrypt, not argon2" lives in someone's head and maybe in `decisions.db` — but the AI sees only the current code. When asked to "improve the auth module," the AI happily rewrites bcrypt to argon2 because nothing told it not to.

Codevira already records architectural decisions (`record_decision`) and supports a `do_not_revert` flag on graph nodes (the file the decision is about). What's missing is **enforcement**: the flag is just metadata. Hero 1 turns it into a runtime policy.

---

## User pain (concrete example)

**Without Hero 1:**

```text
[6 months ago]
User: We discussed bcrypt vs argon2 for password hashing. Going with bcrypt
      because of dependency footprint. Lock this decision.
User: $ codevira lock auth.py "bcrypt over argon2 — see issue #142"

[Today]
User: Refactor auth.py to use a more modern hashing library
AI:   *Reads auth.py, sees bcrypt*
      *Rewrites to argon2*
      *Updates imports + dependencies*
      *Done in one tool call*
User: ...wait, why is this argon2 again?
```

**With Hero 1:**

```text
[Today]
AI:   *Tries to Edit auth.py*
      → 🛑 Codevira blocks: "auth.py has 1 locked decision:
         #142: 'bcrypt over argon2 — see issue #142' (locked 2025-11-04)
         To proceed: ask the user. They locked this for a reason."
AI:   "I notice auth.py is marked do_not_revert with a decision about
       bcrypt vs argon2. Should I revisit that decision, or work around
       it somehow?"
User: Oh right — I forgot. Yeah let's keep bcrypt; do the refactor without
      changing the hashing.
```

The win: the user is brought back into the conversation **before** the AI rewrites their decision into oblivion.

---

## Mechanism

### Policy contract

```python
class DecisionLock(Policy):
    name = "decision_lock"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    # Higher priority than Hero 4 — Decision Lock is a HARD lock;
    # blast-radius is a SOFT analysis. Both can fire on the same
    # event; the runner combines verdicts (any block wins).
    priority = 100

    def evaluate(self, event, signals):
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        # Pull locked decisions for this file
        locked = signals.decisions(file=str(target_relative), locked_only=True)
        if not locked:
            return PolicyVerdict.allow()

        return self._make_verdict(event, locked, config)
```

The whole policy is < 120 LOC. Same shape as Hero 4 but using `signals.decisions(locked_only=True)` instead of `signals.impact()`.

### Decision tree

```
PRE_TOOL_USE event arrives
│
├── Not Edit/Write/MultiEdit/NotebookEdit?  → ALLOW
├── No target_file?                         → ALLOW
├── mode = "off" (env override)?            → ALLOW
├── No signals (engine wiring failure)?     → ALLOW
│
├── signals.decisions(file=X, locked_only=True) is empty?  → ALLOW
│
└── At least one locked decision exists for this file
    │
    ├── mode = "warn"?  → WARN with diagnostic listing the decisions
    └── mode = "block"? → BLOCK with diagnostic listing the decisions
```

Notable: **no signature detection**. Hero 1 is a HARD lock at the file level. If you want body edits to a locked file allowed, that's not Hero 1 — that's Hero 7 (Live Style Enforcement) or Hero 9 (Intent Inference) telling Hero 1 "this edit is safe."

The aggressive default is intentional. From the spec: "Hard block by default; explicit unlock required." Better to over-block and have the user soft-override (`CODEVIRA_DECISION_LOCK_MODE=warn`) than to under-block and have the AI silently revert architectural decisions.

### What "locked" means in the data layer

`signals.decisions(locked_only=True)` joins the `decisions` table to the `nodes` table via `file_path`, filtering on `nodes.do_not_revert = 1`. The lock is **per-file**, not per-decision — every decision attached to a locked file inherits the lock.

This matches the existing v1.x schema. Per-decision locking would need a schema change; deferred to v2.1 if real users ask for it.

### Configuration knobs

Same env-var pattern as Hero 4:

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_DECISION_LOCK_MODE` | `block` | `off` / `warn` / `block` |

For alpha.2, no other knobs. The simplicity is intentional: locked = locked. If users want finer-grained controls (per-language locks, scope-based bypasses, etc.) we add them based on real-world feedback.

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for a non-Edit event | < 50 µs |
| `evaluate()` for an Edit event with cold graph | < 20 ms |
| `evaluate()` for an Edit event with warm graph + cached decisions | < 1 ms |

Should be faster than Hero 4 (no signature regex on the diff). The signal layer caches decision queries per-event, so multiple policies asking the same question pay once.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| File not in graph (`signals.graph` is None or no node) | Allow — no node = no `do_not_revert` flag; nothing to enforce. |
| Node exists but `do_not_revert=0` | Allow (unlocked). |
| Node exists with `do_not_revert=1` but no decisions recorded | Allow with a `warn` even in block mode — the user marked the file as locked but there's no rationale to surface, so blocking would just be confusing. (UX choice; see Risks.) |
| Multi-file edit (MultiEdit) | Evaluates the FIRST target file, like Hero 4. v2.1: per-file evaluation. |
| Locked file edit + Hero 4 conditions also met | Both fire. Runner combines verdicts: any `block` wins. The user sees both diagnostic messages — they're additive. |
| `mode=off` set by attacker via env | Out of v2.0-alpha threat model (attacker controls user's env = bigger problem). |
| Decision exists but is for an old file path (rename) | Allow if no decision matches the current path. (Rename detection is a graph-layer concern, not Hero 1's. v2.1 may add it.) |

---

## Demo storyboard (10-second scene)

1. **(0.0s)** User: "Refactor auth.py — modernize the hashing"
2. **(2.0s)** AI proposes Edit on auth.py
3. **(3.0s)** Codevira blocks:
   ```
   🛑 auth.py has 1 locked decision:
     • #142: "bcrypt over argon2 — see issue #142"
       Locked 2025-11-04
   This file is marked do_not_revert. Either:
     1. Ask the user before proceeding.
     2. Override with CODEVIRA_DECISION_LOCK_MODE=warn (warns instead of blocks).
   ```
4. **(6.0s)** AI: "I notice you've marked this file as locked with a decision about bcrypt. Should I revisit that, or refactor without changing the hashing library?"
5. **(8.0s)** User: "Just keep bcrypt; modernize everything else."
6. **(10.0s)** End frame.

Same pattern as Hero 4 demo: the AI surfaces the decision, the user re-engages, the right work happens.

---

## Acceptance test list

8 scenarios that have to pass before Hero 1 ships:

1. **Non-Edit event allowed** — `Read` / `Bash` / `Grep` allowed without checking decisions.
2. **Edit on file not in graph allowed** — fresh project, no decisions. Hero 1 no-ops.
3. **Edit on unlocked file allowed** — file has decisions but `do_not_revert=0`.
4. **Edit on locked file blocked** — `do_not_revert=1` AND decisions exist → block with the decisions in the message.
5. **Edit on locked file with NO decisions yields warn** — `do_not_revert=1` but no decisions recorded; UX choice not to block (no rationale to show).
6. **`mode=warn` produces warn instead of block** — same scenario as 4 but with warn mode.
7. **`mode=off` disables policy entirely** — verdict is allow even on locked file.
8. **Performance: warm-graph + cached signals < 1 ms p95 over 100 trials.**

Tests live in `tests/engine/test_decision_lock.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/decision_lock.py` | The `DecisionLock` policy implementation |
| `tests/engine/test_decision_lock.py` | 8 acceptance tests + adversarial / mutation regression tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `DecisionLock` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export `DecisionLock` from package |

### No change

- No CLI surface (configurable purely via env var for alpha)
- No new signal types (uses existing `signals.decisions`)
- No setup wizard changes (Hero 1 ships through existing hook plumbing from Pillar 1)

---

## QA gate (per cadence + lessons #10/#13)

Hero 1 IS user-facing — its block message reaches AI tools and through them, end users. Apply the full R1-R8 gauntlet plus mutation testing.

The R1-R8 plan for Hero 1:

- **R1 #1 + #7** — code review + security audit (independent agent)
- **R2 #5 + #8** — integration completeness (is `register_default_policies` updated to include Hero 1?) + type safety
- **R3 #15** — mutation testing (each fix has a regression test that fails on revert)
- **R4 #2 + #6** — adversarial vs the fixes + doc drift
- **R5 #3** — cross-module impact (does Hero 1 break Hero 4 or any Pillar 1 code?)
- **R6 #4 + #9** — latency + concurrent stress (multiple policies firing on same event)
- **R7 #17 + #19 + #20** — crash recovery + Unicode (filenames with non-ASCII chars) + FS edge cases
- **R8 #13 + #11** — schema verification (`signals.decisions` shape) + live observation

Hero 1 reuses signals proven in Hero 4 and the engine layer proven in Week 1, so risk is lower. But discipline is the same.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Locked file with no decisions recorded → confusing UX | Medium | Low | Edge case 5: warn instead of block, with a message saying "marked locked but no rationale recorded." |
| Edits to nested files inheriting from a locked parent | Low | Medium | Out of scope for v2.0; per-file granularity. Document. |
| AI memorizes "I just ignore Hero 1 by setting CODEVIRA_DECISION_LOCK_MODE=off" | Low | High | The env override is for the USER, not the AI. AI tools don't typically have shell-env-modification powers in a Claude Code session. If they do, the user has bigger problems. |
| File rename undetected | Medium | Medium | Existing v1.x behavior; not Hero 1's concern. Defer to graph-layer rename detection. |
| Two policies blocking simultaneously (Hero 1 + Hero 4) → confusing message | Medium | Low | Engine combines verdicts; user sees both messages additively. Acceptable. |

---

## Out of scope (deferred)

- **Per-decision locking** (vs per-file): would need schema change. v2.1.
- **Region-based locking** (lock specific lines, not whole file): same. v2.1+.
- **Bypass via decision unlocking flow** (`codevira unlock <file>`): exists in CLI today but not surfaced in the block message yet. Documented in v2.1 alpha announce.
- **Rename-aware locking**: graph layer concern.
- **Lock annotations in code** (`# codevira:lock` markers in source): considered; rejected for alpha because it duplicates the graph-layer signal. Maybe v2.2.

---

## Definition of done

- [ ] `DecisionLock` policy registered and enabled by default.
- [ ] All 8 acceptance tests pass.
- [ ] R1-R8 QA gauntlet clean (any P1+ findings fixed before merge).
- [ ] Performance bench p95 in `tests/engine/test_decision_lock.py`.
- [ ] `docs/v2-execution-log.md` Week-5 entry written.
- [ ] Both Hero 1 + Hero 4 firing on the same locked-and-high-radius file produces a coherent verdict.
- [ ] alpha.2 plan updated to include Hero 1 in the heroes-shipping list.
