# Hero 2 — Anti-Regression Memory

> "We fixed this six weeks ago — please don't put the bug back."

The fifth policy hero. When the AI's proposed Edit looks like it reverts a previously-fixed bug (per the project's git history of `fix:` commits + manual `codevira fix-noted` flags), Hero 2 blocks the edit and surfaces the original fix.

Sprint week: **Week 8**. Smaller than Hero 4: most of the heavy lifting is in `indexer/fix_history.py:is_revert()` (Week 2) and `signals.fixes(file)` (Week 1). Hero 2 is the policy that calls them.

---

## Problem statement

Bug fixes decay in the AI's awareness. Six weeks ago you fixed a race condition by adding a `with self._lock:` guard. Today the AI is "simplifying" the same function and removes the lock again — your bug is back. The fix is in git history, but the AI didn't see it.

Codevira already has the data:
- `indexer/fix_history.py:scan_git_log()` walks `git log` for `fix:`/`bug:`/`hotfix:`/`fixes #N` patterns and records each touched file as a fix region.
- `is_revert(proposed_change, fix)` heuristically detects whether the AI's diff moves the file back toward the pre-fix state.
- `signals.fixes(file_path)` returns the recorded fixes for a file.

Hero 2 is the policy that wires these into a `PreToolUse` block.

---

## User pain (concrete example)

**Without Hero 2:**

```text
[6 weeks ago]
$ git commit -m "fix: race in token cache when multi-thread reads"
[adds `with self._lock:` to TokenCache.get]

[Today]
User: "Simplify TokenCache to remove the locking — it's hot-path overhead."
AI:   *Reads token_cache.py, sees the lock*
      *Removes `with self._lock:` per the user's request*
      *Tests still pass (race only manifests under load)*
[1 week later, production race fires; another all-hands incident]
```

**With Hero 2:**

```text
User: "Simplify TokenCache to remove the locking..."
AI:   *Tries to Edit token_cache.py*
      → 🛑 Anti-regression veto: this Edit appears to revert a known fix.
        Past fix:
          • #commit-abc123 (2026-04-01): "fix: race in token cache when
            multi-thread reads"
            (touched lines 42-58 of token_cache.py)
        Your proposed change removes the synchronization the fix added.
        To proceed:
          1. Confirm with the user that the race condition is no longer
             relevant (e.g. cache is now single-threaded by construction).
          2. Set CODEVIRA_ANTI_REGRESSION_MODE=warn to override.
AI:   "I want to simplify TokenCache, but codevira flags this as
       reverting a known fix from 6 weeks ago. Should I proceed?"
User: "Oh — right. Keep the lock. Refactor around it instead."
```

The win: **the AI surfaces the historical context the user forgot**, before the regression lands.

---

## Mechanism

### Policy contract

```python
class AntiRegression(Policy):
    name = "anti_regression"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    priority = 80  # between Decision Lock (100) and Blast-Radius (50)

    def evaluate(self, event, signals):
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()
        if signals is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        fixes = signals.fixes(event.target_file)
        if not fixes:
            return PolicyVerdict.allow()  # no fix history → no risk

        diff = event.proposed_diff
        if diff is None:
            return PolicyVerdict.allow()  # full Write — Hero 4 handles that

        # Check each fix; if any look like a revert, block
        from indexer.fix_history import is_revert
        reverting = [f for f in fixes if is_revert(diff, f)]
        if not reverting:
            return PolicyVerdict.allow()

        return self._make_verdict(event, config, reverting)
```

The policy is < 150 LOC. All the heuristic complexity lives in `is_revert`.

### Decision tree

```
PRE_TOOL_USE event arrives
│
├── Not Edit/Write/MultiEdit?           → ALLOW
├── No target_file?                     → ALLOW
├── signals=None?                       → ALLOW
├── mode = "off"?                       → ALLOW
│
├── signals.fixes(target_file) empty?   → ALLOW (no history to check)
├── proposed_diff is None?              → ALLOW (full Write — Hero 4's job)
│
├── No fix matches is_revert()?         → ALLOW
│
└── At least one fix matches is_revert()
    │
    ├── mode = "warn"?  → WARN with the fix(es)
    └── mode = "block"? → BLOCK with the fix(es)
```

### What "revert" means here

Conservatively: a proposed change that REMOVES content from the post-fix region OR matches the pre-fix content again. The heuristic is in `is_revert`:

- **Unified diff format**: hunk header overlaps the fix's line range AND a deletion line is present.
- **Claude Code Edit format**: keyword overlap test — buggy keywords from the fix's description appear MORE in the `after` block than the `before` block (suggesting we're moving back toward the bug state).

False-positive rate is acceptable. Hero 2 is **high-recall** by design — better to over-warn than under-warn. The user can override with `mode=warn` per session if a project has noisy fix history.

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_ANTI_REGRESSION_MODE` | `block` | `off` / `warn` / `block` |

That's it. Threshold-style knobs (e.g. "only fixes from last 6 months") are deferred to v2.1 — the simpler model lets us learn the false-positive rate first.

### Companion: `codevira fix-noted` CLI (already exists, audited here)

`codevira fix-noted` (added in Week 2 plumbing) lets users manually flag a bug-fix region. Hero 2 reads the same `fixes.db` so manual flags + git-scan flags both gate edits.

The CLI surface is already wired; this hero just consumes it.

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-Edit | < 50 µs |
| `evaluate()` cold graph | < 30 ms (includes signal cache load + `is_revert` per fix) |
| `evaluate()` warm + 5 fixes per file | < 5 ms |
| `is_revert()` per fix on a 1-KB diff | < 2 ms |

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| File with no recorded fixes | Allow — nothing to check. |
| Project's `fix_history.db` not yet built (no `scan_git_log` run) | Allow silently — `signals.fixes` returns `[]`. |
| Fix recorded with line_start=0, line_end=0 (whole-file marker from Week-2 git scan) | `is_revert` falls back to keyword-only check. Correct. |
| 100+ fixes on one file | Performance test: `is_revert` runs per-fix; cap to checking top-20 most recent if perf blows budget. (Defer until measured; document.) |
| Fix description is empty string | `is_revert` skips keyword check; relies only on diff-line overlap (which is empty for whole-file fixes). Effectively never matches → allow. Correct (no signal to surface). |
| Multi-file edit | First file only (Hero 4 + 1 follow same v2.0-alpha pattern). |
| Concurrent fixes records being added during evaluation | Week-2 R3 lock — `_db_locks` per-DB RLock serializes reads + writes. Safe. |
| `is_revert` has unicode in `before`/`after` blocks | Week-2 R2 fix — anchored regex parser handles it. |
| Diff is binary content | `is_revert` returns False on non-parseable diff. Allow. |

---

## Acceptance test list

8 scenarios:

1. **Non-Edit event allowed** — Read/Bash/Glob.
2. **Edit on file with no fixes recorded** — allow.
3. **Edit on file with fixes BUT no revert match** — allow (the diff doesn't look like a revert).
4. **Edit on file with fixes AND revert match** — block with diagnostic listing the fix(es).
5. **mode=warn produces warn instead of block** — same scenario as 4.
6. **mode=off disables policy** — allow even with revert.
7. **Hero 2 + Hero 1 simultaneous fire on a locked file with reverts** — block (combined verdict; Decision Lock priority=100 wins as primary message).
8. **Performance: warm signals + 1 fix < 5 ms p95.**

Tests live in `tests/engine/test_anti_regression.py`.

---

## Files affected

### New
| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/anti_regression.py` | The `AntiRegression` policy |
| `tests/engine/test_anti_regression.py` | Acceptance + behavioral + mutation regression tests (Tier-0 pre-flight from start) |

### Modified
| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `AntiRegression` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export the new policy |

### No change
- `indexer/fix_history.py` — Week-2 plumbing reused as-is
- `mcp_server/engine/signals.py` — `signals.fixes()` already exists
- No new CLI surface needed (`fix-noted` already wired)

---

## QA gate (Tier-0 pre-flight from start)

The Lesson #15-#17 + #18 discipline is now baseline, not exceptional. For Hero 2:

- ✅ **Real-DB test**: build a `fix_history.db` with `record_fix()`, run `signals.fixes()`, verify shape.
- ✅ **End-to-end dispatch**: register the hero via `register_default_policies`, fire a real PreToolUse event through `dispatch()`, verify block.
- ✅ **Behavioral spies**: `signals.fixes` spy + `is_revert` spy. Catch gates that output-only would miss.
- ✅ **10+ mutations from start**: each gate, each filter, each priority value, each default.
- ✅ **Bug-shape audit**: any field that the engine declares but never enforces? `enabled_by_default` already covered in Bug 3 fix; check for new ones in this hero.

R1-R8 plan unchanged from prior heroes. R3 mutation list at minimum:
- M1: is_edit gate
- M2: target_file None gate
- M3: signals None gate
- M4: mode=off gate
- M5: empty-fixes gate
- M6: proposed_diff None gate
- M7: is_revert empty-list gate
- M8: priority value
- M9: mode validation
- M10: enabled_by_default flag

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `is_revert` heuristic false positives → noisy blocks | Medium | Medium | High-recall by design. Document `mode=warn` env var as the escape hatch. Collect feedback in alpha; tune heuristic in v2.1. |
| `is_revert` false negatives → missed regressions | Medium | Low | The user already lost this signal without Hero 2. Even partial coverage is a net win. |
| Project never ran `codevira fix-noted --scan-git` | High | Low | Hero 2 silently allows. Empty `fixes.db` = no false positives. The user gets a nice surprise the day they DO scan. |
| `is_revert` slow on 100+ fixes per file | Low | Low | Cap at top-20 most recent if measured (deferred until reported). |
| Bug-fix commits with non-fix changes (mixed commits) | Medium | Low | `scan_git_log` records WHOLE-FILE fix marker (line_start=0, line_end=0) — keyword overlap dominates the heuristic. False-positive rate manageable. |

---

## Out of scope

- **Bug-class clustering** ("similar fixes" auto-grouping). v2.1+.
- **Auto-suggest deprecation+migration** when a fix is detected. Hero 9 territory.
- **Per-test-run "regression scoreboard"** showing how many fixes are still respected. v2.1.

---

## Definition of done

- [ ] `AntiRegression` policy registered + enabled by default.
- [ ] All 8 acceptance tests pass.
- [ ] R1-R8 + Tier-0 pre-flight clean — at least one finding per round documented (or honestly "no findings, here's why").
- [ ] At least one end-to-end test through `dispatch()` against a real `fix_history.db`.
- [ ] `docs/v2-execution-log.md` Week-8 entry written.
- [ ] No fourth Bug-3-class issue (audit during R8).
