# Antigravity — macOS sandbox blocks torch dlopen

**Tracking:** [Issue #10](https://github.com/sachinshelke/codevira/issues/10)
**Status:** v2.1.2 ships graceful degradation; full semantic search in
Antigravity requires a user-side workaround documented below.

---

## Symptom

When you configure Codevira as an MCP server in Google Antigravity
(the Cascade IDE), every `codevira-*` entry fails the `tools/list`
step with:

```
codevira-udap: failed to get tools: calling "tools/list":
dlopen(/Users/.../torch/lib/libtorch_global_deps.dylib, 0x000A): tried:
  '/Users/.../torch/lib/libtorch_global_deps.dylib' (no such file),
  '/System/Volumes/Preboot/Cryptexes/OS/Users/.../torch/lib/libtorch_global_deps.dylib' (no such file),
  '/Users/.../torch/lib/libtorch_global_deps.dylib' (no such file)
```

The file exists on disk — but Antigravity can't dlopen it.

## Why

Antigravity is a hardened-runtime macOS app. When it spawns Codevira
as a subprocess, the subprocess inherits sandbox / library-validation
entitlements that block `dlopen()` of **unsigned dylibs in non-system
paths**. PyPI-installed `torch` ships unsigned `.dylib` files in
`site-packages/torch/lib/` — exactly the case the sandbox blocks.

The smoking gun: macOS reports a search path of
`/System/Volumes/Preboot/Cryptexes/OS/Users/...` — Cryptex paths are
only tried for sandboxed processes with hardened-runtime entitlements.

## What v2.1.2 does about it

Codevira v2.1.2 ships **graceful degradation** for this scenario:

1. **The MCP server starts cleanly** in Antigravity — torch is no
   longer pre-warmed at startup. `tools/list`, `initialize`, and every
   non-search tool (graph, roadmap, changesets, decisions
   read/write) work normally.
2. **Search tools fall back to BM25-only** when torch fails to load.
   You still get keyword search; you lose semantic ranking.
3. **The first search response carries a `_semantic_warning` field**
   explaining the degradation:
   ```
   "Native library load failed: dlopen(...): no such file. Common
   cause: parent process (e.g. Antigravity) sandbox blocks dlopen of
   unsigned PyPI dylibs. Semantic search degraded to BM25 only."
   ```

So Antigravity + Codevira works for everything **except** semantic
search (`search_codebase` and the semantic side of `search_decisions`).
For the other 80% of Codevira's tools, you have full functionality.

## Workarounds for full semantic search

### Option 1 — Use a different IDE for semantic-heavy work (Recommended)

If you only occasionally need semantic search, switch to Claude Code,
Claude Desktop, Cursor, Windsurf, Continue.dev, or any non-sandboxed
MCP client for those queries. Decisions / graph / roadmap state is
shared across all of them via Codevira's local store — no sync needed.

### Option 2 — Disable Antigravity's hardened-runtime checks (security trade-off)

You can tell macOS to skip the library-validation check for Antigravity
specifically. **This reduces the OS's protection against malicious
dylib injection into Antigravity itself.** Only do this if you trust
the dylibs in your `site-packages/torch/lib/` and you accept the
security trade-off.

```bash
# Find the Antigravity bundle (path may differ by install method)
mdfind "kMDItemKind == 'Application'" | grep -i antigravity
# typical: /Applications/Antigravity.app

# Remove library-validation flag (requires admin password)
sudo codesign --remove-options=library --force --deep --sign - /Applications/Antigravity.app

# Restart Antigravity completely (quit + relaunch, don't just close the window)
```

Verify by relaunching Antigravity and checking that `tools/list`
succeeds on a `codevira-*` MCP entry.

### Option 3 — Pin torch from conda-forge (signed binaries)

Conda-forge's torch builds are codesigned by Anaconda Inc., which
satisfies the library-validation entitlement. Installing Codevira via
conda + conda-forge's torch should work in Antigravity. (Not tested as
of v2.1.2; report your findings in Issue #10 if you try this path.)

### Option 4 — Wait for Antigravity to relax library validation

This is an Antigravity-side limitation. If they ship an update that
either (a) signs PyPI-installed Python packages on first use, or
(b) drops the library-validation entitlement, the issue resolves
without any Codevira changes.

## Verifying the workaround

After applying a workaround, run a search query that requires semantic
recall:

```
search_decisions("auth flow you architected last week")
```

If the response includes `_semantic_warning`, semantic search is still
disabled. If the response has `retrieval: "hybrid"` or
`"semantic"` and no warning, semantic search is working.

## Reporting

If you have a different setup (Linux + sandbox? Flatpak'd Antigravity?
different macOS version?) and hit this error or a variant, please add
your findings to [Issue #10](https://github.com/sachinshelke/codevira/issues/10)
so we can document the matrix of working / not-working configurations.

## Related

- Issue #10 (the root tracking ticket for this scenario)
- `mcp_server/tools/_decision_embeddings.py` —
  `_decisions_collection_or_none()` is the code path that catches the
  dlopen failure and sets `_semantic_unavailable_reason`.
- `mcp_server/server.py` (search for "issue #10") — explains why
  `prewarm_embedding_model()` was removed from server startup in v2.1.2.
