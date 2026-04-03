# Frequently Asked Questions

---

## Setup

### How do I install Codevira?

```bash
# Recommended: global install via pipx
pipx install codevira-mcp

# Alternative: pip
pip install codevira-mcp

# With semantic search (adds ChromaDB + sentence-transformers)
pip install 'codevira-mcp[search]'
```

Then run `codevira init` in any project. It auto-detects everything and auto-injects IDE configs.

### Do I need to run the indexer every time?

No. Run `codevira init` once when you first set up a project. After that:
- The **live file watcher** auto-reindexes on every save (starts with the MCP server)
- The **git post-commit hook** (auto-installed by init) reindexes on every commit
- You can manually run `codevira index` or `codevira index --full` if needed

### What is ChromaDB and do I need it?

ChromaDB powers the `search_codebase()` semantic search tool. It's **optional** — all other 35 tools work without it.

Install with search support: `pip install 'codevira-mcp[search]'`

Without it, you still get the full context graph, roadmap, changesets, call graph, learning, and all code reader tools.

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

---

## Usage

### My agent isn't calling the MCP tools — why?

Common causes:

1. **MCP config not written** — run `codevira init` in your project; it auto-injects config into Claude Code, Cursor, Windsurf, and Antigravity
2. **IDE needs restart** — most AI tools require a restart to pick up new MCP servers
3. **Binary not in PATH** — check that `codevira-mcp` is accessible; if installed via pipx, verify `~/.local/bin` is in your PATH
4. **Wrong project directory** — the config's `cwd` must point to the project where `.codevira/config.yaml` exists

Test manually: run `codevira-mcp` from your project directory — it should start without errors.

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
      "command": "codevira-mcp",
      "args": ["--project-dir", "/path/to/project-a"]
    },
    "codevira-project-b": {
      "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
      "command": "codevira-mcp",
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
codevira-mcp --help

# Test server startup from your project dir
cd your-project
codevira-mcp
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
