# Hero 9 — Proactive Intent Inference

> "AI makes 5 round-trips fetching context it could have gotten in one shot. Pre-fetch what the prompt obviously needs."

The eighth policy hero. Fires on **USER_PROMPT_SUBMIT** alongside Hero 5 (Cross-Session Consistency) but adds a different layer: **intent classification + file-mention extraction → per-intent context pre-fetch**.

Sprint week: **Week 11**. ~330 LOC across one classifier + one fetcher + one policy. The master plan envisioned an LLM call for classification; v2.0-alpha uses a regex classifier (local-first non-negotiable). v2.1 may add an OPTIONAL LLM-backed classifier behind an env var.

---

## Problem statement

Today the AI's first turn often looks like this:

```
User: "Fix the auth flow — login is broken when the user has special chars in their email"
AI:   <reads auth.py>
AI:   <reads users.py>
AI:   <calls codevira.search_decisions("auth")>
AI:   <calls codevira.get_impact("auth.py")>
AI:   <calls codevira.search_fixes("auth")>  (if Hero 2's table was exposed as a tool)
AI:   *finally has enough context to propose a fix*
```

5 round-trips before the AI can write a single line. Each adds latency + token spend.

Hero 9 reads the user's prompt UPFRONT (UserPromptSubmit hook), classifies the intent (`fix-bug` here), extracts file mentions (`auth.py` if mentioned, otherwise inferred from keywords), and pre-fetches the signals that intent needs — fix history, related decisions, blast radius — all into one inject. The AI's first turn already has the context.

---

## User pain (concrete example)

**Without Hero 9:**

(The 5-round-trip pattern above.)

**With Hero 9:**

```
User: "Fix the auth flow — login is broken when the user has special chars in their email"

[Hero 9 inject, before AI's first turn]:
   ## Codevira pre-fetch — intent: fix-bug

   Files mentioned: auth.py (inferred from "auth flow")

   ### Recent fixes touching this area:
   - 2025-04-13 (auth.py): "regex didn't escape '+' in email validation"
   - 2025-03-08 (users.py): "fix: unicode-normalize email before lookup"

   ### Related decisions:
   - 2025-04-13 (auth.py): "use bcrypt over argon2 — see issue #142"

   ### Blast radius for auth.py:
   - 7 callers across api.py, login.py, password_reset.py, ...

AI: *responds with full context immediately — no round-trips needed*
```

The win: **the AI sees the actual fix history of auth.py BEFORE proposing a fix**, and is much less likely to re-introduce a previously-fixed bug (Hero 2 catches it post-hoc; Hero 9 prevents it pre-hoc).

---

## Mechanism

### Three pieces

**1. Intent classifier** (regex-based, ordered):

| Intent | Trigger patterns (case-insensitive) |
|---|---|
| `fix-bug` | `\bfix\b`, `\bbug\b`, `\bbroken\b`, `\bdoesn't\s+work\b`, `\berror\b`, `\bcrash`, `\bfailing\b`, `\bregression\b` |
| `add-feature` | `\badd\b`, `\bimplement\b`, `\bcreate\b`, `\bbuild\b`, `\bnew\s+(feature\|endpoint\|method)\b` |
| `refactor` | `\brefactor\b`, `\bclean\s*up\b`, `\bsimplify\b`, `\brename\b`, `\bextract\b` |
| `explain` | `\bexplain\b`, `\bwhat\s+does\b`, `\bhow\s+does\b`, `\bdescribe\b`, `\bsummarize\b` |
| `test` | `\b(write\|add)\s+(a\s+)?tests?\b`, `\btest\s+coverage\b` |
| `docs` | `\b(write\|add)\s+(docs?\|comments?\|docstrings?)\b`, `\bdocument\b` |
| `other` | (default — no patterns match) |

First match wins. Ordered by *specificity*: fix-bug + test + docs are checked before refactor/explain because `"fix the test"` should classify as `fix-bug`, not `test`.

**2. File-mention extractor**:

```python
_FILE_MENTION_RE = re.compile(
    r"(?:^|[\s'\"`(])"
    r"((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,5})"  # path with extension
    r"(?=$|[\s'\"`):,.!?])"
)
```

Returns up to N (default 3) distinct file mentions, in order of first appearance. Filters: extension must be in a known-language allowlist (`.py`, `.js`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`, etc.) so we don't pick up `email@example.com` as `example.com` file.

If the prompt has NO file mentions, fall back to keyword-based area inference: extract content keywords (reuse Hero 5's tokenizer), look up the top-N graph nodes whose `name` or `file_path` matches.

**3. Per-intent signal fetcher**:

| Intent | Signals fetched |
|---|---|
| `fix-bug` | `fixes(file)` for each file mention + `decisions(file=...)` + `impact(file)` (so the AI sees regression risk) |
| `add-feature` | `outcomes()` (top-stable for similar areas) + `decisions(file=...)` |
| `refactor` | `impact(file)` for each file mention + `decisions(file=...)` |
| `explain` | `decisions(file=...)` + `outcomes()` (top-stable in the area) |
| `test` | (skip — let Hero 5 handle decisions on test files) |
| `docs` | (skip — minimal context needed) |
| `other` | `decisions(file=...)` if files mentioned, else nothing |

Each signal call is wrapped in try/except → empty list on failure. **Hero 9 NEVER lets a signal failure propagate** — same robustness as Heroes 5, 7, 10.

### Decision tree

```
USER_PROMPT_SUBMIT event arrives
│
├── event_type != USER_PROMPT_SUBMIT?  → ALLOW
├── mode = "off"?                      → ALLOW
├── prompt_text is None / too short?   → ALLOW
├── signals is None?                   → ALLOW
│
├── Classify intent (regex)
├── Extract file mentions (regex + extension allowlist)
│
├── intent in {test, docs}?            → ALLOW (Hero 5 + others handle)
│
├── Fetch signals per intent
│
├── No signals returned anything?      → ALLOW (silent)
│
└── INJECT formatted context
    (priority=20; lower than Hero 5's 30 — Hero 5 surfaces past decisions
    by keyword search; Hero 9 surfaces context by inferred intent. Both
    inject contexts concatenate via the engine's verdict combiner.)
```

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_INTENT_INFERENCE_MODE` | `inject` | `off` / `inject` |
| `CODEVIRA_INTENT_INFERENCE_MAX_FILES` | `3` | Cap on file mentions extracted (1-10) |
| `CODEVIRA_INTENT_INFERENCE_MIN_PROMPT_CHARS` | `10` | Skip very-short prompts (matches Hero 5) |
| `CODEVIRA_INTENT_INFERENCE_MAX_FIXES_PER_FILE` | `3` | Per-file fix history cap |
| `CODEVIRA_INTENT_INFERENCE_MAX_DECISIONS_PER_FILE` | `3` | Per-file decision cap |
| `CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT` | `1` | `0` to disable blast-radius lookups (slower path) |

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-prompt event | < 50 µs |
| `evaluate()` for prompt, no signals to fetch | < 1 ms |
| `evaluate()` for prompt with 1 file + intent=fix-bug + 5 fixes + impact | < 20 ms |

The biggest cost is `signals.impact()` which queries the graph. We cap to one impact lookup per file mention (max 3 by default).

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Empty / whitespace-only prompt | Allow (covered by `_MIN_PROMPT_CHARS`). |
| Prompt mentions a non-existent file (e.g. `auth.ts` in a Python project) | File mention extracted but `signals.fixes()` / `signals.impact()` return empty. Hero 9's inject section is empty for that file → silently skipped. |
| Prompt has 50 file mentions | Cap to `MAX_FILES` (default 3). Documented. |
| Two intents both match (e.g. "fix the test") | First match wins (`fix-bug` checked before `test`). Documented as intentional. |
| Intent = "test" / "docs" | Skip — those don't need codevira context. |
| File mention with weird path (`../etc/passwd.py`) | Pattern extracts; `signals.impact()` resolves via project_root and is path-traversal-defended (Round-4 HIGH #1 fix). Safe. |
| Prompt is in non-English | Regex patterns are English-only. `intent = "other"`. Falls through to default handling. v2.1 adds i18n. |
| Hero 9 + Hero 5 both inject | Concatenate via engine combiner (`\n\n` separator). Priority 30 (H5) > 20 (H9), so H5's section comes first. |
| Hero 9 + signals.impact raises | try/except → empty impact section → still emit the rest of the inject. |

---

## Acceptance test list

12 scenarios:

1. Non-prompt event allowed (PreToolUse, etc.).
2. Empty / short prompt allowed (no signal calls).
3. Prompt with intent=fix-bug + file mention → inject contains fixes + decisions sections.
4. Prompt with intent=add-feature + file mention → inject contains outcomes section.
5. Prompt with intent=refactor + file mention → inject contains impact section.
6. Prompt with intent=test → silent allow (no inject).
7. Prompt with intent=docs → silent allow.
8. Prompt with no file mention + intent=other → silent allow.
9. mode=off → silent allow even with rich data.
10. Multiple file mentions cap honored (MAX_FILES).
11. Hero 5 + Hero 9 BOTH fire → inject combiner concatenates both.
12. End-to-end through `claude_code_hooks.handle("UserPromptSubmit")`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/intent_classifier.py` | Pure intent + file-mention regex functions |
| `mcp_server/engine/policies/intent_inference.py` | The `ProactiveIntentInference` policy |
| `tests/engine/test_intent_inference.py` | Acceptance + behavioral + mutation tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export |

---

## QA gate (Tier-0 pre-flight)

Per the post-Bug-4 muscle memory:

- ✅ Real-DB integration — fixes via `record_fix()`, decisions + impact via real graph
- ✅ Behavioral spies — verify signal-fetch ordering and gating
- ✅ End-to-end dispatch — register all 8 heroes, fire UserPromptSubmit, assert combined inject
- ✅ End-to-end through `claude_code_hooks.handle("UserPromptSubmit")` — Bug-4 lesson; the wiring path tested with realistic JSON
- ✅ 10+ mutations from start
- ✅ Bug-shape audit — every contract field exercised

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Regex classifier mis-classifies subtle prompts | High | Low | High recall by design. v2.1 adds optional LLM classifier. False classification → wrong but still useful inject. |
| File-mention regex matches false positives | Medium | Low | Extension allowlist filters non-file matches. v2.1 verifies file actually exists in project. |
| Hero 9 + Hero 5 produce overlapping decisions | Medium | Low | Documented as expected; both sections are valuable from different angles (keyword vs intent). v2.1 may dedup. |
| `signals.impact()` slow on large projects | Medium | Medium | Cap to MAX_FILES (3); env var `CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT=0` disables. |
| Bug-4-shape: UserPromptSubmit wiring path drift | Low | High | Already covered by Hero 5's tests + Hero 5 has shipped through this path. New code: end-to-end test via `claude_code_hooks.handle()`. |

---

## Out of scope (deferred)

- **LLM-based intent classifier** — v2.1 with optional `CODEVIRA_INTENT_INFERENCE_LLM` env var pointing at a local Ollama or similar.
- **Multi-language regex patterns** — v2.1 (i18n).
- **Embedding-based similar-decisions retrieval** — v2.2.
- **Inline "AI: do you want me to fetch X too?" prompts** — needs UI; Hero 8 territory.
- **Hero 5/Hero 9 dedup of overlapping decisions** — v2.1.

---

## Definition of done

- [ ] `ProactiveIntentInference` policy registered + enabled by default.
- [ ] All 12 acceptance tests pass.
- [ ] R1-R8 + Tier-0 pre-flight clean.
- [ ] At least one end-to-end test through `claude_code_hooks.handle("UserPromptSubmit")` against a real graph DB.
- [ ] Integration QA round Weeks 1-11 (don't wait for user to ask).
- [ ] No new Bug-class issues.
- [ ] `docs/v2-execution-log.md` Week-11 entry written.
