# Hero 3 — Scope Contract Lock

> "AI scope creep — 'fix this null check' becomes 'and refactor 3 unrelated files'. Block it."

The ninth hero. **Highest-risk in the master plan**: relies on intent inference (which can mis-classify) AND blocks Edits (which can frustrate users when wrong). Off by default; opt-in per project.

Sprint week: **Week 12**. ~330 LOC across one extended module + one policy. Reuses Hero 9's regex intent classifier + file-mention extractor (already shipped, tested).

Risk-mitigation discipline applied from start (post-Bug 1-8):
- Tier-0 pre-flight (real DB, behavioral spies, dispatch end-to-end, wiring end-to-end, 10+ mutations, bug-shape audit)
- Deep-audit probes (path-traversal, content-verifying assertions, all 4 _EDIT_TOOLS, defense-in-depth parity)
- Proactive integration QA round across Weeks 1-12

---

## Problem statement

The AI's prompt is "fix the null check in `auth.py`". The AI does that — and ALSO refactors `users.py`, renames a method in `db.py`, and updates 4 unrelated test files. The user reviews the diff, sees the scope creep, and either spends 10 minutes manually reverting OR accepts it (and now `users.py` has unintended changes).

**Hero 3 prevents this**: parses the user's prompt at UserPromptSubmit, builds a *scope contract* (allowed files inferred from mentions + intent-based LOC budget), enforces on PreToolUse — refuses Edits to files outside the contract.

---

## User pain (concrete example)

**Without Hero 3:**

```
User: "fix the null check in auth.py"

AI:   *Edits auth.py* (correct)
AI:   *Edits users.py* — "I noticed users.py also has a similar issue"
AI:   *Edits db.py* — "and db.py uses the same pattern"
AI:   *Edits tests/test_auth.py* — "let me update the tests too"

[User reviews 4 files of unintended changes; manually reverts users.py, db.py, tests/test_auth.py]
```

**With Hero 3 (mode=block):**

```
User: "fix the null check in auth.py"

AI:   *Edits auth.py* (in scope — allowed)
AI:   *Tries to Edit users.py*
codevira: 🔒 Scope-lock veto on users.py.
   Prompt only mentioned auth.py; this Edit is outside scope.
   Original prompt: "fix the null check in auth.py"
   To proceed: ask user to extend scope, or override with
   CODEVIRA_SCOPE_LOCK_MODE=warn for this session.

AI:   "I notice users.py has a similar issue. Should I extend scope?"
User: "yes, also fix users.py"

[Hero 3 rebuilds contract on the new prompt; users.py is now in scope]
AI:   *Edits users.py* (now allowed)
```

The win: **scope-creep edits get caught at the source**, before they hit disk. User stays in control of which files get changed in each turn.

---

## Mechanism

### Two-event policy

`ProactiveScopeContractLock` registers on **two** events:

| Event | Phase | What it does |
|---|---|---|
| `USER_PROMPT_SUBMIT` | Build | Classify intent (Hero 9's classifier), extract file mentions, build a `ScopeContract`, store keyed by `session_id` |
| `PRE_TOOL_USE` | Enforce | Look up contract by `session_id`. If proposed Edit's `target_file` ∉ contract's `allowed_files` → BLOCK |

This is the first hero to handle two events in one policy. The dispatch loop already supports multi-event handlers (`p.handles` is a tuple, runner checks `event.event_type in set(p.handles)`).

### State storage

Per-session contracts live in a process-module dict in `mcp_server/engine/scope_contract.py`:

```python
_session_contracts: dict[str, _ContractEntry]   # session_id → entry
_MAX_CONTRACT_AGE_SECONDS = 3600                  # 1-hour TTL
```

Each entry is `(contract, created_at)`. On every read, we evict entries older than the TTL. This bounds memory and prevents stale contracts from misclassifying after a long break.

### Building the contract (UserPromptSubmit)

1. Run Hero 9's `classify_intent(prompt)`.
2. If intent in `{test, docs, other}` → no contract built (no clear scope).
3. If intent in `{fix-bug, add-feature, refactor, explain}`:
   - Run Hero 9's `extract_file_mentions(prompt)`.
   - **Apply Bug-5-shape defense**: resolve each mention via `(project_root / mention).resolve()`, then `relative_to(resolved_root)` — drop out-of-project mentions.
   - If no in-project file mentions → no contract built (no concrete scope to enforce).
   - Otherwise build `ScopeContract`:
     - `session_id = event.session_id or ""` (silent skip if no session_id)
     - `allowed_files = {resolved_abs_paths}` as set of strings (relative to project_root for portability)
     - `original_intent = intent`
     - `max_loc_delta` = per-intent default (fix-bug=50, refactor=200, add-feature=500)
     - `created_at = time.time()`
4. Store via `set_session_contract(session_id, contract)`.
5. Return `allow` (build phase doesn't block; it's setup).

### Enforcing the contract (PreToolUse)

1. Filter to Edit / Write / MultiEdit / NotebookEdit (use `event.is_edit()`). Bug-7 lesson: this MUST work for all 4 tools through wiring.
2. If `event.target_file is None` → allow (no file to check).
3. If `event.session_id is None` → allow (no contract lookup possible).
4. Look up `get_session_contract(session_id)`. If `None` (no prior prompt or expired) → allow.
5. Compare `event.target_file` to contract's `allowed_files` (set membership; comparison via `relative_to(project_root)` for stability).
6. If in-scope → allow.
7. If out-of-scope:
   - mode=off → allow
   - mode=warn → return warn verdict
   - mode=block → return block verdict with helpful message

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_SCOPE_LOCK_MODE` | `off` | `off` / `warn` / `block`. **Off by default** — opt-in per project. |
| `CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS` | `3600` | TTL for stored contracts (clamped 60-86400) |
| `CODEVIRA_SCOPE_LOCK_FIX_LOC_DELTA` | `50` | Default LOC delta cap for fix-bug intent (informational, not enforced in v2.0-alpha) |

### Decision tree

```
event arrives
│
├── event_type not in (USER_PROMPT_SUBMIT, PRE_TOOL_USE)?  → ALLOW
├── mode = "off"?                                          → ALLOW
│
├── UserPromptSubmit:
│   ├── prompt empty / too short?            → ALLOW (no build)
│   ├── intent ∈ {test, docs, other}?        → ALLOW (no build)
│   ├── no in-project file mentions?         → ALLOW (no scope)
│   └── build contract → store → ALLOW
│
└── PreToolUse:
    ├── not Edit/Write/MultiEdit/NotebookEdit? → ALLOW
    ├── target_file is None?                   → ALLOW
    ├── session_id is None?                    → ALLOW
    ├── no contract for session?               → ALLOW
    ├── target_file in allowed_files?          → ALLOW
    └── out of scope:
        ├── mode = "warn" → WARN
        └── mode = "block" → BLOCK with original prompt + scope details
```

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-prompt-non-edit event | < 50 µs |
| `evaluate()` UserPromptSubmit (build) | < 5 ms |
| `evaluate()` PreToolUse (enforce) | < 1 ms |

The build path runs Hero 9's classifier + file extractor (regex; sub-ms) plus path resolution per mention (filesystem call, ~µs each). The enforce path is dict lookup + set membership.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| User submits prompt without mentioning a file | No scope built. AI free to edit whatever it concludes is needed (no enforcement). |
| User mentions `auth.py` but the AI also touches `auth_helpers.py` | Out of scope → block (mode=block) or warn. User can re-prompt with both files. |
| User submits a follow-up prompt mid-session | New contract replaces the old one. AI can edit the new scope without restriction. |
| Session has no `session_id` in the event | Allow silently (can't look up contract). |
| Contract is older than TTL | Evicted on read; treated as missing → allow. |
| `target_file` resolves outside `project_root` (Bug-5 shape) | Wiring layer already strips it (target_file = None) so we hit the "target_file is None → allow" branch. |
| MultiEdit with files inside AND outside scope | Today the wiring layer joins all edits into one diff for Hero 7's purposes; Hero 3 only sees `target_file` (the file path passed in tool_input). For MultiEdit, that's a single file. ✓ |
| User intentionally wants AI to expand scope | Two paths: (1) re-prompt with new files; (2) `mode=warn` for advisory-only; (3) `mode=off` to disable per session. |
| Path-traversal in prompt (`"fix '../../etc/passwd.py'"`) | Bug-5 defense: out-of-project mentions stripped at contract-build time. The contract has 0 allowed files → no contract built → allow. |
| Two sessions in parallel (e.g., two terminals) | Each `session_id` gets its own contract. No cross-session interference. |

---

## Acceptance test list

12 scenarios:

1. mode=off disables policy (no signal calls)
2. UserPromptSubmit with no file mentions → no contract built
3. UserPromptSubmit with intent=test/docs → no contract built
4. UserPromptSubmit with intent=fix-bug + file mention → contract stored
5. PreToolUse Edit on in-scope file → allow
6. PreToolUse Edit on out-of-scope file (mode=block) → block with message
7. PreToolUse Edit on out-of-scope file (mode=warn) → warn (not block)
8. PreToolUse without prior UserPromptSubmit → allow (no contract)
9. Path-traversal mention in prompt → not in contract's allowed_files
10. **All 4 _EDIT_TOOLS** trigger enforcement equally (Bug-7 lesson)
11. End-to-end through claude_code_hooks for both UserPromptSubmit AND PreToolUse
12. Contract TTL: contract older than max_age → not enforced

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/scope_contract.py` | The `ProactiveScopeContractLock` policy |
| `tests/engine/test_scope_contract.py` | Acceptance + behavioral + mutation tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/scope_contract.py` | Replace stub with real per-session storage + TTL eviction |
| `mcp_server/engine/__init__.py` | Add to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export |

---

## QA gate (Tier-0 + deep-audit from start)

Lessons #15-21 applied:

- ✅ **Real-DB integration**: while Hero 3's state is in-memory (process-global dict), tests use real `claude_code_hooks` wiring + real signals
- ✅ **Behavioral spies**: track contract build + enforce calls
- ✅ **End-to-end dispatch**: register all 9 heroes, fire UserPromptSubmit then PreToolUse, assert block
- ✅ **End-to-end through `claude_code_hooks.handle()`** for BOTH events (Bug-4 lesson)
- ✅ **All 4 _EDIT_TOOLS through wiring** (Bug-7 lesson)
- ✅ **Path-traversal probe** for prompt file mentions (Bug-5 lesson)
- ✅ **Content-verifying assertions** — block message must contain the OFFENDING file path AND the original prompt (Bug-6 + Lesson #19)
- ✅ **Bug-X-shape audit** — every contract field exercised; the TTL works; mode=off truly disables
- ✅ 10+ mutations from start

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Mis-classified intent leads to wrong scope | High at first | High | **Off by default**. Mode=warn is the recommended onboarding mode. Users can re-prompt anytime. |
| User frustrated by false-positive blocks | High | High | Mode=warn first; mode=block requires explicit opt-in. Block message includes the original prompt and instructions to extend scope. |
| File-mention regex misses a legitimate file | Medium | Low | Allow falls through to existing safety nets. User re-prompts. v2.1 adds LLM-based file inference. |
| TTL too short / contract expires during long task | Medium | Low | Default 1h is generous. Configurable. New prompt rebuilds. |
| Multiple sessions interfere | Low | Medium | session_id keying isolates them. Tested. |
| Bug-5-shape: path traversal in prompt | Low | High | Defense applied at contract-build time. Tested. |
| Bug-7-shape: not all _EDIT_TOOLS enforced | Medium | High | Use `event.is_edit()` (covers all 4). End-to-end test through wiring for each. |
| Bug-X: contract built but enforce silently no-ops | Low | High | Behavioral spies in tests verify build → enforce → block, not just verdict shape. |

---

## Out of scope (deferred)

- **LLM-based intent + scope parsing** — v2.1 with optional `CODEVIRA_SCOPE_LOCK_LLM` env var (local Ollama).
- **Glob support** in `allowed_files` (e.g., user prompt: "fix all auth_*.py files") — v2.1.
- **Auto-extend scope** when user re-prompts adding "also" — v2.1.
- **GUI to review/edit contract before enforcement** — needs MCP Apps; Hero 8 territory.
- **LOC-delta enforcement** — informational only in v2.0-alpha; v2.1 enforces.
- **Cross-tool enforcement** (e.g., user prompts in Claude Code, Edit happens in Cursor) — v2.1; needs cross-process state.

---

## Definition of done

- [ ] `ProactiveScopeContractLock` policy registered + enabled by default (off-by-default mode means it's silent unless user opts in).
- [ ] All 12 acceptance tests pass.
- [ ] Tier-0 + deep-audit probes clean.
- [ ] All 4 _EDIT_TOOLS tested through wiring (Bug-7 lesson).
- [ ] Path-traversal probe (Bug-5 lesson) tested.
- [ ] Block message asserts both the offending file AND the original prompt (Bug-6 + Lesson #19).
- [ ] At least one end-to-end test through `claude_code_hooks.handle()` for both UserPromptSubmit AND PreToolUse against a real graph DB.
- [ ] Proactive Week-12 integration QA round (don't wait for user to ask).
- [ ] No new Bug-class issues.
- [ ] `docs/v2-execution-log.md` Week-12 entry written.
