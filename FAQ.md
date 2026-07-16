# Frequently Asked Questions

---

## Big Picture

### What is Codevira in one line?

A persistent memory layer for AI coding agents — so Claude Code, Cursor, Windsurf, and Antigravity all share the same project context, decisions, and history on your local machine.

### Who is this for?

**Solo developers** working on local projects with AI coding tools. If you switch between AI agents (Claude Code in the morning, Cursor for autocomplete, Antigravity to test), Codevira makes them all share one memory of your project.

**Local-first, and team-safe as of v3.7.0.** The decision log lives in the committed in-repo `.codevira/` (git-diffable, team-shareable); only the cross-project `global.db` + rebuildable caches sit under `~/.codevira/`. Two engineers can share one repo without silently losing decisions on merge — `codevira init` installs a deterministic git merge driver for the decision log, and `codevira repair-ids` fixes an already-merged store.

### I'm on v1.x — should I upgrade to 2.0?

Probably yes, but read [MIGRATING.md](MIGRATING.md) first. Three default
behaviors changed (`init` indexes more, `agents` renders fewer files, and the
old `register` command was removed — use `setup`, or `init`, which since
v3.7.0 registers one user-scope server), all opt-out-able if you want the
legacy behavior. No data loss; existing `~/.codevira/global.db` migrates
safely. The upgrade is `pipx install --upgrade codevira`.

The biggest reason to upgrade: 2.0 introduces the **active guardian
engine** — codevira now intercepts every AI tool call and can block /
warn / inject context (8 engine policies). Pre-2.0 was passive: the AI
looked things up only when it remembered to. Post-2.0 is active: codevira
surfaces relevant prior decisions, blocks reverts of `do_not_revert`
items, etc.

If you don't want the active layer (just the memory tools), you can
disable the engine with `CODEVIRA_ENGINE=0` in your shell — the rest
of codevira works identically to v1.x.

### What problems does Codevira actually solve?

1. **No more re-explaining your project every session** — `get_session_context()` brings any AI agent up to speed in one call
2. **Decisions stick** — `do_not_revert` flags + searchable decision log mean today's AI doesn't undo last week's careful work
3. **Cross-tool continuity** — switch between Claude Code, Cursor, Windsurf, Antigravity; project state carries over
4. **Lower token cost** — summary-first tool design means agents query for what they need, not "give me everything"

### Does Codevira send my code anywhere?

No. Everything runs locally. The context graph is a SQLite database in `~/.codevira/`, and decisions live in `<repo>/.codevira/decisions.jsonl`. Decision search is pure keyword/BM25 (FTS5) — no embeddings, no model download, nothing to phone home about. Session logs, decisions, skills — all local. Your code never leaves your machine.

---

## Setup

### How do I install Codevira?

```bash
# Recommended: global install via pipx
pipx install codevira

# Alternative: pip
pip install codevira
```

The full toolkit installs out of the box — no ML stack, no embedding model, no download on first use. Codevira ships with no vectors and no machine-learning dependencies, so the production install stays small (~66 MB pipx venv) and there's no model to load — warm tool calls return in ~2 ms and the server cold-starts in well under a second. A typical project's memory is ~1–2 MB on disk.

### Do I need to run `codevira init` for every project?

You register the MCP server **once**, not per project. Since v3.7.0 `codevira init` (and `codevira setup`) writes a single **user-scope** server that resolves the active project from your editor's workspace at runtime — so N projects don't create N codevira entries in your IDE.

But you **do** run `codevira init` once in each repo you want tracked. As of v3.7.0 that's the explicit opt-in: codevira tracks **only** projects you've `init`-ed. A project you merely open in your editor stays **inert** — its tools return a "run `codevira init`" hint and nothing is written — so `~/.codevira/projects/` never fills with projects you never chose. `init` scaffolds that project's `.codevira/` and detects its languages. Want a per-project MCP entry instead of the shared one (e.g. an IDE that can't advertise workspace roots)? Run `codevira init --per-project`. Want the old track-everything behavior? Set `CODEVIRA_AUTO_ADOPT=1`.

### Do I need to run the indexer every time?

No. Run `codevira init` once when you first set up a project. After that:
- The **live file watcher** auto-reindexes on every save (starts with the MCP server)
- The **git post-commit hook** (auto-installed by init) reindexes on every commit
- You can manually run `codevira index` or `codevira index --full` if needed

### Does codevira use embeddings or semantic search?

No — not in the default install, and that's deliberate. Decision search is pure keyword/BM25 ranking via SQLite FTS5 (`search_decisions(query)`). There are no vectors, no `sentence-transformers`, no ChromaDB, and nothing to download on first use. That's why a project's memory is ~1–2 MB, the production install is ~66 MB, and warm tool calls return in ~2 ms (no model to load).

An opt-in, off-by-default `[semantic]` path is on the roadmap for users who want vector recall on top of keyword search, but it does not ship in the default install — everything above works with zero ML dependencies.

### Does this work with non-Python projects?

Yes — with an honest split between two axes:

- **Full code intelligence** (code graph, blast-radius, `get_signature`, `get_code`): Python (stdlib `ast`) plus TypeScript, TSX, JavaScript, JSX, Go, and Rust (bundled tree-sitter grammars).
- **Language-agnostic memory** (decision capture + search, cross-IDE `AGENTS.md`, roadmap, sessions, skills, preferences): works for **any** language. For a language outside the code-graph set, the graph/symbol tools don't apply — the AI just `Read`s the file directly — but all of codevira's decision memory and enforcement still work.

So a Java, Ruby, or C# project gets the full cross-IDE decision memory and enforcement; it just doesn't get the tree-sitter code graph. `codevira init` auto-detects the language from project markers (`Cargo.toml`, `go.mod`, `tsconfig.json`, `pyproject.toml`, `package.json`, etc.) for the memory layer regardless.

### Can I use Codevira on a monorepo?

Yes. As of v1.5, `codevira init` scans your project tree and automatically detects all directories containing source files. For a typical monorepo:

```
detected: apps, packages, libs, scripts
```

If you want to override, use CLI flags:
```bash
codevira init --dirs "services/api,services/worker,shared/lib"
```

Or edit `.codevira/config.yaml` after init.

### Do I need a GitHub account or any external service?

No. Codevira runs entirely locally. The context graph is a SQLite database, session logs and decisions are local files, and decision search is keyword-only (FTS5 — no external service, no model). Nothing is sent anywhere.

The only file outside your project is `~/.codevira/global.db` — a local SQLite database for cross-project intelligence.

### How does cross-project memory work?

When you use Codevira on multiple projects, learned preferences and rules are synced to `~/.codevira/global.db`. When you initialize a new project, it imports relevant intelligence from your other projects — so new projects benefit from day one.

This happens automatically. No configuration needed.

### How do I back up Codevira's memory or move it to a new machine?

From v3.3.0 there's a one-command path:

```bash
# old machine — bundles .codevira/ + your global learning into one tar.gz
codevira export setup

# new machine — restores the project memory and merges global learning
codevira import codevira-setup-<timestamp>.tar.gz
codevira init   # re-register IDE hooks, rebuild the index
```

`import` refuses to overwrite an existing non-empty `.codevira/` unless you pass `--force` (which backs it up to `.codevira.pre-import-<ts>/` first). Don't commit the archive — it may contain learning from your other projects.

If you prefer the manual route (or are on < 3.3.0), Codevira's state splits into three tiers, and only one of them needs deliberate care:

**1. `.codevira/` — the canonical project memory** (decisions, sessions, outcomes, skills, reflections, archived working memory, config). By default this is committed to your repo — `codevira init` only gitignores the cache and warns if your `.gitignore` blocks `.codevira/`. If it's committed, `git clone` on the new machine carries the memory automatically.

If your project gitignores `.codevira/` (some teams prefer per-developer memory), git won't carry it. Copy the `.codevira/` directory across — the JSONL files in it ARE the canonical store, so a plain directory copy (or tarball) is a complete backup:

```bash
tar -czf codevira-backup.tar.gz .codevira/
```

> Note: on codevira **< 3.3.0**, `codevira export decisions` dumped only the legacy pre-v3 SQLite store and exported 0 rows on v3.x projects. From 3.3.0 it reads the canonical JSONL store (with per-table legacy fallback), so either the directory copy or `codevira export` works.

**2. `.codevira-cache/` — per-machine, rebuildable.** Live working memory, activity heatmap, FTS index. Don't copy it. If you have uncommitted working-memory entries you want to keep, archive them into the canonical store first:

```bash
codevira working commit <session_id>
```

**3. `~/.codevira/global.db` — cross-project intelligence.** Lives in your home directory, not the repo. Copy `~/.codevira/` to the new machine if you want learned preferences and rules from your other projects to carry over; otherwise it rebuilds gradually as you use Codevira on each project.

On the new machine: install Codevira, run `codevira init` (or the setup wizard) in each project to re-register the MCP server and IDE hooks, and pull/copy the memory as above. The SQLite graph index rebuilds from source — it doesn't need to be copied.

---

## Usage

### How does cross-tool continuity actually work?

You start a session in Claude Code: "build the auth flow." You make decisions, write code, the AI calls `write_session_log()` at the end. Now you switch to Cursor for some autocomplete work — Cursor's AI calls `get_session_context()` and instantly sees:

- Current phase: "Auth flow implementation"
- Next action: "wire refresh-token rotation into `src/auth.py`"
- Recent decisions: "Using JWT with 24h expiry (not session cookies — see decision log)"
- Working memory (recent scratchpad): "verified the login handler rejects expired tokens"
- Files touched recently: `src/auth.py`, `tests/test_auth.py`
- Style: keep answers short, tests first

No need to re-explain. Switch to Antigravity to run tests — same memory. The AI tools are interchangeable; the project state is persistent.

### Which transport should I use — stdio or HTTP?

**Short answer: stdio.** `codevira setup` (or `codevira init`) sets this up as one user-scope server, and it handles every project automatically.

| Client | Use | Why |
|--------|-----|-----|
| Claude Desktop (app) | stdio | Desktop app only supports `command`+`args` config |
| Claude Code CLI | stdio | Both work; stdio handles multi-project automatically |
| Cursor, Windsurf | stdio | These tools use `command`+`args` config |
| Google Antigravity | stdio | Same |

**Stdio** (default + recommended): the MCP client spawns `codevira` as a subprocess for each open workspace, bound to that project via the client's workspace roots, with its own memory from `~/.codevira/projects/<key>/`. One user-scope registration covers every project — zero per-project config after `codevira setup`.

**HTTP/HTTPS** — **Preview in v1.7, single-project only.** The HTTP server binds to one project at startup and cannot switch contexts per request. Useful only when:
- You need Claude.ai (web) support (single-project anyway)
- You're running Codevira in a headless/remote environment
- You want a long-lived process for diagnostics

**Multi-project HTTPS is still on the roadmap** (see [ROADMAP.md](ROADMAP.md)) — when implemented, the server will read the MCP `initialize` handshake's `rootUri` to route each AI session to the right project. Until then, stdio is the answer for multi-project work, and `codevira setup` configures it for every detected AI tool in one command.

### How do I configure Claude Desktop specifically?

Claude Desktop requires the `command`+`args` format in its own config file — the `url` format is not supported.

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "codevira": {
      "command": "/path/to/codevira",
      "args": ["--project-dir", "/path/to/your-project"]
    }
  }
}
```

Find the full binary path with: `which codevira`

### How do I use the HTTP transport with Claude Code?

Start the server in a terminal:
```bash
codevira serve --https --port 7443 --project-dir /path/to/your-project
```

Register in `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "codevira": {
      "url": "https://localhost:7443/mcp"
    }
  }
}
```

Node.js (used by Claude Code) requires a trusted HTTPS certificate. To set that up once:
```bash
brew install mkcert && mkcert -install
launchctl setenv NODE_EXTRA_CA_CERTS "$(mkcert -CAROOT)/rootCA.pem"
echo 'export NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem"' >> ~/.zshrc
```

Then restart Claude Code. Certs are auto-generated at `~/.codevira/certs/` on first `serve --https`.

### My agent isn't calling the MCP tools — why?

Common causes:

1. **MCP config not written** — run `codevira init` in your project; it auto-injects config into Claude Code, Cursor, Windsurf, and Antigravity
2. **IDE needs restart** — most AI tools require a restart to pick up new MCP servers
3. **Binary not in PATH** — check that `codevira` is accessible; if installed via pipx, verify `~/.local/bin` is in your PATH
4. **Wrong project directory** — the config's `cwd` (stdio) or `--project-dir` (HTTP) must point to the project where `.codevira/config.yaml` exists
5. **Claude Desktop config format** — Claude Desktop uses `command`+`args`, not `url`. The `url` format only works in Claude Code CLI

Test manually: run `codevira` from your project directory — it should start without errors.

### What happens if I skip PROTOCOL.md?

Your agent will still work — the MCP tools are always available. But without following the session protocol, agents won't orient to the current phase, won't check blast radius before changes, and won't write session logs. The protocol is what makes memory accumulate across sessions.

### Can multiple developers share the same graph?

The context graph lives in `.codevira/graph/graph.db` (SQLite). It's git-ignored by default because it contains local index data and rebuilds from source. The canonical, team-shareable memory is the JSONL in `.codevira/` (decisions, skills, reflections) — commit that to share it across the team; the graph index stays local and per-machine.

**v3.7.0 makes this safe for real cross-engineer work.** Before, two engineers on two branches could both mint the same decision id; `git merge` combined the appended lines cleanly and one decision was silently shadowed on read. Now `codevira init` installs a **git merge driver** that resolves those collisions deterministically (both sides converge), `read_merged` warns if a collision slips through, and `codevira repair-ids [--apply]` fixes an already-merged store. Commit `.gitattributes` so teammates inherit the driver mapping (each still runs `codevira init` once to configure it locally; `codevira doctor` flags the gap otherwise).

### Can I use Codevira without a roadmap?

Yes. If the roadmap doesn't exist, `get_roadmap()` auto-creates a minimal Phase 1 stub on first call. You can use all graph, search, decision, and code-reader features without ever touching the roadmap.

### Why is my agent reading the roadmap from a different project?

As of **v3.7.0** codevira registers ONE user-scope server (a single constant `codevira` entry) that resolves the active project from your editor's workspace roots at runtime — so this cross-workspace bleed shouldn't happen. If it does, your client isn't advertising workspace roots. Two fixes:

- **Pin explicitly** — set `CODEVIRA_PROJECT_DIR=/path/to/project` in that server's `env` (or run with `--project-dir`), which overrides roots resolution.
- **Per-project entries** — run `codevira init --per-project` (or `CODEVIRA_INIT_PER_PROJECT=1`) to register a distinct server per project instead of the shared one.

Run `codevira doctor` — the `project_binding` check shows exactly which project the server resolved to and why.

### What is the function-level call graph?

New in v1.5. Codevira now tracks which functions call which — not just file-level imports. Use:
- `query_graph(file, symbol, "callers")` — who calls this function?
- `query_graph(file, symbol, "callees")` — what does this function call?
- `get_impact(file_path)` — blast radius: who depends on this code before you change it

---

## Architecture

### What's the difference between the graph and the decision store?

| | Context Graph | Decision Store |
|---|---|---|
| **Storage** | SQLite (`graph.db`), per-machine, rebuilds from source | JSONL (`.codevira/decisions.jsonl`), git-tracked |
| **Content** | File metadata, dependencies, symbols, call edges | Decisions, fix history, rationale |
| **Used by** | `get_node`, `get_impact`, `query_graph`, `get_signature`, `get_code` | `search_decisions`, `record_decision` |
| **Search** | Graph traversal (callers/callees, blast radius) | Keyword/BM25 (FTS5) — no vectors, no model |
| **Best for** | "What does this file do? Who calls this function?" | "What did we decide about X, and why?" |

### Does Codevira send my code anywhere?

No. Everything runs locally:
- Context graph is a local SQLite database (rebuilds from source)
- Decisions live in `.codevira/decisions.jsonl` and are searched with FTS5 keyword/BM25 — no embeddings, no model
- Session logs and skills are local files
- Global memory (`~/.codevira/global.db`) is a local file
- The MCP server runs as a local subprocess

Your code never leaves your machine.

### How does decision search work — does it need a model?

No model, no download. `search_decisions(query)` ranks the decision log with SQLite FTS5 (keyword + BM25), so it's instant, deterministic, and adds zero ML dependencies. An opt-in `[semantic]` vector path is on the roadmap, but the default install is keyword-only by design.

---

## Troubleshooting

### The MCP server crashes on startup

```bash
# Verify the binary works
codevira --help

# Test server startup from your project dir
cd your-project
codevira
```

Common causes: wrong Python version (requires 3.10+), missing `mcp` package, or `.codevira/config.yaml` not found (run `codevira init` first).

### Database corruption

If the SQLite graph database is corrupted, delete it and rebuild from source:
```bash
rm -rf .codevira/graph/graph.db
codevira index --full
```

The decision store (`.codevira/decisions.jsonl`) is plain JSONL — it's never an index to rebuild; the FTS5 search index over it lives in the rebuildable cache.

### The index is out of date

```bash
# Rebuild from scratch
codevira index --full

# Or re-index just stale files
codevira index
```

The live file watcher and git post-commit hook normally keep this current, so you rarely need to run it by hand.

### `get_node()` returns `index_status.stale: true`

The file has been modified since the last index build. The graph node is still valid, but symbol/call-edge details may be outdated. Run `codevira index` (or just save the file again — the watcher reindexes on save) to refresh it.

---

## Contributing & Issues

### How do I report a bug?

Open a [bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md) on GitHub. Include your OS, Python version, AI tool, and the full error message.

### How do I request a feature?

Open a [feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md). Describe the problem you're trying to solve.

### I found a security vulnerability

Please **do not** open a public issue. Email **sachin@prayog.io** directly. See [SECURITY.md](SECURITY.md).

### How do I contribute code?

Read [CONTRIBUTING.md](CONTRIBUTING.md) — covers forking, branching, PR process, and AI-assisted contribution workflows.
