# Frequently Asked Questions

---

## Big Picture

### What is Codevira in one line?

A persistent memory layer for AI coding agents — so Claude Code, Cursor, Windsurf, and Antigravity all share the same project context, decisions, and history on your local machine.

### Who is this for?

**Solo developers** working on local projects with AI coding tools. If you switch between AI agents (Claude Code in the morning, Cursor for autocomplete, Antigravity to test), Codevira makes them all share one memory of your project.

**Not for teams (yet)** — Codevira is local-first. Every developer has their own `~/.codevira/` directory with their own project memory. Team-shared memory is on the roadmap but not in v2.0.

### I'm on v1.x — should I upgrade to 2.0?

Probably yes, but read [MIGRATING.md](MIGRATING.md) first. Three default
behaviors changed (`init` indexes more, `agents` renders fewer files,
`register` is deprecated in favor of `setup`), all opt-out-able if you
want the legacy behavior. No data loss; existing `~/.codevira/global.db`
migrates safely. The upgrade is `pipx install --upgrade codevira`.

The biggest reason to upgrade: 2.0 introduces the **active guardian
engine** — codevira now intercepts every AI tool call and can block /
warn / inject context (10 hero policies). Pre-2.0 was passive: the AI
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

No. Everything runs locally. The context graph is a SQLite database in `~/.codevira/`. Embeddings (semantic search) are generated locally with `sentence-transformers`. Session logs, decisions, learned rules — all local. Your code never leaves your machine.

---

## Setup

### How do I install Codevira?

```bash
# Recommended: global install via pipx
pipx install codevira

# Alternative: pip
pip install codevira
```

The full toolkit installs out of the box. Adds ~500MB (includes the ML stack for semantic search). Downloads a ~90MB embedding model on first `search_codebase()` call.

For a minimal install (no ML stack, no semantic search), see the README "Minimal install" section.

### Do I need to run `codevira init` for every project?

No. Just run `codevira register` once globally — it injects MCP config into all your AI tools. Then open any project; the first MCP tool call auto-initializes that project in the background.

`codevira init` is still available if you want to set things up explicitly with custom settings, but it's optional.

### Do I need to run the indexer every time?

No. Run `codevira init` once when you first set up a project (or just let auto-init handle it). After that:
- The **live file watcher** auto-reindexes on every save (starts with the MCP server)
- The **git post-commit hook** (auto-installed by init) reindexes on every commit
- You can manually run `codevira index` or `codevira index --full` if needed

### What is ChromaDB and do I need it?

ChromaDB powers the `search_codebase()` semantic search tool. As of v1.7.0 it's included in the default install — all tools work out of the box. The `[search]` extra is kept as a no-op alias for backwards compatibility.

Without it (using the minimal install path), `search_codebase` is hidden from the AI agent's tool list. All other tools still work — context graph, roadmap, changesets, call graph, learning, code reader.

### Does this work with non-Python projects?

Yes. Codevira supports 15+ languages with zero-config auto-detection:

- **Full support** (AST parsing, get_signature, get_code, call graph): Python, TypeScript, Go, Rust
- **Standard support** (graph, search, roadmap, changesets, learning): Java, Kotlin, C#, Ruby, PHP, C, C++, Swift, Solidity, Vue, JavaScript

`codevira init` auto-detects the language from project markers (`Cargo.toml`, `go.mod`, `tsconfig.json`, `pyproject.toml`, `package.json`, etc.).

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

No. Codevira runs entirely locally. The context graph is a SQLite database, session logs are local files, and semantic search (if installed) uses an embedded database. Nothing is sent anywhere.

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
- Recent decisions: "Using JWT with 24h expiry (not session cookies — see decision log)"
- Open changesets: "auth-flow-v2 — 3 of 5 files done"
- Files touched recently: `src/auth.py`, `tests/test_auth.py`
- Top learned preferences: pytest style, dataclasses over Pydantic

No need to re-explain. Switch to Antigravity to run tests — same memory. The AI tools are interchangeable; the project state is persistent.

### Which transport should I use — stdio or HTTP?

**Short answer: stdio.** `codevira register` sets this up globally, and it handles every project automatically.

| Client | Use | Why |
|--------|-----|-----|
| Claude Desktop (app) | stdio | Desktop app only supports `command`+`args` config |
| Claude Code CLI | stdio | Both work; stdio handles multi-project automatically |
| Cursor, Windsurf | stdio | These tools use `command`+`args` config |
| Google Antigravity | stdio | Same |

**Stdio** (default + recommended): the MCP client spawns `codevira` as a subprocess per project. Each project gets its own process with its own memory from `~/.codevira/projects/<key>/`. Zero config after `codevira register`.

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

The context graph lives in `.codevira/graph/graph.db` (SQLite). It's git-ignored by default because it contains local index data. If you want to share graph nodes and rules across a team, you can commit it — but the semantic index (`codeindex/`) should stay local.

### Can I use Codevira without a roadmap?

Yes. If the roadmap doesn't exist, `get_roadmap()` auto-creates a minimal Phase 1 stub on first call. You can use all graph, search, and changeset features without ever touching the roadmap.

### Why is my agent reading the roadmap from a different project?

This happens with global MCP clients like Google Antigravity that share config across workspaces. Each project needs a unique server name:

```json
{
  "mcpServers": {
    "codevira-project-a": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "codevira",
      "args": ["--project-dir", "/path/to/project-a"]
    },
    "codevira-project-b": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "codevira",
      "args": ["--project-dir", "/path/to/project-b"]
    }
  }
}
```

Claude Code, Cursor, and Windsurf use per-project config files, so this issue doesn't apply to them.

### What is the function-level call graph?

New in v1.5. Codevira now tracks which functions call which — not just file-level imports. Use:
- `query_graph(file, symbol, "callers")` — who calls this function?
- `query_graph(file, symbol, "callees")` — what does this function call?
- `analyze_changes()` — function-level risk scoring with test coverage gaps
- `find_hotspots()` — large functions, high fan-in, complexity heatmap

---

## Architecture

### What's the difference between the graph and the code index?

| | Context Graph | Code Index |
|---|---|---|
| **Storage** | SQLite (`graph.db`) | ChromaDB (`codeindex/`) |
| **Required?** | Yes (always) | Optional (`[search]` extras) |
| **Content** | File metadata, rules, dependencies, symbols, call edges, sessions, decisions | Chunked source code as vectors |
| **Used by** | `get_node`, `get_impact`, `query_graph`, `analyze_changes`, all learning tools | `search_codebase` |
| **Best for** | "What does this file do? Who calls this function?" | "Where in the codebase is X implemented?" |

### Does Codevira send my code anywhere?

No. Everything runs locally:
- Context graph is a local SQLite database
- Embeddings (if using `[search]`) are generated locally using `sentence-transformers`
- Session logs and decisions are stored in the local SQLite database
- Global memory (`~/.codevira/global.db`) is a local file
- The MCP server runs as a local subprocess

Your code never leaves your machine.

### What model does semantic search use?

`all-MiniLM-L6-v2` from sentence-transformers — a fast, lightweight embedding model that runs entirely on CPU. Downloaded once on first use (~90MB) and cached locally. Only used if you install with `[search]` extras.

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

If the SQLite database is corrupted:
```bash
rm -rf .codevira/graph/graph.db
codevira index --full
```

If the search index is corrupted:
```bash
rm -rf .codevira/codeindex
codevira index --full
```

### The index is out of date

```bash
# Rebuild from scratch
codevira index --full

# Or re-index just stale files
codevira index
```

You can also ask your agent to call `refresh_index(["path/to/file.py"])` mid-session.

### `get_node()` returns `index_status.stale: true`

The file has been modified since the last index build. The graph node is still valid, but search results may be outdated. Call `refresh_index(["path/to/file.py"])` to re-embed it.

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
