# Antigravity troubleshooting

## Current — v3.7.0: `/ is a system directory` / `initialize: EOF`

**Symptom.** After upgrading to v3.7.0, opening a project in Antigravity shows:

```
codevira: Error: / is a system directory, not a project. The MCP server
cannot start with this project root. ... : calling "initialize": EOF
```

**Cause.** v3.7.0 registers a single *global* `codevira` MCP entry (no
`--project-dir`) that relies on the editor telling the server which workspace
is active. Antigravity does **not** support a `cwd` field in `mcp_config.json`
and does not send MCP `roots`, so it spawns the server with working directory
`/`. Codevira's forbidden-root guard correctly refuses `/` and the server
exits — which Antigravity reports as `initialize: EOF`. (Tracked for a proper
fix in v3.7.1; see below.)

**Fix (until v3.7.1).** Antigravity needs **per-project** entries, never the
bare global one.

1. Open your Antigravity MCP config — `~/.gemini/config/mcp_config.json`
   (and, if present, `~/.gemini/antigravity/mcp_config.json`).
2. **Delete the bare `"codevira"` entry** (the one whose `args` is `[]`, with
   no `--project-dir`). Keep every `"codevira-<project>"` entry — those carry
   an explicit `--project-dir` and work correctly.
3. If you also see `codevira-*` entries pointing at folders that no longer
   exist (e.g. old `/tmp/...` test dirs), delete those too — they fail to
   launch.
4. **Restart Antigravity.**

**Registering a new project for Antigravity.** Use per-project mode so `init`
writes a working entry instead of the crashing global one:

```bash
cd /path/to/your-project
codevira init --per-project
```

**Reusable cleanup.** If the bare entry ever reappears (after a plain
`codevira init` or `codevira setup`), this script strips the crash-causing
entries (bare global + dead-directory), backs up first, and keeps valid ones:

```python
# save as fix-codevira-antigravity.py, run: python3 fix-codevira-antigravity.py
import json, os, time, shutil
FILES = [os.path.expanduser("~/.gemini/config/mcp_config.json"),
         os.path.expanduser("~/.gemini/antigravity/mcp_config.json")]
ts = time.strftime("%Y%m%d-%H%M%S")
for p in FILES:
    if not os.path.exists(p): continue
    d = json.load(open(p)); servers = d.get("mcpServers", {})
    drop = [n for n, c in servers.items()
            if n.lower().startswith("codevira")
            and ("--project-dir" not in c.get("args", [])
                 or not os.path.isdir(c["args"][c["args"].index("--project-dir")+1]))]
    if not drop: print(f"· {p}: clean"); continue
    shutil.copy2(p, f"{p}.bak-{ts}")
    for n in drop: del servers[n]
    json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
    print(f"✓ {p}: removed {len(drop)} — {', '.join(drop)}")
print("Restart Antigravity to reload.")
```

**Coming in v3.7.1.** (A) the server will degrade gracefully — serve an inert
"open a project" hint instead of crashing — so no client is ever taken down
by an unresolved root; (B) `init` will stop writing a bare global entry for
Antigravity (per-project only); (C) a `codevira untrack` command will prune
stale IDE-config entries so the manual script above is no longer needed.

---

## Historical — torch dlopen sandbox issue (resolved in v2.2.0)

**Status: no longer applicable.**

This page documented a macOS hardened-runtime sandbox failure where Google
Antigravity could not `dlopen()` PyPI's unsigned `torch` dylibs, which
degraded Codevira's semantic search to keyword-only and (in the worst case)
failed the `tools/list` step entirely.

**v2.2.0 removed semantic search entirely** — there is no ChromaDB, no
sentence-transformers, and no `torch`, so Codevira ships **no native ML
dylibs**. Decision search is pure SQLite FTS5 (keyword + BM25). The dlopen
failure described here can no longer occur, and Codevira works fully in
Antigravity like any other MCP client — no workaround needed.

If you hit a *different* native-library load error in Antigravity (for
example a tree-sitter grammar), please open an issue with the full
`tools/list` error output.

_The original analysis is preserved in the v2.1.2 / v2.2.0 CHANGELOG entries
and [Issue #10](https://github.com/sachinshelke/codevira/issues/10)._
