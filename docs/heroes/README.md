# v2.0 Hero Specifications

Per-hero detailed specs. Each is written **just before its implementation sprint** — not all upfront. Heroes 1-10 inform each other; later specs benefit from earlier learnings.

## How to read these

- **Master plan**: `docs/v2-master-plan.md`
- **Running progress**: `docs/v2-execution-log.md`
- **This folder**: detailed design per hero (when/if written)

Each spec follows the same structure:

1. Problem statement (1 paragraph)
2. User pain it solves (concrete example)
3. Mechanism (which hook fires, signals consumed, when block/warn/allow)
4. Configuration knobs
5. Edge cases (false-positive handling)
6. Demo storyboard (10-second scene)
7. Acceptance test list (5-10 scenarios)

Specs cap at ~500 lines each.

## The line-up

| # | Hero | Spec | Sprint week | Status |
|---|---|---|---|---|
| 0 | **Engine** (shared infrastructure) | [`00-engine.md`](./00-engine.md) | 1-2 | ✅ shipped through Week-2; 5 QA rounds clean |
| P1 | **Pillar 1: `codevira setup`** | [`pillar-1-setup.md`](./pillar-1-setup.md) | 3 | ✅ shipped Week 3; 8 QA rounds (1 HIGH + 5 P2 fixed; 1 deferral) |
| 4 | **Blast-Radius Veto** | [`04-blast-radius.md`](./04-blast-radius.md) | 4 | ✅ shipped Week 4; 5 QA rounds (1 MEDIUM + 2 P1 + 1 test-gap fixed) |
| 1 | **Decision Lock** | [`01-decision-lock.md`](./01-decision-lock.md) | 5 | ✅ shipped Week 5; R1-R8 clean (1 docstring nit only) |
| 5 | **Cross-Session Consistency** | [`05-cross-session.md`](./05-cross-session.md) | 6 | ✅ shipped Week 6; R1-R8 (1 test gap on short-prompt-skip caught + fixed) |
| 6 | **Token Budget Live View** | [`06-token-budget.md`](./06-token-budget.md) | 7 | ✅ shipped Week 7; Tier-0 pre-flight from start (10 mutations + behavioral spies + real CLI subprocess); Bug 3 caught (enabled_by_default flag was dead) |
| 2 | **Anti-Regression Memory** | [`02-anti-regression.md`](./02-anti-regression.md) | 8 | ✅ shipped Week 8; Tier-0 pre-flight from start; 9/10 mutations caught (M7 documented as observably-redundant) |
| 7 | **Live Style Enforcement** | [`07-live-style.md`](./07-live-style.md) | 9 | ✅ shipped Week 9; Tier-0 pre-flight (38 tests, 10/10 mutations); Bug 4 caught in Week-9 integration QA round (silent no-op on Write tool — fixed) |
| 10 | **AI Promotion Score** | [`10-ai-promotion.md`](./10-ai-promotion.md) | 10 | ✅ shipped Week 10; Tier-0 pre-flight from start (35 + 2 subprocess tests, real outcomes DB, 10/10 mutations); first SESSION_START policy + `codevira insights` CLI |
| 9 | **Proactive Intent Inference** | `09-intent-inference.md` (TBD) | 11 | spec pending |
| 3 | **Scope Contract Lock** | `03-scope-contract.md` (TBD) | 12 | spec pending |
| 8 | **Decision Replay** | `08-decision-replay.md` (TBD) | 13 | spec pending |

The order is dependency-aware (front-load easiest wins; defer riskiest), not numerical.

## Per-hero workflow (from master plan)

1. Write the spec in `docs/heroes/NN-name.md` (under 500 lines).
2. Sleep on it once.
3. Implement on feature branch `hero/NN-name`.
4. Tests from acceptance list.
5. Founder dogfood ≥ 48 hours.
6. Bundle into next alpha release.
7. Update `docs/v2-execution-log.md` Week-N entry.
8. Move to next hero.
