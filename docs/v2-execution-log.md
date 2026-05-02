# Codevira v2.0 — Execution Log

> Running journal of what shipped, what changed, what we learned. Updated at the end of each hero sprint and at every public alpha checkpoint. Brief — full design rationale lives in per-hero specs (`docs/heroes/`).

---

## How to read this log

- **By week** for chronology.
- **By hero** for deeper view of any specific feature's evolution.
- **Each entry**: ~1 page max. Keep it scannable.
- **Always include**: what shipped, what surprised us, what we'd do differently, what's next.

---

## Week 0 — Planning lockdown (2026-05-03)

### Decisions made tonight

- **v1.9 → v2.0 rename.** Scope grew to 10 heroes + new engine architecture + universal multi-tool coverage. Major version bump is semver-correct and signals magnitude to users.
- **Vision locked**: per-project memory across every AI tool the developer uses. NOT universal cross-project memory.
- **All 10 heroes ship in v2.0 GA**, not staged across releases. Founder chose "all-in-one" over staged approach. 14-week timeline accepted.
- **Per-hero focused planning.** This master plan stays high-level; each hero gets `docs/heroes/NN-name.md` written *just before* implementation. Solo founder; one hero at a time.
- **Success metric**: founder personally using codevira daily on own projects 60 days post-GA. Stars/users are secondary.

### Cofounder pushbacks recorded (rejected by founder, on record)

1. Argued for staged shipping (4 heroes in v2.0, rest in v2.0.x patches over 3-6 months). Founder chose all-in-one.
2. Argued for separately shipping v1.8.1 hotfix to existing users (small but real risk of further crash logs). Founder chose to fold into v2.0.
3. Argued for engine umbrella name (Guardian/Sentinel) for marketing surface. Founder chose to keep brand as "codevira" only.

### Risks acknowledged

- Anthropic could ship native Claude Code memory inside the 14-week window
- 14 weeks of building blind without v2.0 GA — mitigated by alpha cadence
- Solo-founder energy at week 12+ is the historical danger zone

### What's next

- Week 1: Engine sprint begins. Build hook intercept layer, signal aggregation, policy plugin API.
- Week 2: Engine acceptance criteria green; Pillar 1 (UX install) work starts.
- Week 3: alpha.1 tag — Engine + Pillar 1 + Hero 4 (Blast-Radius).

### Tonight's deliverables

- ✅ `docs/v2-master-plan.md`
- ✅ `docs/heroes/00-engine.md`
- ✅ `docs/heroes/README.md` (index)
- ✅ `docs/v2-execution-log.md` (this file)

---

## Week 1 — Engine sprint, part 1 (2026-05-03)

### Shipped

**Engine core** (`mcp_server/engine/`):
- `__init__.py` — public API (`Policy`, `PolicyVerdict`, `HookEvent`, `EventType`, `register_policy`, `dispatch`, `registered_policies`, `reset_policies`)
- `events.py` — frozen `HookEvent` dataclass, 5 `EventType` values (PRE_TOOL_USE, POST_TOOL_USE, SESSION_START, USER_PROMPT_SUBMIT, STOP), convenience predicates (`is_edit`, `is_read`)
- `policies.py` — `Policy` base class + `PolicyVerdict` with allow/warn/block/inject constructors
- `signals.py` — `SignalContext` lazy accessor wrapping graph, decisions, fixes, preferences, token_budget, scope_contract, current_session
- `runner.py` — `dispatch` + verdict combination rules, registry mgmt, exception isolation, `CODEVIRA_ENGINE=0` kill switch, p95 budget tracking

**Helper subsystems**:
- `engine/token_meter.py` — per-session `TokenMeter` (thread-safe), `get_or_create_session_meter`, `end_session`, `reset_meters`
- `engine/scope_contract.py` — interface stub for Hero 3 (`current_contract`, `set_current_contract`)
- `indexer/fix_history.py` — minimal SQLite-backed fix log (`record_fix`, `lookup`, `is_revert` heuristic with proper word-boundary matching)

**Wiring adapters** (`mcp_server/engine/wiring/`):
- `claude_code_hooks.py` — translates Claude Code hook stdin JSON → `HookEvent` → `dispatch` → `{continue, stopReason, ...}` JSON on stdout, exit 0/2 per protocol
- `mcp_dispatch.py` — `pre_call(tool_name, args)` and `post_call(tool_name, args, output)` for the existing MCP `call_tool` to invoke

**Hook scripts** (`mcp_server/data/hooks/`):
- 5 executable shell scripts (`pre_tool_use.sh`, `post_tool_use.sh`, `session_start.sh`, `user_prompt_submit.sh`, `stop.sh`)
- Each is a 1-liner: `exec codevira engine handle <EventName>`. Performance budget naturally met since all logic is Python-side.

**CLI wiring**:
- New `codevira engine handle <event-type>` subcommand in `mcp_server/cli.py` — invoked by hook scripts, never user-facing.

**Demo policy** (`mcp_server/engine/demo_policy.py`):
- `BackupExtensionGuard` blocks Edit/Write of `.py.bak` files when `CODEVIRA_DEMO_POLICY=1` is set in env. Acceptance test target only — deleted before v2.0 GA.

**Tests** (`tests/engine/`):
- 73 passing unit + integration tests
- Coverage: events immutability + predicates, policy verdicts, registration semantics, verdict combination rules (block > warn > inject > allow), event-type filtering, exception isolation, kill switch, priority ordering, signals attached to event, token meter accounting + thread safety, fix history record/lookup/heuristic, demo policy through both Claude Code wiring AND MCP dispatch wiring
- Full suite: 1,531 pass / 0 fail (was 1,458 pre-engine + 73 new)

**End-to-end smoke test** (with `pipx install` of new wheel):
- ✅ `.py.bak` edit + demo ON → blocks (exit 2, JSON `continue: false` + stopReason)
- ✅ normal `.py` edit + demo ON → allows (exit 0)
- ✅ `.py.bak` edit + demo OFF → allows (exit 0; policy not registered)

### Acceptance criteria status (from `docs/heroes/00-engine.md`)

- ✅ All 5 hook event types dispatch to registered policies
- ✅ Verdict combination rules pass property-style tests
- ✅ Performance: per-policy slow-eval warning fires above 100 ms; engine itself is thin pass-through under 5 ms
- ✅ Demo policy registers and works end-to-end through Claude Code wiring AND MCP dispatch wiring
- ✅ Crash in one policy doesn't break others (`test_runner.py::TestErrorHandling`)
- ✅ Engine kill switch via `CODEVIRA_ENGINE=0` env var works
- ✅ Token meter records every tool response (interface ready; full instrumentation Week 2)
- 🟡 Fix history detects fix commits in agent-mcp's git log (smoke test — basic; git scanning is Week 2 work as planned)

### Surprises

- **Frozen-dataclass + signals attribute trick.** HookEvent is frozen for policy safety, but the runner needs to attach a SignalContext per-event. Solved with `object.__setattr__` — one synthetic attribute, never a state-carrying field. Documented in `runner.py`.
- **`@@ -10` substring matched `@@ -100`.** Caught by an "unrelated diff" test in test_fix_history.py. Fixed with proper unified-diff hunk-header regex (`@@ -<start>(?:,| )`).
- **No regressions.** All 1,458 v1.8.1-era tests still pass. The engine landed cleanly without touching any v1.8.1 code paths.

### What changed in the spec

- **Hooks shell out to `codevira engine handle <Event>`** rather than directly importing Python — keeps hook scripts trivial and lets us version Python logic independent of installed hook scripts. `~/.claude/hooks/codevira-*` is the namespace; written by `codevira hooks install` (Week 3).
- **`SignalContext` exposes `is_revert` access via `signals.fixes(file)`** rather than requiring policies to import `indexer.fix_history` directly. Cleaner abstraction; policies only know about the engine.

### Founder dogfood notes

- Not yet — engine isn't installed in any project's hook config. Pillar 1 (Week 3) ships `codevira hooks install`; that's when dogfood begins.

### Open questions / decisions deferred

- Real performance benchmarks under 10 registered policies — Week 2 deliverable (`tests/engine/test_perf.py`).
- Git fix-detection for `indexer/fix_history.py` — wire in Week 2.
- `codevira config policy <name> <key> <value>` CLI subcommand for per-policy enabling — Week 3 (Pillar 1).

### Next week (Week 2)

- Performance test under realistic load (10 policies, 100 events each)
- Git fix-detection helper for `fix_history.py` (scans last 1000 commits, regex-matches subjects)
- Token meter persistence to `<data_dir>/logs/token_budget.jsonl`
- Edge-case tests (huge diffs, missing project_root, malformed Claude Code input)
- Engine acceptance criteria fully green
- Begin Pillar 1 (UX install) prep work — `codevira setup` design

---

## Week 2 — Engine sprint, part 2

_(to be filled in)_

---

## Week 3 — Pillar 1 (UX install)

_(to be filled in)_

---

## Week 4 — Hero 4 (Blast-Radius Veto) → alpha.1

_(to be filled in)_

---

## Template for new entries

```markdown
## Week N — <topic>

### Shipped
- ...
- ...

### Surprises
- something we didn't expect

### What changed in the spec
- explicit revisions to docs/heroes/NN-name.md

### Founder dogfood notes
- where it helped on actual work
- where it got in the way

### Open questions / decisions deferred
- ...

### Next week
- ...
```
