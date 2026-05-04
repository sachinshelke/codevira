# Hero 10 — AI Promotion Score

> "Codevira learns silently. Show the user what's working — and what isn't."

The seventh policy hero. First **SESSION_START** policy — fires when a new AI session begins, summarizes which past decisions held up under git scrutiny, and surfaces the top stable patterns as injected context.

Sprint week: **Week 10**. Reuses existing `outcome_tracker.py` + `rule_learner.py` + `outcomes` + `learned_rules` tables. New code: ~300 LOC across one policy + one CLI command + one scoring helper.

---

## Problem statement

Heroes 1-7 operate silently. Codevira watches edits, blocks bad ones, surfaces decisions, learns preferences — but the *user never sees what's working*. They can't tell:

- Which past architectural decisions are *stable* (kept across many subsequent commits)?
- Which decisions get *reverted* often (signal that the AI keeps trying something the user keeps undoing)?
- Which *learned rules* (from `rule_learner`) have crossed the confidence threshold?

Without visibility, users assume codevira is doing nothing. The data is already in the graph — Heroes 0-9 wrote it. Hero 10 is the **read** side: a weekly digest that closes the feedback loop.

---

## User pain (concrete example)

**Without Hero 10:**

```text
[Friday afternoon, after 2 weeks of using codevira]
User (in their head): "Has any of this codevira stuff actually been useful?
  I've made 30+ Edit calls this week. The AI hasn't broken anything obvious.
  But I have no idea if it's because codevira's working or because nothing
  was at risk in the first place."
```

**With Hero 10:**

```bash
$ codevira insights
═══════════════════════════════════════════════════════════════════
  Codevira Insights — last 7 days
═══════════════════════════════════════════════════════════════════

📌 Top stable decisions (kept untouched in 5+ subsequent commits):

  1. auth.py — "use bcrypt over argon2 — see issue #142"
     Score: 0.95  •  6 outcomes (6 kept, 0 reverted)
     Locked 14 days ago.

  2. retries.py — "exponential backoff with jitter; max 5 attempts"
     Score: 0.88  •  4 outcomes (4 kept, 0 reverted)

  3. db/migrations.py — "always make schema changes additive"
     Score: 0.83  •  3 outcomes (3 kept, 0 reverted)

⚠ Top reverted decisions (AI keeps trying, you keep undoing):

  1. style.css — "Bootstrap not Tailwind"
     Score: 0.20  •  5 outcomes (1 kept, 4 reverted)
     Suggestion: lock this decision (`do_not_revert`) so Hero 1
     blocks future Bootstrap edits.

📈 Emerging patterns (rule_learner confidence ≥ 0.7):

  • "Files in 'tests/' should mirror 'src/' layout" (confidence 0.85)
  • "auth.py imports from 7 files — review changes carefully" (0.78)

(Run with --since=14d for a longer window.)
```

The win: **users see codevira earning its keep**, and the system surfaces *its own gaps* (e.g., "this decision keeps getting reverted; lock it").

---

## Mechanism

### Two-part design

**1. Score engine** (`mcp_server/engine/promotion_score.py`):

Pure functions over the `outcomes` table:

```python
def score_decision(outcomes: list[dict]) -> float:
    """Compute promotion score from outcome history.

    score = (kept + 0.5 * modified) / max(total, 1)

    Range: [0.0, 1.0]
    - 1.0  = every outcome was 'kept'
    - 0.5  = mix of modified + reverted (uncertain)
    - 0.0  = every outcome was 'reverted'
    """
```

Reads from `outcomes` table; writes nothing. Other modules (the policy + CLI) consume.

**2. Policy** (`mcp_server/engine/policies/ai_promotion.py`):

`AIPromotionScore` — fires on SESSION_START. If the project has ≥ N high-confidence rules or top-stable decisions, INJECT them into the AI's first turn. Same shape as Hero 5 (Cross-Session Consistency) but on SessionStart not UserPromptSubmit.

```python
class AIPromotionScore(Policy):
    name = "ai_promotion_score"
    handles = (EventType.SESSION_START,)
    enabled_by_default = True
    priority = 10  # advisory; runs after blocking + warning policies

    def evaluate(self, event, signals=None):
        if event.event_type != EventType.SESSION_START:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        # 1. Top stable decisions (score ≥ threshold)
        stable = _top_stable_decisions(
            signals, min_score=config["min_score"],
            max_items=config["max_inject"],
        )
        # 2. High-confidence learned rules
        rules = _top_rules(
            signals, min_confidence=config["min_confidence"],
            max_items=config["max_inject"],
        )

        if not stable and not rules:
            return PolicyVerdict.allow()

        return self._make_inject_verdict(stable, rules)
```

**3. CLI** (`mcp_server/cli_insights.py`):

`codevira insights [--since=7d] [--top=5]` — pretty-printed terminal output. Wraps the same scoring functions.

### Decision tree

```
SESSION_START event arrives
│
├── event_type != SESSION_START?      → ALLOW (defensive)
├── mode = "off"?                     → ALLOW
├── signals = None?                   → ALLOW
│
├── Read outcomes for this project
│   └── No outcomes recorded yet?     → ALLOW (cold start; nothing to surface)
│
├── Compute top-N stable decisions
├── Read top-N high-confidence rules
│
├── Both lists empty?                 → ALLOW
│
└── INJECT a digest as additionalContext
    (priority=10 means other inject policies — Hero 5 — run first if both fire)
```

### Configuration knobs

| Setting | Default | Purpose |
|---|---|---|
| `CODEVIRA_AI_PROMOTION_MODE` | `inject` | `off` / `inject`. No `block` mode — this is advisory. |
| `CODEVIRA_AI_PROMOTION_MIN_SCORE` | `0.7` | Decisions with score < this aren't surfaced as "stable" |
| `CODEVIRA_AI_PROMOTION_MIN_CONFIDENCE` | `0.7` | Learned rules below this confidence aren't surfaced |
| `CODEVIRA_AI_PROMOTION_MAX_INJECT` | `3` | Max items per category in the inject (1-10) |
| `CODEVIRA_AI_PROMOTION_MIN_OUTCOMES` | `2` | Decisions with fewer than N outcomes aren't scored (low signal) |

### What "score" means

Per decision:

```
score = (kept + 0.5 × modified) / max(kept + modified + reverted, 1)
```

Rationale:
- A "kept" outcome is full credit (the AI's decision survived as-is)
- A "modified" outcome is half credit (the AI was on the right track but needed correction — still partly useful)
- A "reverted" outcome is zero credit (the developer undid it)
- Bounded to [0, 1]; safe for division-by-zero (the `max(..., 1)`)

This isn't a probability — it's a relative ranking signal. v2.1 will add Bayesian smoothing (laplace prior) so a single "kept" doesn't immediately rank decisions with score=1.0 above decisions with 10 kepts and 1 modified.

### Reading from the existing `outcomes` table

Outcome shape (from `indexer.outcome_tracker._analyze_single_session`):
```sql
outcome_type IN ('kept', 'modified', 'reverted')
```

A decision can have N outcomes (one per session that touched its file after creation). Hero 10 aggregates per `decision_id`:

```sql
SELECT
    d.id, d.decision, d.file_path, d.created_at,
    COUNT(o.id) AS total,
    SUM(CASE WHEN o.outcome_type = 'kept' THEN 1 ELSE 0 END) AS kept,
    SUM(CASE WHEN o.outcome_type = 'modified' THEN 1 ELSE 0 END) AS modified,
    SUM(CASE WHEN o.outcome_type = 'reverted' THEN 1 ELSE 0 END) AS reverted
FROM decisions d
LEFT JOIN outcomes o ON o.decision_id = d.id
WHERE d.created_at >= datetime('now', ?)  -- e.g. '-7 days'
GROUP BY d.id
HAVING total >= ?  -- min_outcomes filter
ORDER BY (kept * 1.0 + modified * 0.5) / MAX(total, 1) DESC
LIMIT ?
```

The "since" parameter defaults to `-30 days` for the policy (broad context for new sessions) and `-7 days` for the CLI (weekly digest framing).

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `evaluate()` for non-SESSION_START | < 50 µs |
| `evaluate()` for SESSION_START, no outcomes | < 1 ms |
| `evaluate()` for SESSION_START, 100 decisions / 500 outcomes | < 10 ms |
| `codevira insights` CLI cold start | < 500 ms (one-shot CLI; relaxed budget) |

Aggregation queries hit indexed columns (`outcomes.decision_id`, `decisions.created_at`); 1000 decisions × 5000 outcomes is well under target on SQLite.

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Project has no decisions yet | Cold start — INJECT empty → allow; no noise |
| Project has decisions but no outcomes (no commits since session) | Score is undefined → all decisions filtered by `min_outcomes` → no inject |
| All decisions have score = 0 (everything was reverted) | They appear in CLI's "top reverted" section but are NOT injected (positive signal only) |
| `outcomes` table corrupted / missing | `signals.outcomes()` already wraps in try/except → returns []  → allow |
| User runs `codevira insights` with no outcomes | Friendly message: "No outcomes recorded yet — use codevira for a few sessions and try again" |
| `--since=invalid` on CLI | Falls back to default 7d + warns once on stderr |
| Multi-project `codevira insights` invocation | Resolves via `paths.get_project_root()` like every other CLI; respects `CODEVIRA_PROJECT_DIR` |
| Emoji-stripped terminal | CLI uses `--ascii` flag to swap unicode badges for ASCII fallbacks |

---

## Acceptance test list

10 scenarios:

1. **Non-SESSION_START event allowed** — PreToolUse, PostToolUse, etc. pass through with `signals.outcomes` NOT called (behavioral spy).
2. **SessionStart with no decisions → allow** — fresh project; no inject.
3. **SessionStart with decisions but no outcomes → allow** — fail-open below `min_outcomes`.
4. **SessionStart with high-score decision → inject** — happy path; verdict.action == "inject" + decision text in inject_context.
5. **SessionStart with mixed-score decisions → only top-N injected** — sorted, capped at `max_inject`.
6. **`mode = "off"` disables policy even with high-score decisions present** — behavioral spy proves outcomes() NOT called.
7. **`min_score` filter excludes decisions below threshold** — score=0.5 excluded when `min_score=0.7`.
8. **High-confidence learned rules included in inject** — rule_learner data surfaced.
9. **CLI `codevira insights` returns formatted output with stable + reverted sections** — end-to-end against real DB.
10. **Performance: SessionStart eval with 100 decisions / 500 outcomes < 10 ms p95.**

Tests live in `tests/engine/test_ai_promotion.py` + `tests/test_cli_insights.py`.

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/engine/promotion_score.py` | Pure scoring functions: `score_decision`, `top_stable_decisions`, `top_rules` |
| `mcp_server/engine/policies/ai_promotion.py` | `AIPromotionScore` policy + inject formatter |
| `mcp_server/cli_insights.py` | `codevira insights` CLI |
| `tests/engine/test_ai_promotion.py` | Acceptance + behavioral + mutation tests |
| `tests/test_cli_insights.py` | CLI integration tests against real DB |

### Modified

| Path | Change |
|---|---|
| `mcp_server/engine/__init__.py` | Add `AIPromotionScore` to `register_default_policies()` |
| `mcp_server/engine/policies/__init__.py` | Re-export the new policy |
| `mcp_server/engine/signals.py` | Add `signals.outcomes(decision_id=None, since_days=30)` accessor |
| `mcp_server/cli.py` | Wire `codevira insights` subcommand |

---

## QA gate (Tier-0 pre-flight from start)

Per Lessons #15-#18 and the post-Bug-4 reinforcement:

- ✅ **Real-DB integration** — outcomes inserted via `db.record_outcome()` then read via `signals.outcomes()`
- ✅ **Behavioral spies** — `_FakeSignals.outcomes_calls` to verify gating
- ✅ **End-to-end dispatch** — register all 7 heroes, fire a SessionStart through `dispatch()`, assert inject
- ✅ **End-to-end CLI** — subprocess-invoke `codevira insights` against an isolated project; check stdout
- ✅ **End-to-end through `claude_code_hooks.handle("SessionStart")`** — feed JSON, parse stdout, verify `additionalContext` (the Bug 4 lesson: test the wiring path, not just dispatch)
- ✅ 10+ mutations from start
- ✅ Bug-shape audit — every contract field used (no dead fields)

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `outcomes` table empty on most projects (low signal) | High at first | Low | Hero 10 silently no-ops below `min_outcomes` threshold. Ramps up as outcome_tracker accumulates data. |
| Score over-confident on small samples (1 kept = score 1.0) | High | Medium | `min_outcomes = 2` default. v2.1 adds laplace smoothing. |
| `signals.outcomes` shape drift (Bug-1-class) | Low | High | Tier-0 pre-flight: real-DB integration test. Document SQL schema in code. |
| CLI output overflows terminal width | Low | Low | Truncate decision text to 60 chars. Use `shutil.get_terminal_size()`. |
| SessionStart inject duplicates Hero 5 (cross-session) | Low | Low | Different shape: Hero 5 fires on UserPromptSubmit (per turn); Hero 10 on SessionStart (once per session). They complement. |
| Bug-4-shape: SessionStart wiring path untested | **Medium** | **High** | Mandatory end-to-end test through `claude_code_hooks.handle("SessionStart")` with realistic JSON payload — not just dispatch unit test |

---

## Out of scope (deferred)

- **Auto-suggest "lock this decision" on reverted-decision section** — needs MCP Apps integration for clickable actions. Hero 8 territory.
- **Bayesian smoothing of scores** — v2.1+. Current arithmetic mean is good enough for ordering.
- **Cross-project insights** — global.db trends across all projects. v2.1.
- **Scheduled email digest** — out of scope (local-first, no SMTP).
- **Rule auto-promotion to `do_not_revert`** when score consistently > 0.95 — risky; needs user confirmation flow. v2.1.

---

## Definition of done

- [ ] `AIPromotionScore` policy registered + enabled by default.
- [ ] `signals.outcomes()` accessor added to SignalContext.
- [ ] `codevira insights` CLI works end-to-end against a real project.
- [ ] All 10 acceptance tests pass.
- [ ] Tier-0 pre-flight clean (real DB + behavioral spies + dispatch + CLI subprocess + Claude Code wiring).
- [ ] No new Bug-class issues (audit during R8).
- [ ] `docs/v2-execution-log.md` Week-10 entry written.
