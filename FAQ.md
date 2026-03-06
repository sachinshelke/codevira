# Frequently Asked Questions

---

## Setup

### Do I need to run the indexer every time?

No. Run it once with `--full` when you first set up Codevira. After that, the git post-commit hook (installed via `bash .agents/hooks/install-hooks.sh`) automatically re-indexes any files you change on every commit. You can also run `python indexer/index_codebase.py --watch` during active development for real-time sync.

### What is ChromaDB and do I need to install it separately?

ChromaDB is the vector database Codevira uses to power semantic code search. You don't install it separately — it's included in `requirements.txt` and installed with `pip install -r requirements.txt`.

### Does this work with non-Python projects?

Yes, with one limitation. The context graph, roadmap, changeset tracking, semantic search, and all session management tools work for any language. The only Python-specific features are `get_signature`, `get_code`, and auto-generated graph stubs — all of which use Python's AST. For TypeScript, Go, Rust, and other languages, those three features are unavailable but everything else works fully.

See the [Language Support](README.md#language-support) table for details.

### Can I use Codevira on a monorepo?

Yes. Set `watched_dirs` in `config.yaml` to include all the subdirectories you want indexed:
```yaml
project:
  watched_dirs: ["services/api", "services/worker", "shared/lib"]
  file_extensions: [".py"]
```
Each directory is indexed and graphed. The blast-radius tool (`get_impact`) works across all of them.

### Do I need a GitHub account or any external service?

No. Codevira runs entirely locally. ChromaDB is embedded (no server needed), graph files are YAML on disk, and session logs are local files. Nothing is sent anywhere.

---

## Usage

### My agent isn't calling the MCP tools — why?

The most common cause is the MCP server not being registered correctly. Check:

1. The server is listed in your AI tool's MCP config (see [Quick Start](README.md#quick-start))
2. The path to `mcp-server/server.py` is correct relative to your project root
3. The server starts without errors: `python .agents/mcp-server/server.py`

If the server starts but tools aren't appearing, restart your AI tool — most require a restart to pick up new MCP servers.

### What happens if I skip PROTOCOL.md?

Your agent will still work — the MCP tools are always available. But without following the session protocol, agents won't orient to the current phase, won't check blast radius before changing files, and won't write session logs. Over time the graph and decision history drift from reality, and future agents lose the accumulated context. The protocol is what makes Codevira valuable across sessions.

### Can multiple developers share the same graph?

Yes — commit your `graph/` directory to version control. The graph files are plain YAML, merge cleanly, and are designed to be shared. Session logs (`logs/`) and the code index (`codeindex/`) are git-ignored because they're either personal or binary — those stay local.

### Can I use Codevira without a roadmap?

Yes. If `roadmap.yaml` doesn't exist, `get_roadmap()` auto-creates a minimal Phase 1 stub on first call. You can use all graph, search, and changeset features without ever touching the roadmap. It's there when you need it, invisible when you don't.

### Can I use this on an existing project that has no graph nodes yet?

Yes. Run:
```bash
python indexer/index_codebase.py --full --generate-graph
```
This creates auto-generated graph stubs for all your Python files. Stubs are marked `auto_generated: true` and have basic metadata inferred from imports and docstrings. Enrich them with `rules`, `stability`, and `do_not_revert` flags over time as your agents work on them.

---

## Architecture

### What's the difference between the graph and the code index?

They serve different purposes:

| | Context Graph | Code Index |
|---|---|---|
| **Storage** | YAML files in `graph/` | ChromaDB in `codeindex/` |
| **Content** | File metadata, rules, dependencies, decisions | Chunked source code, embedded as vectors |
| **Used by** | `get_node`, `get_impact`, `list_nodes` | `search_codebase` |
| **Best for** | "What does this file do? What are its rules?" | "Where in the codebase is X implemented?" |
| **Updated by** | Agents via `update_node`, `add_node` | Indexer CLI or git hook |

Both work together — a typical session uses the graph for orientation and the code index for finding existing patterns.

### Does Codevira send my code anywhere?

No. Everything runs locally:
- ChromaDB is an embedded database (no external server)
- Embeddings are generated locally using `sentence-transformers` (no API calls)
- Graph files, session logs, and roadmap are plain files on disk
- The MCP server runs as a local process

Your code never leaves your machine.

### What model does the semantic search use?

By default, `all-MiniLM-L6-v2` from sentence-transformers — a fast, lightweight embedding model that runs entirely on CPU. It's downloaded once on first use (~90MB) and cached locally. You can swap it out in `indexer/chunker.py` if you prefer a different model.

---

## Troubleshooting

### The index is out of date — how do I fix it?

```bash
# Rebuild from scratch
python indexer/index_codebase.py --full

# Or re-index just the stale files
python indexer/index_codebase.py
```

You can also ask your agent to call `refresh_index(["path/to/file.py"])` mid-session — it re-embeds specific files without a full rebuild.

### `get_node()` returns `index_status.stale: true` — what does that mean?

The file has been modified since the last index build. The graph node is still valid, but `search_codebase()` results for that file may be outdated. Call `refresh_index(["path/to/file.py"])` to re-embed it, or run the indexer CLI.

### The MCP server crashes on startup — what do I check?

```bash
# Check dependencies are installed
pip install -r requirements.txt

# Run the server directly to see the error
python .agents/mcp-server/server.py
```

Common causes: missing `pyyaml`, wrong Python version (requires 3.10+), or `mcp` package not installed.

### My graph file has no nodes — why?

If you skipped `--generate-graph` during setup, the graph directory will be empty. Run:
```bash
python indexer/index_codebase.py --generate-graph
```
Or ask your agent to call `refresh_graph()` — it scans for unregistered files and creates stubs.

---

## Contributing & Issues

### How do I report a bug?

Open a [bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md) on GitHub. Please include your OS, Python version, AI tool, and the full error message.

### How do I request a feature?

Open a [feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md). Describe the problem you're trying to solve — not just the solution.

### I found a security vulnerability — what do I do?

Please **do not** open a public issue. Email **sachin@prayog.io** directly. See [SECURITY.md](SECURITY.md) for the full policy.

### How do I contribute code?

Read [CONTRIBUTING.md](CONTRIBUTING.md) — it covers forking, branching, PR process, and how to contribute using an AI agent (which works great with Codevira).
