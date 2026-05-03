# Hero 6 — Token Budget Live View

> "How many tokens did this session burn? Where did they go? Was anything wasted?"

The fourth policy hero, but the smallest one yet — most of the work is already done. Week-2 built `_persist_session_summary` (writes `token_budget.jsonl`) and `read_session_history` (reads it back). Hero 6 is just two thin pieces:

1. A `Stop` policy that triggers persistence at session end
2. A `codevira budget` CLI command that reads the log

Sprint week: **Week 7**. Tier-0 pre-flight from start: real-DB integration tests + behavioral spies + 10+ mutations are the BASELINE, not the ceiling.

---

## Problem statement

When the AI session ends, the user has no idea what they spent. Codevira already MEASURES — `TokenMeter.record_injected` and `record_used` instrument every tool response. What's missing:

1. **Persistence at end-of-session.** The meter exists in memory only until `end_session()` is called with `project_root`. Without a `Stop`-event policy, that call never happens; meter data is lost.
2. **A way to read it back.** The `read_session_history` function exists; nothing calls it from a user-facing surface.

Hero 6 wires both ends. Pure plumbing — no signal queries, no policy decisions, just observable telemetry.

---

## User pain (concrete example)

**Without Hero 6:**

```text
[After 4 hours of Claude Code work]
User: "I wonder how token-heavy that was..."
User: $ codevira budget
codevira: command not found  (or: "no such command")

[2 weeks later, AI bill arrives]
User: "Why was last week so expensive? Was it that big refactor session?"
[no way to find out — codevira logged nothing]
```

**With Hero 6:**

```text
[Session ends]
[Stop hook fires; codevira persists the summary to ~/.codevira/.../token_budget.jsonl]

[Later]
User: $ codevira budget
codevira:
  Last session (2 hours ago):    8,247 tokens injected, 4,102 used (49.7%)
    Top wasted sources:
      get_node:        2,400 tokens injected, 480 used  → 80% wasted
      search_decisions:  1,200 tokens injected, 600 used → 50% wasted

User: $ codevira budget history --last 7
codevira:
  2026-05-01  session-x   1,400 inj   720 used  (51%)
  2026-05-02  session-y   8,247 inj  4,102 used (50%)
  ...
```

The win: **token cost becomes legible**. The user can see WHERE the spend went and decide whether to optimize.

---

## Mechanism

### Two surfaces

**1. Stop policy** — runs on the `STOP` lifecycle event. Pulls the current session's `TokenMeter` summary and calls `end_session(session_id, project_root=event.project_root)` to persist. The meter is then dropped from in-memory state.

```python
class TokenBudgetPersist(Policy):
    name = "token_budget_persist"
    handles = (EventType.STOP,)
    enabled_by_default = True
    priority = 10  # very low; this is post-session telemetry

    def evaluate(self, event, signals=None):
        if event.event_type != EventType.STOP:
            return PolicyVerdict.allow()

        session_id = event.session_id
        if session_id is None:
            return PolicyVerdict.allow()  # no session to persist

        try:
            from mcp_server.engine.token_meter import end_session
            summary = end_session(
                session_id,
                project_root=event.project_root,
            )
        except Exception:
            return PolicyVerdict.allow()  # never crash on telemetry

        if summary is None:
            return PolicyVerdict.allow()

        return PolicyVerdict.allow(metadata={
            "policy": self.name,
            "persisted": True,
            "session_id": session_id,
            "injected_total": summary.get("injected_total", 0),
            "used_total": summary.get("used_total", 0),
            "efficiency": summary.get("efficiency", 0.0),
        })
```

`STOP` policies don't surface anything to the AI; this is observable via `codevira doctor`/logs and via the persisted JSONL.

**2. `codevira budget` CLI** — three subcommands:

| Command | Output |
|---|---|
| `codevira budget` | Most-recent session summary: total injected/used + efficiency + top wasted sources |
| `codevira budget history` | Last 10 sessions, one per line, with totals |
| `codevira budget history --last N` | Last N sessions (clamped 1-100) |

Each subcommand reads `~/.codevira/projects/<key>/logs/token_budget.jsonl` via `read_session_history`. Pure read; no mutation.

### Decision tree (Stop policy)

```
STOP event arrives
│
├── event.event_type != STOP?              → ALLOW (defensive)
├── event.session_id is None?              → ALLOW (no session to persist)
│
├── end_session() raises?                  → ALLOW (never crash on telemetry)
├── end_session() returns None?            → ALLOW (no meter for that session)
│
└── persisted summary
    └── ALLOW with metadata (the engine logs it; user sees via doctor)
```

Notably, this policy NEVER blocks/warns/injects. It's pure persistence side-effect.

### CLI surface

```text
codevira budget [history] [--last N] [--full] [--project PROJECT]
```

Flags:
- `(no args)` → show most-recent session summary, with top-3 wasted sources
- `history` → list last 10 sessions one-per-line
- `--last N` → last N sessions (clamp 1-100)
- `--full` → show full per-source breakdown for each session (default: top-3 wasted only)
- `--project PROJECT` → cross-project: read another project's log instead of cwd's

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_TOKEN_BUDGET_MODE` | `persist` | `off` disables persistence (Stop policy no-ops) |

That's it for v2.0-alpha.2. The threshold-watching / live-counter UI is deferred to v2.1 (needs an MCP-Apps interactive renderer).

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for a non-STOP event | < 50 µs (one event-type check) |
| `evaluate()` for STOP — meter exists | < 5 ms (one JSONL line write + meter cleanup) |
| `evaluate()` for STOP — no meter | < 0.5 ms (early return) |
| `codevira budget` (most recent) | < 100 ms (read tail of JSONL) |
| `codevira budget history --last 100` | < 200 ms (read 100 lines max — bounded by Week-2 cap) |

The Stop policy fires at session end where the user is no longer waiting. Even 50 ms here is invisible.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Session never ran any tools (no meter) | end_session returns None → policy returns allow with no metadata |
| `token_budget.jsonl` doesn't exist yet | `codevira budget` says "no sessions recorded yet"; exits 0 |
| `token_budget.jsonl` is corrupt JSON | `read_session_history` already skips malformed lines (Week 2 guarantee) |
| Disk full during persist | Caller (this policy) silently allows; `read_session_history` may see truncated log on next read; Week-2 atomicity protections apply |
| User runs `codevira budget` from a non-project dir | Friendly error: "no codevira project at <cwd> — try cd into one or use `--project /path`" |
| Concurrent sessions on the same project (multi-IDE) | Pillar-1 idempotency contract: single-writer-per-project. Each session writes its own line on Stop; no contention |
| Session ends without ever calling record_injected/record_used | Meter has zero totals; persisted as `{ "injected_total": 0, "used_total": 0, ... }` |
| User has 10,000+ historical sessions | `read_session_history` already caps at 16 MiB tail (Week-2 R1 finding); old sessions past cap are not returned |
| Time travel: session ends at t=0, persisted; second session same minute | Two records, distinguishable by `session_id` |
| `--last 0` or negative | Clamped to 1 |
| `--last 999999` | Clamped to 100 |

---

## Acceptance test list

8 scenarios:

1. **Stop event without session_id** → allow, no persistence
2. **Stop event with valid session_id and active meter** → allow with `persisted=True` metadata + JSONL line written
3. **Stop event with active meter but disk full (simulated)** → allow gracefully, no crash
4. **Non-STOP event passes through** → allow, no end_session call
5. **`codevira budget` with no sessions yet** → friendly empty-state message, exit 0
6. **`codevira budget` after one session persists** → output shows the session's totals + top wasted
7. **`codevira budget history --last 5`** with 10 sessions → returns 5 newest
8. **Performance: Stop policy evaluate p99 < 5 ms over 100 trials**

Tests live in `tests/engine/test_token_budget.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/policies/token_budget.py` | TokenBudgetPersist policy |
| `mcp_server/cli_budget.py` | `codevira budget` command implementation |
| `tests/engine/test_token_budget.py` | Acceptance + behavioral + mutation regression tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `TokenBudgetPersist` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export the new policy |
| `mcp_server/cli.py` | Add `codevira budget` subcommand wiring |

### No change

- `mcp_server/engine/token_meter.py` — Week-2 plumbing reused as-is
- `mcp_server/engine/wiring/claude_code_hooks.py` — Stop hook already fires on `STOP` event
- `mcp_server/data/hooks/stop.sh` — already invokes `codevira engine handle Stop`

---

## QA gate (Tier-0 pre-flight)

Per the Week-5 retrospective lessons (#15, #16, #17), every angle must be EXERCISED, not just checked off. Tier-0 pre-flight asks (before declaring done):

- **Did each fake-signals test honor the args it claims to test?** (Week-5 lesson — fakes that ignore args hide mutations)
- **Did the test exercise the REAL data path?** (Hero 6's path is `end_session` → JSONL append → `read_session_history`. Tests must use a real temp dir + real JSONL file, not mocks.)
- **Are gates verified BEHAVIORALLY** (spy on `end_session`), not just by output equivalence?
- **Are mutations tested at every gate**, not just the happy path?
- **Is there at least ONE end-to-end test through `dispatch()`** that fires the Stop policy on a real session?
- **Is the CLI tested via subprocess**, not just direct function call?

If any of these are "no," the round isn't done. Document why with concrete evidence.

The R1-R8 plan:

- **R1** code review + security audit (independent agent)
- **R2** integration completeness (Stop policy registered, CLI wired) + type safety
- **R3** broader mutation testing (10+ mutations from start)
- **R4** adversarial vs the implementation
- **R5** cross-module impact (Hero 6 + existing 3 heroes coexist)
- **R6** latency + concurrency (multiple Stop events in parallel)
- **R7** edges (corrupt JSONL, disk full, no meter, large history)
- **R8** real-DB / real-CLI / live observation — every angle through real code paths

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Persist failure silently loses session data | Medium | Low | Documented behavior — telemetry is best-effort. `codevira doctor` surfaces persist failures. |
| User runs `codevira budget` and confuses "0 tokens" with "broken instrumentation" | Low | Low | Output explicitly says "no instrumentation calls were made this session" when injected_total = 0 |
| JSONL grows unboundedly | Already mitigated | — | Week-2 R1 finding: `read_session_history` caps at 16-MiB tail. `codevira budget` doesn't blow up on huge logs. |
| Stop policy crashes mid-cleanup | Low | Medium | Wrapped in try/except per the engine contract; runner safety net catches. |
| Multi-process concurrent persist | Already mitigated | — | Week-2 R3 atomic-write fix + `_persist_session_summary` partial-line guard. |

---

## Out of scope (deferred)

- **Live in-session token counter** (terminal display while session is active). Needs MCP-Apps integration; Hero 8 territory.
- **Per-tool token-budget alerts** ("you've spent 80% of budget on get_node"). Needs cost model + threshold config; v2.1.
- **Cost in dollars** (multiply token count by model rate). Needs pricing table + per-model awareness; v2.1+.
- **`codevira budget reset`** to clear history. Defer until a user asks.
- **Project-cross-cutting "budget across all projects"** view. Defer.

---

## Definition of done

- [ ] `TokenBudgetPersist` policy registered in default heroes.
- [ ] `codevira budget` CLI subcommand works (no args, history, --last N).
- [ ] All 8 acceptance tests pass.
- [ ] R1-R8 gauntlet clean — Tier-0 pre-flight applied to each round.
- [ ] At least one end-to-end test: Stop event through `dispatch()` → JSONL persisted → `read_session_history` returns it.
- [ ] At least one CLI subprocess test that exercises `codevira budget` end-to-end.
- [ ] `docs/v2-execution-log.md` Week-7 entry written.
- [ ] v2.0-alpha.2 plan updated to bundle Heroes 1, 5, 6.
