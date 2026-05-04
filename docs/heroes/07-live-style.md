# Hero 7 — Live Style Enforcement

> "AI writes generically-correct code that doesn't match your team's idioms. Surface the mismatch."

The sixth policy hero. Where Heroes 1, 2, 4 are PreToolUse blockers, Hero 7 fires on **PostToolUse** — checking the AI's diff AFTER it lands and warning if it violates recorded preferences (`snake_case` vs `camelCase`, indent style, quote style, etc.).

Sprint week: **Week 9**. Should be small (~200 LOC) — most of the lift is regex-based pattern detection on the diff's `after` block.

---

## Problem statement

The AI writes correct code. But it writes `function getUserId()` in a project that's used `function get_user_id()` for years. Codevira's existing preferences engine ALREADY records "category=naming, signal=snake_case" when the user records preferences manually (or future Hero 10 auto-learns them). What's missing: a runtime check that the AI's NEW code matches the recorded preferences.

Hero 7 reads `signals.preferences()`, scans the AI's just-applied diff for violations, and surfaces them as warns. **Never blocks** — style is advisory, not safety-critical. The user can then ask the AI to fix, or accept and move on.

---

## User pain (concrete example)

**Without Hero 7:**

```text
[6 months ago, after several sessions]
codevira: learned preferences:
  • category=naming, signal=snake_case (frequency=42)
  • category=quotes, signal=double-quotes (frequency=28)

[Today]
User: "Add a helper to fetch user metadata."
AI:   *Edits user.py*
      def fetchUserMetadata(userId):    ← camelCase!
          return 'metadata for ' + userId    ← single quotes!
[code review later]
Reviewer: "Why camelCase? We're snake_case. Why single quotes?"
[user has to refactor by hand or ask AI to redo]
```

**With Hero 7:**

```text
User: "Add a helper to fetch user metadata."
AI:   *Edits user.py*
      def fetchUserMetadata(userId):
          return 'metadata for ' + userId
[Edit completes; PostToolUse fires]

⚠️ Style enforcement on user.py: 2 style violation(s) detected
  • naming: 'fetchUserMetadata' looks like camelCase, but project
    prefers snake_case (recorded 42×, last seen 2 weeks ago)
  • quotes: 4 single-quoted strings, but project prefers double-quotes
    (recorded 28×, last seen 1 month ago)

To fix: ask the AI to rewrite using project conventions, OR override
this session with CODEVIRA_LIVE_STYLE_MODE=off.

AI:   "I noticed style violations after writing — let me redo with
       project conventions:
       def fetch_user_metadata(user_id):
           return f'metadata for {user_id}'"
```

The win: **the AI catches the mismatch immediately**, before code review surfaces it.

---

## Mechanism

### Policy contract

```python
class LiveStyleEnforcement(Policy):
    name = "live_style_enforcement"
    handles = (EventType.POST_TOOL_USE,)
    enabled_by_default = True
    priority = 20   # advisory; runs after other PostToolUse policies

    def evaluate(self, event, signals):
        if event.event_type != EventType.POST_TOOL_USE:
            return PolicyVerdict.allow()
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        prefs = signals.preferences()
        if not prefs:
            # No recorded preferences → nothing to enforce
            return PolicyVerdict.allow()

        # Extract the AI's added/changed content (after block of diff)
        after_text = _extract_after_block(event.proposed_diff)
        if not after_text:
            return PolicyVerdict.allow()

        violations = _detect_violations(
            after_text=after_text,
            target_file=event.target_file,
            preferences=prefs,
        )
        if not violations:
            return PolicyVerdict.allow()

        return self._make_verdict(event, config, violations, prefs)
```

The whole policy is < 250 LOC including the regex detectors.

### Decision tree

```
POST_TOOL_USE event arrives
│
├── Not Edit/Write/MultiEdit?      → ALLOW
├── No target_file?                → ALLOW
├── mode = "off"?                  → ALLOW
├── No signals?                    → ALLOW
├── No recorded preferences?       → ALLOW (silent no-op until prefs exist)
│
├── Diff after-block empty?        → ALLOW
│
├── No violations detected?        → ALLOW
│
└── Violations found
    └── WARN (never blocks; style is advisory)
```

### Detector framework

Each preference becomes a `_StyleDetector` — pure function from `(after_text, target_file, signal)` → list of violations.

Built-in detectors for v2.0-alpha:

| Category | Signal | Detector |
|---|---|---|
| `naming` | `snake_case` | scan `def NAME(` and `class NAME` for camelCase identifiers (Python) + `function NAME(` for JS/TS |
| `naming` | `camelCase` | inverse — flag snake_case identifiers |
| `quotes` | `double-quotes` / `double` | count single-quoted strings in non-comment lines |
| `quotes` | `single-quotes` / `single` | inverse |
| `indent` | `spaces` / `4-spaces` | count lines with leading tabs |
| `indent` | `tabs` | count lines with leading spaces |

Each detector returns `[{"line": int, "snippet": str, "rule": str}, ...]`. Empty list = no violation.

If the preference signal isn't recognized (e.g. "category=other, signal=use_dataclasses"), the detector is a no-op for v2.0-alpha. v2.1 adds custom rule support.

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_LIVE_STYLE_MODE` | `warn` | `off` / `warn`. (No `block` mode — style is never blocking.) |
| `CODEVIRA_LIVE_STYLE_MIN_FREQ` | `3` | Skip preferences observed fewer than N times (low-confidence noise) |

### What "is_edit" means for PostToolUse

The base `HookEvent.is_edit()` checks `event_type == PRE_TOOL_USE`. We need a similar predicate for POST. Hero 7 inlines the check rather than expanding the base helper for one user — straightforward `event.tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}` plus `event.event_type == POST_TOOL_USE`.

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-Edit POST event | < 50 µs |
| `evaluate()` for Edit, no preferences | < 1 ms (one signal call returning empty) |
| `evaluate()` for Edit, 5 preferences, 1 KB diff | < 5 ms |
| `_detect_violations` per detector on 10 KB diff | < 2 ms |

Detectors are regex-based and run sequentially; total cost is bounded by the number of preferences × diff size. Keep diff size cap at 100 KB (same as Hero 4's `_MAX_DIFF_BYTES`) for safety.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Project has no preferences recorded | Silent no-op. Hero 7 only fires once preferences exist. |
| Preferences exist but signal isn't recognized | Detector returns []; effectively no-op for that pref. |
| Preferences DB raises (corrupt, locked) | `signals.preferences()` already catches — returns `[]` → allow. |
| Non-Python / non-JS file (e.g. `.md`, `.json`) | Naming + quote detectors are language-aware via `target_file.suffix` lookup. Unknown extensions → all detectors no-op. |
| File with mixed style (legacy code + new code) | Hero 7 only looks at the AFTER block of the diff. Pre-existing violations elsewhere in the file aren't surfaced — that's a separate audit, not a regression. Documented as scope. |
| Naming preferences but no def/function/class lines in diff | Detector returns []; no violation surfaced. |
| 50 preferences recorded, all with frequency=1 | `MIN_FREQ=3` default skips them — too noisy. Documented. User can override via env. |
| User intentionally violated style for one method (e.g. third-party API match) | False positive. Acceptable — the AI sees the warn and the user can ignore. v2.1 adds inline opt-out comments. |
| Diff has Unicode identifiers (`def フォー():`) | Regex `[A-Za-z_]\w*` won't match. Skipped. Documented as v2.1+ enhancement. |

---

## Acceptance test list

8 scenarios:

1. **Non-PostToolUse event allowed** — PreToolUse, SessionStart, etc. pass through.
2. **PostToolUse on non-Edit allowed** — PostToolUse with tool_name=Read.
3. **Edit but no preferences recorded → allow** — fresh project.
4. **Edit with snake_case preference + camelCase identifier in diff → warn** — pattern violation.
5. **Edit with snake_case preference but matching identifier in diff → allow** — no violation.
6. **Edit with double-quote preference + single-quoted strings in diff → warn**.
7. **mode=off disables the policy** — even with violations.
8. **Performance: warm signals + 5 preferences < 5 ms p95.**

Tests live in `tests/engine/test_live_style.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/live_style.py` | The `LiveStyleEnforcement` policy + detectors |
| `tests/engine/test_live_style.py` | Acceptance + behavioral + mutation tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `LiveStyleEnforcement` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export the new policy |

---

## QA gate (Tier-0 pre-flight from start)

Per Lessons #15-#18 (now muscle memory):

- ✅ Real-DB integration — preferences stored via `db.add_preference` then read via `signals.preferences`
- ✅ Behavioral spies — verify `signals.preferences` IS called when expected, and is NOT called when gated
- ✅ End-to-end dispatch test — register Hero 7, fire a PostToolUse Edit through `dispatch()`, assert warn
- ✅ 10+ mutations from start
- ✅ Bug-shape audit — every contract field used (no dead fields)

R1-R8 plan as for prior heroes.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Detectors over-report (false positives) — tests as snake_case shouldn't trigger camelCase detector if camelCase IS the project pref | High at first | Low | High-recall by design. User can disable with `mode=off`. v2.1 tunes precision via per-file overrides. |
| Detectors miss subtle violations | Medium | Low | This is per-detector quality; v2.1 adds tree-sitter parsing for accurate token boundaries. v2.0-alpha is regex. |
| `signals.preferences()` returns wrong shape (Bug-1-class drift) | Low | High | Tier-0 pre-flight: real-DB integration test. |
| Performance regression on long diffs | Medium | Low | Diff size cap (100 KB) + `_MAX_VIOLATIONS_PER_DETECTOR` cap (50) bound work. |

---

## Out of scope (deferred)

- **Auto-fix suggestions in the warn message** ("here's the snake_case version") — needs LLM call. Hero 9 territory.
- **Tree-sitter parsing** for accurate identifier extraction — v2.1+.
- **Per-file overrides** (e.g. `# codevira:style-allow camelCase` comment) — v2.1.
- **Configuration via YAML** — env vars only for alpha.

---

## Definition of done

- [ ] `LiveStyleEnforcement` policy registered + enabled by default.
- [ ] All 8 acceptance tests pass.
- [ ] R1-R8 + Tier-0 pre-flight clean.
- [ ] At least one end-to-end test through `dispatch()` against a real preferences DB.
- [ ] No new Bug-class issues (audit during R8).
- [ ] `docs/v2-execution-log.md` Week-9 entry written.
