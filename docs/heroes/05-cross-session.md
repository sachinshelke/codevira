# Hero 5 — Cross-Session Consistency

> "Last week we decided on Tailwind. Don't propose Bootstrap today and forget."

The third policy hero. Where Heroes 1 and 4 were `PRE_TOOL_USE` blockers, Hero 5 is a `USER_PROMPT_SUBMIT` **injector**: when the user types a prompt, codevira surfaces relevant prior decisions into the AI's context — proactively, before the AI proposes anything.

Sprint week: **Week 6**. Goal: a policy that's smaller than Hero 1 (no graph traversal, just keyword search), reuses the engine machinery proven across Hero 4 + Hero 1, and lands in v2.0-alpha.2 alongside Hero 1.

---

## Problem statement

LLMs have no episodic memory across sessions. The user said "use Tailwind, not Bootstrap" three weeks ago — that decision is in `decisions.db`, but it's not in the AI's context window. So when the user today asks "add a styled button," the AI happily reaches for Bootstrap (or whatever it pattern-matches first).

Codevira's `search_decisions` tool can find that prior decision — but only if the AI thinks to call it. Hero 5 makes the call automatic: every user prompt, codevira checks for relevant decisions and prepends them as `additionalContext` before the AI's first turn.

---

## User pain (concrete example)

**Without Hero 5:**

```text
[3 weeks ago]
User: After comparing, we're going with Tailwind.
User: $ codevira record-decision "Tailwind, not Bootstrap — bundle size matters" --file styles/

[Today]
User: Add a styled "Get Started" button to the homepage hero.
AI:   *Reads existing components, sees no styling library imports*
      "I'll add Bootstrap for the button styling..."
      *Adds bootstrap.min.css to the head*
      *Writes <button class="btn btn-primary">Get Started</button>*
User: Wait, why Bootstrap? We're on Tailwind.
```

**With Hero 5:**

```text
[Today]
User: Add a styled "Get Started" button to the homepage hero.

(codevira injects via UserPromptSubmit, before AI's first response):
   ## Prior decisions you may want to consider
   - 2025-04-13 [styles/] "Tailwind, not Bootstrap — bundle size matters"
   - 2025-04-08 [components/] "Use class:hover, not @apply hover:..."
   If your current request conflicts with any of these, surface the
   conflict to the user before proceeding.

AI:   "I'll add the button using Tailwind's utility classes — to match
       the project's existing styling decision."
       *Writes <button class="bg-blue-500 hover:bg-blue-600 ...">*
```

The win: **the decision is in context for free**, not buried in a `decisions.db` the AI never thought to query.

---

## Mechanism

### Policy contract

```python
class CrossSessionConsistency(Policy):
    name = "cross_session_consistency"
    handles = (EventType.USER_PROMPT_SUBMIT,)
    enabled_by_default = True
    # Lower priority than block-class policies (1, 4): inject is
    # advisory, runs after blocks decide. Priority only affects the
    # order injects compose if multiple inject policies fire.
    priority = 30

    def evaluate(self, event, signals):
        if event.event_type != EventType.USER_PROMPT_SUBMIT:
            return PolicyVerdict.allow()
        if not event.prompt_text:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        # Extract query tokens, search decisions for each, dedup
        tokens = _extract_keywords(event.prompt_text)
        if not tokens:
            return PolicyVerdict.allow()

        matches = _collect_matches(signals, tokens, max_per_token=3, total_cap=5)
        if not matches:
            return PolicyVerdict.allow()

        return PolicyVerdict.inject(
            context=_format_injection(matches),
            metadata={...},
        )
```

The whole policy is < 200 LOC.

### Decision tree

```
USER_PROMPT_SUBMIT event arrives
│
├── Empty prompt or < 10 chars?           → ALLOW (greeting / ack)
├── mode = "off"?                         → ALLOW
├── No signals (engine wiring failure)?   → ALLOW
│
├── extract_keywords(prompt) returns []?  → ALLOW
│   (prompt is all stop-words / punctuation)
│
├── No decisions match any keyword?       → ALLOW
│
└── At least one match
    └── INJECT formatted decision list
```

### Keyword extraction

For each prompt:
1. Lowercase, split on whitespace + punctuation
2. Filter: ≥ 3 chars, not all-numeric, not pure punctuation
3. Filter against a small built-in stop-words list (~70 common English words)
4. Cap at 5 distinct tokens (top-frequency or first-occurrence)

For "Add a styled Get Started button to the homepage hero":
- Tokens after filter: `[styled, started, button, homepage, hero]`
- Top-5 unique: same.
- Each becomes a `signals.search_decisions(token, limit=3)` call.

The cap-at-5 is intentional: we don't want one chatty prompt to trigger 50 SQL queries. Token efficiency matters.

### Match collection

For each token, search → top-3 results. Then:
1. Dedup by `(decision_text, file_path)` — same decision matched by multiple tokens shows once.
2. Sort by `created_at` DESC — most recent first.
3. Cap at 5 total matches.

If 0 matches → no injection (allow).
If 1+ matches → inject formatted list.

### Injection format

```
## Prior decisions you may want to consider

Based on your prompt, here are recent codevira-tracked decisions on related topics:

1. 2025-04-13 — [styles/] Tailwind, not Bootstrap — bundle size matters
2. 2025-04-08 — [components/] Use class:hover, not @apply hover:...
3. 2025-03-29 — [api/] FastAPI, not Flask — async-first by default

If your current request conflicts with any of these, surface the conflict to the user before proceeding.
```

Token budget: 5 decisions × ~50 tokens each + 80-token preamble = ~330 tokens. Well under the "respect the AI's context window" promise.

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_CROSS_SESSION_MODE` | `inject` | `off` / `inject` (only two modes; no warn/block analog applies) |
| `CODEVIRA_CROSS_SESSION_MAX_INJECT` | `5` | Total decisions to surface across all keywords |

Same env-var pattern as Heroes 1 + 4. YAML in v2.1.

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-USER_PROMPT_SUBMIT | < 50 µs (just an event-type check) |
| Keyword extraction on 200-char prompt | < 0.5 ms |
| `signals.search_decisions(token)` × 5 tokens (cold) | < 50 ms (each is one LIKE query) |
| Same with cached signals | < 1 ms |
| Total `evaluate()` p95 (cold graph) | < 100 ms |

The expensive part is 5 SQLite LIKE queries. If the decisions table is small (which it usually is — most projects have < 100 decisions), this is sub-millisecond. If it grows to 10K+, we'd add an FTS5 index in v2.1.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Prompt is empty | Allow — nothing to search on. |
| Prompt is < 10 chars (e.g. "ok", "thanks") | Allow — likely a greeting; not worth a search. |
| Prompt is all stop-words ("are you there?") | Allow — no useful keywords extracted. |
| Prompt has 100+ tokens | Use top-5 by uniqueness; ignore the rest. (Also reduces SQL query count.) |
| `decisions.db` doesn't exist | Allow silently — `signals.search_decisions` returns []. |
| All matches are duplicates | Dedup → 1 unique → inject once. |
| Decision text contains markdown formatting | Render as-is in the inject context. The user's AI tool will render it. |
| Decision context is None | Skip context line in the format; show decision + file_path + date only. |
| Keyword matches multiple unrelated decisions (false positives) | Acceptable — better to over-surface than to under-surface. The AI can ignore irrelevant items. |
| Project has 1000+ matching decisions for "the" | Stop-word filter prevents "the" from being a query token; if a non-stop-word matches 1000 times, top-3 by recency keeps response bounded. |
| Same decision appears in two adjacent sessions | Single decision — `created_at` dedup keeps the most recent record. |

---

## Demo storyboard (10-second scene)

1. **(0.0s)** User typing in Claude Code: "add a styled button to the homepage"
2. **(2.0s)** Hits Enter. Codevira's UserPromptSubmit fires.
3. **(2.5s)** AI's first turn includes:
   ```
   [from codevira]: Prior decisions:
     - 2025-04-13: Tailwind, not Bootstrap (bundle size)
   ```
4. **(4.0s)** AI: "I'll use Tailwind utility classes to match the project's styling decision."
5. **(7.0s)** AI proposes the change with `bg-blue-500 hover:bg-blue-600 ...`
6. **(10.0s)** End frame. The user never had to remind the AI.

---

## Acceptance test list

8 scenarios that have to pass before Hero 5 ships:

1. **Non-USER_PROMPT_SUBMIT event allowed** — PreToolUse, SessionStart, etc. pass through unchanged.
2. **Empty prompt allowed (no inject)** — `event.prompt_text = ""`.
3. **Short prompt allowed** — prompts < 10 chars skip the search.
4. **All-stop-words prompt allowed** — "are you there?" extracts no tokens.
5. **No matching decisions → allow** — keywords extracted but search returns 0 hits.
6. **Matching decisions → inject formatted context** — 3 decisions inject as numbered markdown list.
7. **Dedup matches across keywords** — same decision matched by 2 tokens shows once.
8. **Performance: warm-graph < 1 ms p95 over 100 trials.**

Tests live in `tests/engine/test_cross_session.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/cross_session.py` | The `CrossSessionConsistency` policy implementation |
| `tests/engine/test_cross_session.py` | 8 acceptance + adversarial / mutation regression tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `CrossSessionConsistency` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export `CrossSessionConsistency` |
| `mcp_server/engine/signals.py` | (Already done before spec write) Added `signals.search_decisions(query, limit)` |

### No change

- No CLI surface. Pure env-var config in alpha.
- No new wiring code. Hero 5 is the FIRST policy to use the existing `inject` verdict path; the wiring layer handled this since Week-1 R5.

---

## QA gate

Per Lessons #10 + #13, full R1-R8 + integration. Hero 5's user-facing surface is the injected text the AI sees — easy to be wrong in subtle ways.

Specific R8 priorities for Hero 5:
- Verify Claude Code `additionalContext` field arrives correctly under `hookSpecificOutput` (R5 from Week 1's claude-code-guide round confirmed this — same path; verify it still holds for our injected content).
- Adversarial: prompt with shell metas / SQL meta / Markdown injection (the AI sees rendered markdown — could a decision text containing ``` ``` ` `` ` ` `` ``` close out a code fence and inject hostile content? In practice the AI tool sanitizes; we should still test).

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Over-surfacing irrelevant decisions ("button" → 50 hits across the project) | Medium | Low | Cap-at-5 keeps the injection bounded. Recency sort prefers the user's latest thinking. False positives are noise, not damage. |
| Under-surfacing ("Bootstrap" misses "Tailwind decision") | Medium | Medium | Token-by-token search means the user must mention the topic at least obliquely. Hero 5 is BEST-EFFORT; it's not a substitute for the user being on top of their decisions. |
| Decision text containing AI-prompt-injection ("Ignore previous instructions...") | Low | High | The AI sees codevira's injection as `additionalContext`, not user input. Even if a malicious decision text contained "Ignore all previous instructions," it's clearly delimited. We don't HTML-escape (markdown stays markdown), but we DO prefix with "Prior decisions you may want to consider" so the AI knows the source. The full hostile-decision attack requires an attacker to have already gotten a decision into the user's `decisions.db` — which means they already control the user's machine. Out of v2.0 threat model. |
| 5 SQL queries × 1ms each = 5ms hot path | Low | Low | Acceptable. If the user has 10K+ decisions, FTS5 in v2.1. |
| Markdown formatting in decision text breaking the AI's rendering | Medium | Low | Test with adversarial markdown (code fences, bold, links). Acceptable to render as-is — the AI tool typically sandboxes. |

---

## Out of scope (deferred)

- **Semantic search** (embeddings-based relevance): bigger work, v2.1+.
- **FTS5 SQLite full-text search**: only needed when projects have many decisions. Defer until a real user reports slow Hero-5 with 5000+ decisions.
- **Per-project keyword stop-words**: a project might have a domain term that should NOT be a stop-word ("the queue manager"). Defer; default stop-words are English-only conservative.
- **Time-decay weighting** (older decisions weighted less): currently strict recency sort. Could combine with frequency; defer.

---

## Definition of done

- [ ] `CrossSessionConsistency` policy registered and enabled by default.
- [ ] All 8 acceptance tests pass.
- [ ] R1-R8 QA gauntlet clean.
- [ ] Performance bench p95 in `tests/engine/test_cross_session.py`.
- [ ] First USER_PROMPT_SUBMIT inject through real Claude Code (or subprocess simulation) verified end-to-end.
- [ ] `docs/v2-execution-log.md` Week-6 entry written.
- [ ] v2.0-alpha.2 plan updated to bundle Hero 1 + Hero 5.
