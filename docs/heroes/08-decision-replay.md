# Hero 8 — Decision Replay

> "Six months from now, you stare at this code: 'why is auth done this way?' Codevira already has the answer."

The tenth (and final!) policy hero. Surfaces the project's decision history as a browsable timeline through three universal channels: MCP resource (any MCP client), CLI command (any terminal), HTML render (Claude Desktop / future MCP Apps clients).

Sprint week: **Week 13**. ~330 LOC across one builder/renderer module + MCP resource wiring + CLI subcommand. Reuses existing `decisions` + `outcomes` + `sessions` tables (already populated by Heroes 1, 5, 10).

This hero is NOT a policy — it doesn't intercept events or block. It exposes existing data in new surfaces.

---

## Problem statement

Codevira tracks decisions Heroes 1-7 contribute to: `do_not_revert` flags, fix history, learned rules, outcome scores. The data exists. **What's missing is a browsable surface.** Today, the only way to see "what decisions touched auth.py?" is via:

```bash
codevira search "auth"
```

…which returns a flat list. No timeline, no per-session grouping, no outcomes attached, no clickable navigation. Hero 8 fixes that.

---

## User pain (concrete example)

**Without Hero 8:**

```text
[6 months later]
User: "Why does auth.py use bcrypt instead of argon2?"
Reviewer: *greps git log* → "fix(auth): bcrypt over argon2" — but no
                            context for WHY
User: *eventually finds the GitHub issue from 6 months ago, then a
       Slack thread, then asks the original author*
```

**With Hero 8:**

```text
$ codevira replay --query auth
═══════════════════════════════════════════════════════════════════
  Codevira Replay — auth (last 30 days)
═══════════════════════════════════════════════════════════════════

📌 2025-04-13 — auth.py
   "use bcrypt over argon2 — see issue #142"
   🔒 LOCKED · score 0.95 · 6 outcomes (6 kept, 0 reverted)
   Session: s_4f2a · "Fix login flow for special-char emails"

📌 2025-03-20 — auth.py
   "use 12-round bcrypt cost factor"
   score 0.88 · 4 outcomes (4 kept, 0 reverted)
   Session: s_2b9d · "Tune bcrypt for our hardware"

⚠ 2025-02-08 — auth.py (REVERTED)
   "switch to argon2"
   score 0.0 · 2 outcomes (0 kept, 2 reverted)
   Session: s_1e7b · "Try argon2 — performance test"

(Run with --format html for browser view; --format markdown for clipboard.)
```

In MCP Apps-capable clients (Claude Desktop), the same data renders as an interactive HTML timeline directly in the chat:

```
@codevira show decisions on auth
[interactive timeline renders inline; click any decision for full context]
```

The win: **decision history is one command away** in any terminal, in any MCP client, in any browser.

---

## Mechanism

### Three render surfaces, one data path

```
                                   ┌─→ Terminal (codevira replay)
build_timeline(query, since_days)  ┼─→ Markdown (codevira replay --format markdown)
                                   ├─→ HTML (codevira replay --format html)
                                   └─→ MCP resource (codevira://decisions/<query>)
                                       — Claude Desktop renders the HTML
```

### Pure data path: `mcp_server/decision_replay.py`

**`build_timeline()`** — pure SQL aggregation:

```python
def build_timeline(
    *,
    query: str | None = None,      # filter by substring in decision/file
    since_days: int = 30,          # lookback window
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build a per-decision timeline with outcomes attached.

    Each row:
      {
        "id": int,
        "decision": str,
        "file_path": str | None,
        "context": str | None,
        "created_at": str,         # ISO date
        "session_id": str,
        "session_summary": str | None,
        "locked": bool,
        "kept": int,
        "modified": int,
        "reverted": int,
        "total": int,
        "score": float,            # promotion_score.score_decision
      }
    """
```

Joins `decisions LEFT JOIN outcomes` (Hero 10's aggregation pattern), plus `LEFT JOIN sessions` for the human-readable session summary, plus `LEFT JOIN nodes` for `do_not_revert` flag.

**Renderers** — three pure functions:

- `render_terminal(timeline, *, ascii=False)` → list of strings (CLI output)
- `render_markdown(timeline)` → markdown string (clipboard / docs)
- `render_html(timeline, *, embeddable=False)` → standalone HTML string

**Empty-section defense (Lesson #19)**: every renderer's empty case is explicitly tested. No vacuous headers.

### MCP resource handlers (server.py additions)

```python
@server.list_resources()
async def _list_resources():
    return [
        Resource(
            uri="codevira://decisions",
            name="Codevira decisions timeline",
            description="Browse this project's decision history with "
                        "outcomes and session context.",
            mimeType="text/html",
        ),
    ]

@server.read_resource()
async def _read_resource(uri: AnyUrl) -> str:
    if str(uri) == "codevira://decisions":
        timeline = build_timeline(since_days=30, limit=20)
        return render_html(timeline)
    if str(uri).startswith("codevira://decisions/"):
        query = str(uri).split("/", 3)[3]
        timeline = build_timeline(query=query, since_days=90, limit=20)
        return render_html(timeline)
    raise ValueError(f"unknown resource: {uri}")
```

**MCP Apps SEP-1865 compatibility note**: the spec proposes a `ui://` URI scheme for interactive iframes. Until our SDK exposes that explicitly, we emit standard HTML at standard MCP URIs (`codevira://decisions`). Clients that support HTML rendering in resource content (Claude Desktop) display it; clients that only show plain text fall back to a text representation. v2.1 may upgrade to `ui://` when SDK support lands.

### CLI subcommand

```
codevira replay [--query QUERY] [--since 30d] [--top 20]
                [--format terminal|markdown|html]
                [--ascii] [--out FILE]
                [--project PATH]
```

- Default format: `terminal` (pretty-printed)
- `--out FILE`: write to file instead of stdout (useful for `--format html > timeline.html`)
- `--project PATH`: queries another project's data; runs through `is_invalid_project_root()` (Bug-8 lesson)

### Configuration knobs

Hero 8 has no env vars — it's a read-only browsing surface. Configuration is per-invocation via CLI flags or resource URI parameters.

---

## Performance budget

| Operation | Target p95 |
|---|---|
| `build_timeline()` with 100 decisions, 500 outcomes | < 20 ms |
| `render_html()` with 20-row timeline | < 5 ms |
| `render_markdown()` / `render_terminal()` | < 2 ms |
| MCP `read_resource("codevira://decisions")` | < 30 ms |
| CLI `codevira replay` cold start | < 500 ms |

The SQL hits indexed columns (`decisions.created_at`, `outcomes.decision_id`); we already proved this scales in Hero 10. Hero 8 adds a substring filter via SQL `LIKE`, which is fine for project-sized data (<10k decisions).

---

## Edge cases

| Edge case | Behaviour |
|---|---|
| Empty `decisions` table | Empty timeline; renderers emit a friendly "no decisions yet" placeholder, NOT an empty section header (Bug-6 + Lesson #19). |
| Query matches nothing | Same as above per query. |
| Decision has no outcomes recorded | row has `total=0`, `score=0`. Renderer shows the decision but omits the "X outcomes" suffix. |
| Decision has no session_id (legacy v1.x data) | Renderer shows "Session: (unknown)" instead of crashing. |
| HTML render with adversarial decision text containing `<script>` | We escape via `html.escape()`. **Tested** — Bug-X-shape: declared HTML safety must trace through every concat. |
| Terminal width < 80 chars | Renderer wraps long fields gracefully. |
| MCP read_resource with malformed URI | `ValueError` propagates → MCP server returns error to client (it's the client's job to display it). |
| CLI `--project` invalid | `is_invalid_project_root()` rejection (Bug-8 parity). |
| CLI `--since=garbage` | `_parse_since()` (already in cli_insights) reused — falls back to default + warns to stderr. |

---

## Acceptance test list

12 scenarios:

1. `build_timeline()` returns ordered rows with score from real DB
2. `build_timeline(query="auth")` filters by substring
3. `build_timeline()` on empty DB returns `[]`
4. `render_html()` escapes adversarial `<script>` content
5. `render_terminal()` empty-timeline shows "no decisions yet"
6. `render_markdown()` empty-timeline shows "no decisions yet"
7. MCP `list_resources()` exposes the decisions URI
8. MCP `read_resource("codevira://decisions")` returns HTML
9. CLI `codevira replay` against empty project shows friendly message
10. CLI `codevira replay --query auth` against real DB lists matching decisions
11. CLI `codevira replay --project $HOME` rejected (Bug-8 lesson)
12. CLI `codevira replay --format markdown` produces valid markdown

---

## Files affected

### New

| Path | Purpose |
|---|---|
| `mcp_server/decision_replay.py` | Pure timeline builder + 3 renderers |
| `mcp_server/cli_replay.py` | `codevira replay` command |
| `tests/engine/test_decision_replay.py` | Builder + renderer unit + acceptance |
| `tests/test_cli_replay.py` | CLI subprocess tests |

### Modified

| Path | Change |
|---|---|
| `mcp_server/server.py` | Add `@server.list_resources()` + `@server.read_resource()` |
| `mcp_server/cli.py` | Wire `replay` subcommand |

---

## QA gate (Tier-0 + deep-audit from start)

Lessons #15-21 applied:

- ✅ Real DB integration via real `record_outcome` + `INSERT INTO decisions` (same path Heroes 1, 10 use)
- ✅ End-to-end through MCP `read_resource` handler (the wiring path — Bug-4 lesson)
- ✅ End-to-end through CLI subprocess (the second wiring path — Bug-4 lesson)
- ✅ HTML XSS probe (Bug-X-shape: declared "renders decision text as HTML" must trace through `html.escape`)
- ✅ Empty-timeline probe for all 3 renderers (Lesson #19)
- ✅ CLI `--project` runs through `is_invalid_project_root()` (Bug-8 lesson)
- ✅ Content-verifying assertions on HTML output (decision text appears, not just headers)
- ✅ 10+ mutations from start
- ✅ Bug-shape audit

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MCP Apps SEP-1865 not in SDK yet | High | Low | Ship standard HTML at standard URIs; v2.1 upgrades to `ui://` when SDK lands. |
| HTML XSS via decision text | Medium | High | `html.escape()` everywhere; XSS probe in tests. |
| Bug-8-shape: CLI `--project` skips validation | Low | Medium | Reuse `is_invalid_project_root` from `cli_insights.py` pattern. |
| `build_timeline` SQL scan on 100k+ decisions slow | Low | Medium | LIMIT 20 default; substring filter uses index where possible. |
| Outcome data missing for old decisions | High | Low | Renderer omits "X outcomes" suffix gracefully. |
| Bug-X: empty-DB renders ugly empty headers | Medium | Low | Lesson #19 lock-in: every renderer's empty case explicitly tested. |

---

## Out of scope (deferred)

- **Interactive HTML with click-through to source** — needs `ui://` scheme + iframe sandboxing. v2.1.
- **Filter UI in HTML** (date range, file path filter) — v2.1.
- **Search by AI session ID** — `codevira replay --session abc` deferred to v2.1.
- **Export to PDF / image** — out of scope (use browser print).
- **Real-time streaming updates** as new decisions land — needs MCP subscriptions; v2.1.

---

## Definition of done

- [ ] `build_timeline()` + 3 renderers shipped
- [ ] MCP resources `codevira://decisions` and `codevira://decisions/<query>` exposed
- [ ] `codevira replay` CLI works against real DB
- [ ] All 12 acceptance tests pass
- [ ] Tier-0 + deep-audit checklist clean
- [ ] Proactive Week-13 integration QA round (don't wait for user push)
- [ ] No new Bug-class issues
- [ ] `docs/v2-execution-log.md` Week-13 entry written
- [ ] Heroes shipped: **10 of 10**. Ready for Week 14 (comprehensive E2E).
