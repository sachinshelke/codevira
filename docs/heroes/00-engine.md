# Hero 0 — The Engine (shared infrastructure for all 10 policies)

> The engine has no user-facing pitch. Users only see Heroes 1-10. But the engine **is** the architecture: build it once, and the 10 heroes become ~100-300 LOC each. Build it badly and every hero becomes 1000-LOC duplication.

This is written first because Heroes 1-10 all depend on it. Two weeks budgeted (Weeks 1-2 of v2.0).

---

## What the engine is

A small Python package that:

1. **Intercepts AI tool calls** at MCP/stdio + Claude Code lifecycle hook boundaries.
2. **Aggregates signals** from existing codevira data sources (graph, decisions, prefs) and new ones (fix history, scope contract, token budget).
3. **Runs registered policies** that decide allow / warn / block / log on each interception.
4. **Returns a verdict** that the hook layer translates into the right action (block edit, inject context, log decision, etc.).

The engine does not know what a "Decision Lock" is. It just runs whatever policies are registered. Policies are pluggable.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                                                                    │
│  Claude Code              MCP server tool dispatch                 │
│  lifecycle hooks          (existing)                               │
│  (NEW shell scripts)      (existing call_tool handler)             │
│       │                              │                             │
│       └──────────────┬───────────────┘                             │
│                      ▼                                             │
│  ┌────────────────────────────────────────────────────┐            │
│  │   codevira.engine.dispatch                         │            │
│  │   • normalizes input into a HookEvent              │            │
│  │   • collects relevant Signals                      │            │
│  │   • runs all registered Policies                   │            │
│  │   • combines verdicts (any block → blocks)         │            │
│  └────────────┬───────────────────────────────────────┘            │
│               │                                                    │
│               ▼                                                    │
│       ┌───────────────────┐                                        │
│       │  PolicyVerdict    │ → block/warn/allow + message + signal  │
│       └───────────────────┘                                        │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

Three new files in `mcp_server/engine/`:
- `__init__.py` — public API (`register_policy`, `dispatch`)
- `events.py` — `HookEvent` dataclass + event-type enum
- `signals.py` — `SignalContext` accessor wrapping existing data sources
- `policies.py` — `Policy` base class + `PolicyVerdict` dataclass
- `runner.py` — orchestrates signal collection + policy execution
- `wiring/claude_code_hooks.py` — adapters that plug Claude Code hook scripts into `engine.dispatch`
- `wiring/mcp_dispatch.py` — adapters for the existing MCP server's `call_tool`

---

## Hook event types

All policies receive one of these:

| Event type | When it fires | What it carries |
|---|---|---|
| `pre_tool_use` | Before any AI tool/Edit/Write/etc. runs | Tool name, args, target file(s), proposed diff (if available) |
| `post_tool_use` | After tool runs | Tool name, args, result, side effects (files changed) |
| `session_start` | New AI session begins | Project root, AI tool identity (Claude/Cursor/etc.) |
| `user_prompt_submit` | User sends a prompt to the AI | Prompt text, project root, prior session id |
| `stop` | AI session ends | Session id, summary of what happened |

Event objects are immutable dataclasses. Policies can't mutate them — only return verdicts.

---

## Signals (what policies can read)

Every event includes a `SignalContext` lazy-accessor for:

| Signal | Source | Notes |
|---|---|---|
| `graph` | `indexer.sqlite_graph.SQLiteGraph` (existing) | Lazy: only loaded if a policy asks |
| `impact(file)` | `tools.graph.get_impact` (existing) | Cached per-event |
| `decisions(filters)` | `tools.search.search_decisions` (existing) | Filters: file, recency, locked-only |
| `fixes(file)` | NEW `indexer.fix_history` | Built in engine sprint; see "Fix history" below |
| `preferences(category)` | `tools.learning.get_preferences` (existing) | |
| `token_budget` | NEW `engine.token_meter` | Per-session counter; see "Token meter" |
| `scope_contract` | NEW `engine.scope_contract` | Per-session; populated by Hero 3 only |
| `current_session` | `tools.search.get_session_context` (existing) | |

Lazy loading is non-negotiable: a policy that doesn't ask for `graph` doesn't pay the SQLite query.

---

## Policy plugin API

```python
from codevira.engine import Policy, HookEvent, PolicyVerdict, register_policy

class DecisionLockPolicy(Policy):
    name = "decision_lock"
    handles = ["pre_tool_use"]
    enabled_by_default = True

    def evaluate(self, event: HookEvent) -> PolicyVerdict:
        if event.tool_name not in {"Edit", "Write", "MultiEdit"}:
            return PolicyVerdict.allow()

        target = event.signals.target_file
        decisions = event.signals.decisions(file=target, locked_only=True)
        if not decisions:
            return PolicyVerdict.allow()

        # Check whether the proposed diff would revert any locked decision
        violated = self._find_violated_decisions(event.proposed_diff, decisions)
        if violated:
            return PolicyVerdict.block(
                message=f"Edit blocked. Decision #{violated[0].id} is locked: ...",
                policy="decision_lock",
                metadata={"decisions_violated": [d.id for d in violated]},
            )
        return PolicyVerdict.allow()

register_policy(DecisionLockPolicy())
```

### `Policy` base class

| Member | Purpose |
|---|---|
| `name: str` | Stable identifier (snake_case). Used for config, logs, metrics. |
| `handles: list[EventType]` | Which event types to receive. |
| `enabled_by_default: bool` | Default-on or opt-in? |
| `priority: int = 0` | Higher = runs first. Used for fast-rejection chains. |
| `evaluate(event) -> PolicyVerdict` | The actual policy logic. |
| `config_schema()` | Optional: JSON-schema-like dict describing user-facing config knobs. |

### `PolicyVerdict`

```python
@dataclass
class PolicyVerdict:
    action: Literal["allow", "warn", "block", "inject"]
    message: str | None = None       # shown to user / AI
    inject_context: str | None = None  # for "inject" — string added to AI prompt
    policy: str | None = None        # auto-filled with policy name
    metadata: dict = field(default_factory=dict)  # arbitrary extras
```

---

## Verdict combination rules

When multiple policies return verdicts on the same event:

1. **Any `block` → final verdict is block.** First block wins (by priority order).
2. **Otherwise, all `warn` are concatenated** in priority order.
3. **All `inject` are concatenated** in priority order, then prepended to the AI's context.
4. **Otherwise allow** (the default).

Verdicts are returned to the hook layer, which translates them:
- `block` → exit non-zero from the hook script (Claude Code interprets as blocked tool call)
- `warn` → log + non-blocking notification (no AI behavior change)
- `inject` → write to the hook's stdout (Claude Code includes it in next turn)
- `allow` → silent pass-through

---

## New helper subsystems (built as part of engine sprint)

### Fix history (`indexer/fix_history.py`)
Tracks "this region of code is the fix for bug X" so Hero 2 (Anti-Regression) can warn on reverts. Sources:
- Git commits with messages matching `/^fix(.*)?:|^bug(.*)?:|fixes #\d+/i`
- Explicit `codevira fix-noted` CLI command (user flags after a manual fix)
- Stored in `<data_dir>/graph/fixes.db` (separate SQLite for clean migration)

Methods:
- `record_fix(file, region, description, source: 'git' | 'manual', commit_sha=None)`
- `lookup(file, lines) -> list[FixRecord]`
- `is_revert(proposed_diff, fix_record) -> bool` — heuristic: does the diff move the file's content towards the pre-fix state?

### Token meter (`engine/token_meter.py`)
Per-session counter for tokens injected into AI context vs tokens actually used.
- Wraps every tool response with metadata: `{"tokens_injected": N, "tool": "get_node", ...}`
- `record_used(token_count)` called by Hero 6's PostToolUse policy when the AI references a tool's output.
- `summary()` returns `{injected, used, efficiency, top_wasted_tools}`.
- Persisted in-memory per session; flushed to `<data_dir>/logs/token_budget.jsonl` on session end.

### Scope contract (`engine/scope_contract.py`)
Per-session structure populated by Hero 3 only. If Hero 3 isn't enabled, this signal returns `None` and other policies ignore it.
- Schema: `{allowed_files: list[str], allowed_change_types: list, max_loc_delta: int, ...}`
- `evaluate_change(target_file, diff) -> Literal["in_scope", "out_of_scope"]`

---

## Configuration (per-policy, per-project)

Configuration lives in `<data_dir>/config.yaml` under a `policies` block:

```yaml
project:
  name: my-project
  watched_dirs: [src]
  file_extensions: [.py]

policies:
  decision_lock:
    enabled: true
    strictness: hard  # hard | warn | log
  anti_regression:
    enabled: true
  scope_contract:
    enabled: false  # opt-in, false by default
    max_loc_delta: 50
  blast_radius:
    enabled: true
    threshold_callers: 5
    threshold_files: 3
  cross_session:
    enabled: true
  token_budget:
    enabled: true
    show_in_terminal: true
  live_style:
    enabled: true
    auto_fix: false
  decision_replay:
    enabled: false  # only renders on MCP-Apps-capable clients anyway
  intent_inference:
    enabled: false  # opt-in (LLM call cost)
  ai_promotion:
    enabled: true
```

`codevira config policy <name> <key> <value>` (NEW CLI) for setting these.

---

## Performance budget

Hooks fire on EVERY tool call. They must be fast.

| Event type | p95 target | p99 target |
|---|---|---|
| `pre_tool_use` | 50 ms | 200 ms |
| `post_tool_use` | 50 ms | 200 ms |
| `session_start` | 500 ms | 1000 ms (more leeway: only fires once per session) |
| `user_prompt_submit` | 100 ms | 300 ms |
| `stop` | 1000 ms | (best-effort; AI session is over) |

Mechanisms to hit these:
- Lazy signal loading — policies that don't read graph don't trigger graph queries
- Per-event signal caching — multiple policies asking for `decisions` get one query
- Async policy execution where order doesn't matter (warn/inject combine; block short-circuits)
- Fast-rejection: policies declare `handles` event types so engine skips ones they don't care about

---

## Testing strategy

### Unit
- `tests/engine/test_dispatch.py` — verdict combination rules
- `tests/engine/test_signals.py` — lazy loading, caching
- `tests/engine/test_policy_base.py` — Policy lifecycle
- `tests/engine/test_token_meter.py` — accounting accuracy
- `tests/engine/test_fix_history.py` — git fix detection patterns

### Integration
- `tests/engine/test_claude_code_wiring.py` — fake Claude Code hook input → engine → expected verdict output
- `tests/engine/test_mcp_dispatch_wiring.py` — same for MCP tool dispatch path
- `tests/engine/test_perf.py` — p95 measured under realistic load (10 policies, 100 events)

### Property
- `tests/engine/test_policy_combinator.py` — hypothesis-based: any combination of N policies returns a deterministic verdict

---

## Edge cases to handle

1. **Policy raises an exception** → engine catches, logs to crash_logger, returns `allow` for that policy. Other policies still run. One bad policy doesn't break the whole hook.
2. **Two policies disagree on `inject`** — concatenate in priority order, deduplicate identical lines.
3. **PreToolUse fires but tool name is unknown** (new tool the AI is trying) → policies that don't handle it return `allow`; engine falls through to allow.
4. **Hook script can't reach codevira CLI** (codevira not in PATH) → hook exits 0 (allow) with a stderr warning. Never block on infrastructure failure.
5. **User has policies disabled but hook is registered** → hook short-circuits to allow with no work. Latency target: <5 ms.
6. **Diff is too large to analyze** (>10 MB) → policies that need diff-inspection bail with allow + warning. Don't time out the hook.

---

## Deliverables (engine sprint, weeks 1-2)

| Artefact | Owner |
|---|---|
| `mcp_server/engine/{__init__,events,signals,policies,runner}.py` | code |
| `mcp_server/engine/wiring/claude_code_hooks.py` | code |
| `mcp_server/engine/wiring/mcp_dispatch.py` | code |
| `indexer/fix_history.py` | code |
| `mcp_server/engine/token_meter.py` | code |
| `mcp_server/engine/scope_contract.py` (interface only; populated by Hero 3) | code |
| `mcp_server/data/hooks/session_start.sh`, `pre_tool_use.sh`, `post_tool_use.sh`, `user_prompt_submit.sh`, `stop.sh` | code |
| Unit + integration tests | code |
| `docs/heroes/00-engine.md` | this file |
| `docs/v2-execution-log.md` Week-1 + Week-2 entries | written as we go |

### Acceptance criteria (engine sprint complete when all true)

- [ ] All 5 hook event types dispatch to registered policies.
- [ ] Verdict combination rules pass property tests.
- [ ] Performance: p95 of `pre_tool_use` <50 ms with 5 policies registered.
- [ ] One demo policy (a simple `block` if file ends with `.py.bak`) registers and works end-to-end through both Claude Code wiring AND MCP dispatch wiring.
- [ ] Crash in one policy doesn't break others.
- [ ] Engine can be enabled/disabled wholesale via `CODEVIRA_ENGINE=0` env var (escape hatch).
- [ ] Token meter records every tool response and exposes session summary.
- [ ] Fix history detects fix commits in the agent-mcp repo's git log (smoke test).

Once acceptance is green, the engine sprint is done and Hero 4 (Blast-Radius) starts in Week 4 (Pillar 1 fills Week 3).

---

## Decisions deliberately deferred to per-hero sprints

These were tempting to bake into the engine but I'm holding them back:

1. **MCP Apps `ui://` resource registration** — only Hero 8 (Decision Replay) needs this. Defer to Hero 8 sprint.
2. **LLM-based intent classifier** — only Heroes 3 and 9 need it. Build in those sprints.
3. **Per-policy A/B framework** — interesting but premature. Add when we have ≥3 deployed users.

---

## Open questions (to resolve during engine sprint)

1. **Should `pre_tool_use` block delay the AI's response, or send a "blocked" message and let it continue?** Claude Code's hook contract says exit-non-zero blocks; need to verify behavior is what we want. Test in Week 1.
2. **How do we distribute hook scripts across users?** Bundled in pip package + copied to `~/.claude/hooks/` on `codevira hooks install`. But what if user already has hooks at the same paths? → namespace under `~/.claude/hooks/codevira-*` to avoid collisions.
3. **Should signals expose raw SQL access, or only typed accessors?** Typed accessors (better encapsulation, easier to mock in tests). Raw SQL is escape hatch for v2.1 if a policy needs something we didn't anticipate.

These get answered in the execution log as we hit them.
