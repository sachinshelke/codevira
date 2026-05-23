# Codevira Foolproof Product Charter

**The product, not just the release.** v2.0.0 shipped via a process
that satisfied "all unit tests pass," but the *product* shipped with
23 silent-failure bugs. This document defines what "foolproof
product" means for codevira and how every code change must satisfy
it.

The release-discipline scaffold (Makefile + hook + CI + skills) is
the process. **This charter is the product**. They're complementary;
both are necessary.

---

## The 10 product principles (P1–P10)

| # | Principle | One-line rule |
|---|---|---|
| **P1** | No silent failures | Every 0-result code path emits a reason + `fix_command`. |
| **P2** | Self-diagnose on startup | Every entry point detects + reports known-bad states; never starts in a degraded state silently. |
| **P3** | Atomic state mutations | All external writes (files, DB, IDE configs) use atomic patterns. No half-written state. |
| **P4** | Defensive parsing | External input parses with fail-safe defaults; never crashes on malformed input. |
| **P5** | Bounded resources | No unbounded loops, retries, or log writes. Circuit breakers on every retry boundary. |
| **P6** | Predictable detection | One source of truth per concept. Two code paths that look at the same thing must agree. |
| **P7** | Reversible operations | Every install has a documented + tested uninstall. Every write merges, never overwrites user keys. |
| **P8** | Helpful error messages | WHAT failed + WHY + FIX. The `fix_command` pattern in every MCP tool response. |
| **P9** | Graceful degradation | Single-subsystem failure must not cascade. Document what still works when X is broken. |
| **P10** | Observability | Structured logging; `codevira doctor` reads actual state, never infers; logs auto-rotate. |

Full SKILL definitions live in
[`.claude/skills/foolproof-product/SKILL.md`](../.claude/skills/foolproof-product/SKILL.md).
Machine-checkable tests live in
[`tests/e2e/test_product_invariants.py`](../tests/e2e/test_product_invariants.py).

---

## How the principles map to the v2.0 bugs

Every v2.0 bug traces to one or more violated P-principle. The
post-mortem:

| Bug | What broke | Violated principles |
|---|---|---|
| **A** — discovery vs. indexing matcher mismatch | `configure` finds 8 files; `index` matches 0 | P6 (predictable detection) |
| **B** — `index` says "up to date" with 0 chunks | Lies about state | P1 (silent failure) |
| **C** — `status` shows 0/0 with no warning on uninitialized | No actionable state signal | P1, P10 |
| **D** — config split between in-repo and centralized | Two configs disagree silently | P3 (atomic), P6 |
| **E** — docs-only repos produce silent 0 chunks | No reason given | P1 |
| **F** — `init` drops top-level files (`.`) | Single-language filter on dirs | P6 |
| **G** — project keys are unreadable hashes | UX-hostile state representation | (UX, not a P violation) |
| **H** — no `--verbose` on `index` | No way to diagnose | P10 |
| **I** — "Auto-detected" promises detection but shows defaults | Misleading label | P1, P8 |
| **J/K** — sentence-transformers on critical path | Claude Desktop disconnects | P9 (graceful degradation) |
| **L** — init lists generic dirs not present | False positives in detection | P1, P6 |
| **M** — init shows all 80 extensions regardless of project | Same | P1 |
| **N** — init interactive but should match configure | Inconsistency | P6 |
| **O** — configure typing "1,3,5" not arrow-keys | UX (not P violation, but quality) |
| **HNSW writer crash storm** | 41 crashes in one session | P2 (startup detect), P5 (circuit breaker) |
| **41-crash log spam** | Grows unbounded | P5, P10 |
| **Claude Desktop config cleared (rare)** | Race in non-atomic write | P3 |
| **codevira clean leaves orphaned hooks** | Reverse incomplete | P7 |

**Lesson:** these aren't isolated bugs. They're a category. The
category is "we wrote code that satisfies the function signature but
not the product invariants."

---

## The mandatory checklist (every code change to mcp_server/ or indexer/)

```
Subsystem touched: <indexer | MCP tool | hook | CLI | watcher>
Files in target list: <N files>

For each affected code path, answer:

P1  No silent failures      [ ] All 0-result paths emit reason + fix_command
P2  Self-diagnose           [ ] Doctor check added/updated if state added
P3  Atomic mutations        [ ] Writes use tmp→fsync→rename or transactions
P4  Defensive parsing       [ ] External input has try/except + safe default
P5  Bounded resources       [ ] Loops bounded; retries have circuit breaker
P6  Predictable detection   [ ] No parallel matcher; single source of truth
P7  Reversible              [ ] Uninstall counterpart works (tested)
P8  Helpful errors          [ ] Every error: WHAT + WHY + FIX
P9  Graceful degradation    [ ] List what still works when this subsystem fails
P10 Observability           [ ] Structured log; doctor surfaces new state

Justifications for any "N/A":
  <e.g. "P3 N/A: no external state written">
```

A code review (human OR AI) that doesn't see this checklist for a
mcp_server/indexer change is a discipline breach. The
`foolproof-product` SKILL.md enforces it conversationally; the
`test_product_invariants.py` suite enforces it mechanically.

---

## Subsystem-specific invariants

### Indexer
- One matcher (`discover_source_files()` is THE matcher)
- 0-chunk results MUST include reason + fix_command
- Watcher: circuit-broken after 5 consecutive failures
- Crash log rate-limited: 1 entry per file per minute MAX

### MCP server
- `tools/list` MUST respond in <1s (no chromadb / sentence-transformers
  import on critical path)
- Every tool response includes `fix_command` on error
- Graceful degradation: keyword search works without ChromaDB
- Startup self-check: Chroma openable, DB tables present, version OK

### CLI
- Every error message: WHAT + WHY + FIX
- Every command has `--help` + a docstring
- Status / doctor must reflect actual state, never lie

### Hooks
- Hook scripts MUST be idempotent (re-running is safe)
- `hooks uninstall` removes everything `hooks install` wrote
- Hook injection respects user keys in `~/.claude/settings.json`

### IDE config writes
- Atomic: tmp → fsync → rename
- Merge: never overwrite user keys
- Verify: read back after write to confirm

---

## Graceful degradation matrix

What still works when each subsystem fails:

| Down | Still works |
|---|---|
| **Cache file corrupt** (manifest.yaml, digest.jsonl, AGENTS.md) | Decisions persist in canonical JSONL (P9 invariant). Cache regenerates via `codevira sync` after corruption. Read paths log + skip bad lines (`jsonl_store`) — they never crash. v3.0.0+ writers go through `mcp_server.storage.atomic` for crash + concurrency safety. |
| **Two MCP servers racing** (Claude Desktop + Cursor on same project) | All writes go through `atomic.file_lock` (Posix `fcntl.flock` + Windows sentinel). 20-subprocess cross-process stress test pins the contract. Last-writer-wins replaced with serialized read-modify-write. |
| **HuggingFace network down** | n/a in v2.2.0+ (sentence-transformers removed). Historical pain point — keeping the row for context. |
| **`~/.codevira/global.db` corrupt** | Auto-reinit + warn; per-project state preserved |
| **Watcher crash** | All MCP tools; manual `codevira index --full` available |
| **One hook script broken** | MCP server alone; other hooks continue |
| **Disk full** | Operations refuse with clear "disk full" message; never silently truncate |
| **Permission denied** | Clear error with `chmod` / `chown` remediation |

Every new feature: ask "if this fails, what continues to work?"
Document the answer in the docstring AND in the relevant cell of
this matrix.

---

## The continuous-foolproof loop

```
1. Bug found in production
       ↓
2. Identify which P-principle was violated
       ↓
3. Fix the bug
       ↓
4. Add a regression test to test_first_contact.py OR test_product_invariants.py
       ↓
5. If the test exposes a pattern (not just one bug), strengthen the
   relevant P principle in the SKILL.md
       ↓
6. Update this charter's bug table (the post-mortem section above)
```

Goal: every category of bug we ship gets encoded as a principle.
The next bug in that category fails the gauntlet before reaching users.

---

## Anti-patterns the charter forbids

These are non-negotiable. A PR that introduces any of these is
blocked at review:

1. **Returning 0 results without a reason field.**
2. **Catching exceptions and silently continuing without logging.**
3. **Retry loops without a max-iteration limit.**
4. **Error messages that say "failed" without saying WHY.**
5. **New subsystems without a `codevira doctor` check.**
6. **New subsystems without a clean uninstall path.**
7. **Writes that overwrite user keys in shared config files.**
8. **DB migrations without a rollback script.**
9. **"It works in the happy path" without walking the sad paths.**
10. **Skipping P1-P10 checklist with "looks fine."**

---

## Reference

- Skill enforcement: [`.claude/skills/foolproof-product/SKILL.md`](../.claude/skills/foolproof-product/SKILL.md)
- Machine tests: [`tests/e2e/test_product_invariants.py`](../tests/e2e/test_product_invariants.py)
- Bug catalog: [ROADMAP.md § v2.1](../ROADMAP.md#-v21--new-user-first-contact--reliability-hardening)
- Release process: [`docs/release-process.md`](release-process.md)
- Contributor guide: [`CONTRIBUTING.md`](../CONTRIBUTING.md)
