---
name: foolproof-product
description: |
  Use this skill on ANY code change in codevira's product surface
  (mcp_server/, indexer/). Triggers on Edit/Write to product code,
  on phrases like "fix bug", "add feature", "handle error", or any
  modification to commands the end-user runs. Enforces 10 product-
  reliability principles: no silent failures, atomic state mutations,
  defensive parsing, bounded resources, predictable detection,
  reversible operations, helpful error messages, graceful degradation,
  observability, self-diagnosis. Refuses to ship code that violates
  any of them.
---

# Foolproof product — non-skippable code invariants

The release-readiness skill prevents shipping a release without
evidence. THIS skill prevents writing code that misbehaves once
shipped. The two are complementary; both must be enforced.

v2.0.0 shipped with 23 bugs (A–O) because individual code changes
satisfied "the function returns a value" but violated the
product invariants below. This skill makes those invariants explicit
and checkable.

## The 10 principles (P1–P10)

For every code change you make in `mcp_server/` or `indexer/`, walk
through this checklist. Skipping any P is a discipline breach.

### P1 — No silent failures, ever

For every code path that does work:
- Returns >0 results → succeeds with concrete counts in the response
- Returns 0 results → MUST emit a clear "no results because <reason>"
  message with a `fix_command` field

**Anti-pattern (the v2.0 bug pattern):**
```python
def index() -> dict:
    matched = [f for f in files if matches(f)]
    if not matched:
        return {"chunks": 0}   # ← silent zero. NEVER.
    return {"chunks": len(matched)}
```

**Correct:**
```python
def index() -> dict:
    matched = [f for f in files if matches(f)]
    if not matched:
        return {
            "chunks": 0,
            "warning": f"No files matched. watched_dirs={watched_dirs} "
                       f"file_extensions={extensions}",
            "fix_command": "codevira configure",
        }
    return {"chunks": len(matched), "files": [str(f) for f in matched]}
```

### P2 — Self-diagnose on startup

Every long-running entry point (MCP server boot, watcher start,
indexer process) must run startup health checks:
- Database files openable + expected tables present
- Schema version matches (migrate forward OR halt with error)
- Required dependencies importable (chromadb, sentence-transformers)
- File system permissions on data dir

On any failure: halt with a clear remediation hint. Never start in
a degraded state without telling the user.

### P3 — Atomic state mutations

Any operation that writes external state (files, DB rows, IDE configs)
must be atomic. The temp-file → fsync → rename pattern is the rule:

```python
def write_config_atomic(path: Path, content: dict) -> None:
    """Write JSON atomically — readers never see half-written state."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, indent=2))
    os.fsync(tmp.open("rb").fileno())  # force kernel buffer to disk
    tmp.replace(path)                  # atomic rename on POSIX
```

Database mutations: SQLite transactions (begin → ... → commit OR
rollback on exception). Multi-step operations: rollback on partial
failure.

### P4 — Defensive parsing

Every external input is parsed with try/except + fail-safe defaults:

```python
def load_config(path: Path) -> dict:
    """Load config with safe fallback. Never crashes on bad input."""
    if not path.is_file():
        return DEFAULT_CONFIG  # log a warning
    try:
        return yaml.safe_load(path.read_text()) or DEFAULT_CONFIG
    except yaml.YAMLError as e:
        logger.warning("Bad config at %s: %s — using defaults", path, e)
        return DEFAULT_CONFIG
```

User input that arrives malformed should produce a log warning + safe
default, NOT a crash. The crash is a P4 violation.

### P5 — Bounded resources

No unbounded loops. No retry without a circuit breaker. No log line
written without a rate limiter.

The 41-crash UDAP log was P5 violation: the watcher hit a corrupted
Chroma store and retried every file in sequence with no halt
condition.

**Pattern:**
```python
class CircuitBreaker:
    def __init__(self, max_consecutive_failures: int = 5):
        self.failures = 0
        self.limit = max_consecutive_failures

    def call(self, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            self.failures = 0
            return result
        except Exception:
            self.failures += 1
            if self.failures >= self.limit:
                raise CircuitOpen(f"halted after {self.limit} consecutive failures")
            raise
```

Apply at every retry boundary.

### P6 — Predictable detection

If `configure` says "8 files will be indexed" and `index` then matches
0 files, **that's a bug**. The same matcher must drive both. Single
source of truth for "what counts as a source file."

Concrete rule: `discover_source_files()` is THE matcher. Any code in
the indexer pipeline that filters files must call it, not roll its
own.

### P7 — Reversible operations

Every operation that touches user state has a documented reverse:

| Operation | Reverse |
|---|---|
| `codevira setup` writes IDE configs | `codevira clean` removes them |
| `codevira init` registers in global.db | `codevira clean --project <key>` |
| Hook installation writes to `~/.claude/hooks/` | `codevira hooks uninstall` |
| DB migration | Rollback script in `indexer/migrations/`  |

For IDE configs specifically: ALWAYS merge, never overwrite. User keys
must survive a codevira setup/refresh.

### P8 — Helpful error messages

Every error visible to the user (CLI output, MCP tool response, log)
follows the WHAT + WHY + FIX pattern:

```
Bad:
  ✗ Error: file not found

Good:
  ✗ Cannot read config at ~/.codevira/projects/foo/config.yaml:
    file does not exist. Project may not be initialized.
    Fix: run `codevira init` from the project root.
```

For MCP tool responses, include a `fix_command` field as the
codevira convention.

### P9 — Graceful degradation

Single subsystem failure must NOT cascade. Build with this matrix:

| Subsystem | Down state | What still works |
|---|---|---|
| ChromaDB corrupted | Keyword search via SQLite | get_node, decisions, graph, roadmap |
| HuggingFace network | Cached model | Everything (until model needs re-download) |
| `~/.codevira/global.db` corrupt | Auto-reinitialize, warn | Per-project state preserved |
| Watcher crash | Manual `codevira index --full` | All MCP tools |
| Hook script broken | MCP server alone | Memory + retrieval |

Every new feature: ask "if this subsystem fails, what continues to work?" Document the answer in the docstring.

### P10 — Observability

Every operation logged with structured data (not bare strings):

```python
logger.info(
    "indexed_files",
    extra={
        "project": project_name,
        "files": len(matched),
        "chunks": chunk_count,
        "elapsed_ms": int(elapsed * 1000),
    },
)
```

`codevira doctor` is the truth oracle: it reads actual state, never
infers. Crash log auto-rotates (30 days OR on version change). Audit
log for any sensitive operation (IDE config write, DB migrate, hook
install).

## The mandatory checklist (before any product code change)

Before calling Edit/Write on `mcp_server/` or `indexer/`:

```
For this change, I am affecting:
  - Subsystem: <indexer | mcp tool | hook | CLI | watcher>
  - Files touched: <list>

Principle check:
  P1 No silent failures:       [ ] All zero-result paths emit reason + fix_command
  P2 Self-diagnose:            [ ] Startup checks updated if subsystem touched
  P3 Atomic mutations:         [ ] Any file/DB write uses atomic pattern
  P4 Defensive parsing:        [ ] External input has try/except + fallback
  P5 Bounded resources:        [ ] Loops bounded; retries circuit-broken
  P6 Predictable detection:    [ ] Single matcher; no parallel implementations
  P7 Reversible:               [ ] Uninstall counterpart exists/documented
  P8 Helpful errors:           [ ] WHAT + WHY + FIX in every error message
  P9 Graceful degradation:     [ ] If <this fails>, <these still work>
  P10 Observability:           [ ] Structured log; doctor check covers new state
```

Skipping a P without justification is a discipline breach. If a P
genuinely doesn't apply (e.g. a refactor with no state mutation
skips P3), say so explicitly.

## Anti-patterns this skill refuses

- Returning 0 results without a reason field.
- Writing files non-atomically.
- Catching exceptions and silently continuing (the P4 dark form).
- Retry loops without a max count.
- Error messages that say "failed" without saying WHY.
- New subsystems with no doctor check.
- New subsystems without a clean uninstall path.
- "It works in the happy path, ship it." All sad paths must be
  walked too.

## Why this exists

23 bugs shipped in v2.0.0 (A–O) all trace to one or more violated
P-principles. The post-mortem:

| Bug | Violated P |
|---|---|
| A: configure/index disagree | P6 (single source of truth) |
| B: index says "up to date" with 0 chunks | P1 (silent failure) |
| C: status doesn't warn on uninitialized | P1, P10 |
| D: config split (in-repo + centralized) | P3 (atomic), P6 |
| E: docs-only silent 0 chunks | P1 |
| F: init drops top-level files | P6 |
| Chroma HNSW crash storm | P2 (startup check), P5 (circuit breaker) |
| Claude Desktop disconnects | P9 (graceful degradation under timeout) |
| 41-crash log spam | P5 (rate limit), P10 (auto-rotate) |

If P1–P10 had been enforced at code-write time, none of these would
have shipped. This skill makes the enforcement structural.
