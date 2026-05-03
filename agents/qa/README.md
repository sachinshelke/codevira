# QA Agent Team

Catalog of 22 QA angles, codified as agent prompts that any Claude
session can invoke. Companion to `docs/qa-playbook.md` (the strategy
doc).

## How to invoke an agent

Each `.md` file here is a self-contained prompt. To run one:

```python
# In a Claude session, invoke via Agent tool:
Agent({
    description: "QA: <angle name>",
    subagent_type: "Explore",   # or Plan / general-purpose
    prompt: <contents of the .md file, with {scope} placeholder filled>
})
```

The placeholder `{scope}` is replaced with the files/directories
under review for the current hero or sprint.

## Agent files

### Tier 1 — Automatable (8 agents)

| Agent file | Angle # | Subagent type |
|---|---|---|
| `01-code-review.md` | 1 | Explore |
| `02-adversarial-fix-review.md` | 2 | Explore |
| `03-cross-module-impact.md` | 3 | Explore |
| `06-doc-drift.md` | 6 | Explore |
| `07-security-audit.md` | 7 | Explore |
| `12-llm-redteam.md` | 12 | Explore |
| `13-multi-ide-schema.md` | 13 | Explore (with WebFetch) |
| `22-competitor-benchmark.md` | 22 | Explore (with WebFetch) |

### Tier 2 — Script-driven (8, scripts in `agents/qa/scripts/`)

| Script file | Angle # | What it does |
|---|---|---|
| `04-latency.sh` | 4 | Measures hook + dispatch latency |
| `05-integration-grep.sh` | 5 | Verifies adapter calls are wired |
| `08-type-safety.sh` | 8 | Runs mypy + multi-Python import |
| `09-concurrent-stress.py` | 9 | Threads × events stress |
| `14-upgrade-sim.sh` | 14 | v1.8.0 → v2.0 simulation |
| `15-mutation.sh` | 15 | Runs mutmut on engine modules |
| `17-crash-recovery.sh` | 17 | kill -9 mid-write |
| `21-resource-limits.py` | 21 | Huge SQLite + many policies |

### Tier 3 — Human-driven checklists (6)

| Checklist file | Angle # |
|---|---|
| `10-external-schema-check.md` | 10 |
| `11-live-claude-code.md` | 11 |
| `16-soak-test.md` | 16 |
| `18-cli-ux-newuser.md` | 18 |
| `19-i18n-unicode.md` | 19 |
| `20-fs-edge-cases.md` | 20 |

## Per-hero QA loop

For each hero before alpha:

1. Run all Tier 1 agents (parallel; ~30 min total)
2. Triage findings: HIGH/P0 → fix immediately; MEDIUM/P1 → backlog
3. Run relevant Tier 2 scripts (~30 min)
4. Re-run Tier 1 agents on the fixes
5. Document results in `docs/v2-execution-log.md` for that hero

For pre-beta / pre-GA (Heroes shipping):
6. Tier 3 checklists (~1 day total)
