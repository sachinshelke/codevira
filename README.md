# Codevira MCP

> Persistent memory and project context for AI coding agents — across every session, every tool, every file.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-1.0.0-orange)](CHANGELOG.md)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)
[![Contributions Welcome](https://img.shields.io/badge/contributions-welcome-brightgreen)](CONTRIBUTING.md)

**Works with:** Claude Code · Cursor · Windsurf · Google Antigravity · any MCP-compatible AI tool

---

## The Problem

Every time you start a new AI coding session, your agent starts from zero.

It re-reads files it has seen before. It re-discovers patterns already established. It makes decisions that contradict last week's decisions. It has no idea what phase the project is in, what's already been tried, or why certain files are off-limits.

You end up spending thousands of tokens on re-discovery — every single session.

**Codevira fixes this.**

---

## What It Does

Codevira is a [Model Context Protocol](https://modelcontextprotocol.io) server you drop into any project. It gives every AI agent that works on your codebase a shared, persistent memory:

| Capability | What It Means |
|---|---|
| **Context graph** | Every source file has a node: role, rules, dependencies, stability, `do_not_revert` flags |
| **Semantic code search** | Natural language search across your codebase — no grep, no file reading |
| **Roadmap** | Phase-based tracker so agents always know what phase you're in and what comes next |
| **Changeset tracking** | Multi-file changes tracked atomically; sessions resume cleanly after interruption |
| **Decision log** | Every session writes a structured log; past decisions are searchable by any future agent |
| **Agent personas** | Seven role definitions (Planner, Developer, Reviewer, Tester, Builder, Documenter, Orchestrator) with explicit protocols |

**The result:** ~1,400 tokens of overhead per session instead of 15,000+ tokens of re-discovery.

---

## How It Works

## Agent Session Lifecycle

```mermaid
flowchart TB

Start([Start Session])

subgraph Orientation
A[Check Open Changesets]
B[Get Project Roadmap]
C[Search Past Decisions]
D[Load Graph Context\nget_node • get_impact]
end

subgraph Execution
E[Plan Task]
F[Implement Code]
G[Run Tests / Validation]
end

subgraph Completion
H[Update Graph Metadata]
I[Write Session Log]
J[Complete Changeset]
end

Start --> A
A --> B
B --> C
C --> D
D --> E
E --> F
F --> G
G --> H
H --> I
I --> J
```

---


### Code Intelligence Model

```mermaid
flowchart TB

A[Source Code]

subgraph Structural Analysis
B[AST Parser]
C[Function / Class Extraction]
D[Dependency Analysis]
end

subgraph Knowledge Stores
E[(Semantic Index<br/>ChromaDB)]
F[(Context Graph<br/>YAML Nodes)]
end

subgraph Runtime Access
G[MCP Query Layer<br/>search_codebase • get_node • get_impact]
end

H[AI Coding Agent<br/>Claude Code • Cursor]

A --> B
B --> C
C --> E

B --> D
D --> F

E --> G
F --> G

G --> H
```


## Quick Start

### 1. Add to your project

```bash
git clone https://github.com/sachinshelke/codevira .agents
pip install -r .agents/requirements.txt
```

### 2. Configure

```bash
cp .agents/config.example.yaml .agents/config.yaml
```

Edit `.agents/config.yaml` for your project:

```yaml
project:
  name: my-project
  watched_dirs: ["src"]          # directories to index
  language: python               # python | typescript | go | rust
  file_extensions: [".py"]
  collection_name: my_codebase
```

### 3. Build the index

```bash
# Builds code index + graph stubs + roadmap in one command
python .agents/indexer/index_codebase.py --full --generate-graph --bootstrap-roadmap

# Auto-reindex on every git commit
bash .agents/hooks/install-hooks.sh
```

### 4. Connect to your AI tool

**Claude Code** — add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "Codevira": {
      "command": "python",
      "args": [".agents/mcp-server/server.py"]
    }
  }
}
```

**Cursor** — Settings → MCP → Add Server:
- Command: `python`
- Args: `.agents/mcp-server/server.py`
- Working directory: project root

**Windsurf / Google Antigravity** — same as Cursor via the MCP settings panel.

### 5. Verify

Ask your agent to call `get_roadmap()` — it should return your current phase and next action.

> **No roadmap yet?** No problem. `get_roadmap()` auto-creates a Phase 1 stub on first call. No setup required.

---

## Session Protocol

Every agent session follows `.agents/PROTOCOL.md`. Read it once — then your agents handle the rest.

**Session start (mandatory):**
```
list_open_changesets()      → resume any unfinished work first
get_roadmap()               → current phase, next action
search_decisions("topic")   → check what's already been decided
get_node("src/service.py")  → read rules before touching a file
get_impact("src/service.py") → check blast radius
```

**Session end (mandatory):**
```
complete_changeset(id, decisions=[...])
update_node(file_path, changes)
update_next_action("what the next agent should do")
write_session_log(...)
```

This loop keeps every session fast, focused, and resumable.

---

## 26 MCP Tools

### Graph Tools
| Tool | Description |
|---|---|
| `get_node(file_path)` | Metadata, rules, connections, staleness for any file |
| `get_impact(file_path)` | BFS blast-radius — which files depend on this one |
| `list_nodes(layer?, stability?, do_not_revert?)` | Query nodes by attribute |
| `add_node(file_path, role, type, ...)` | Register a new file in the graph |
| `update_node(file_path, changes)` | Append rules, connections, key_functions |
| `refresh_graph(file_paths?)` | Auto-generate stubs for unregistered files |
| `refresh_index(file_paths?)` | Re-embed specific files in ChromaDB |

### Roadmap Tools
| Tool | Description |
|---|---|
| `get_roadmap()` | Current phase, next action, open changesets |
| `get_full_roadmap()` | Complete history: all phases, decisions, deferred |
| `get_phase(number)` | Full details of any phase by number |
| `update_next_action(text)` | Set what the next agent should do |
| `update_phase_status(status)` | Mark phase in_progress / blocked |
| `add_phase(phase, name, description, ...)` | Queue new upcoming work |
| `complete_phase(number, key_decisions)` | Mark done, auto-advance to next |
| `defer_phase(number, reason)` | Move a phase to the deferred list |

### Changeset Tools
| Tool | Description |
|---|---|
| `list_open_changesets()` | All in-progress changesets |
| `get_changeset(id)` | Full detail: files done, files pending, blocker |
| `start_changeset(id, description, files)` | Open a multi-file changeset |
| `complete_changeset(id, decisions)` | Close and record decisions |
| `update_changeset_progress(id, last_file, blocker?)` | Mid-session checkpoint |

### Search Tools
| Tool | Description |
|---|---|
| `search_codebase(description, top_k?)` | Semantic search over source code |
| `search_decisions(query, top_k?)` | Search all past session decisions |
| `get_history(file_path)` | All sessions that touched a file |
| `write_session_log(...)` | Write structured session record |

### Code Reader Tools _(Python only)_
| Tool | Description |
|---|---|
| `get_signature(file_path)` | All public symbols, signatures, line numbers |
| `get_code(file_path, symbol)` | Full source of one function or class |

### Playbook Tool
| Tool | Description |
|---|---|
| `get_playbook(task_type)` | Curated rules for a task: `add_route`, `add_service`, `add_schema`, `debug_pipeline`, `commit`, `write_test` |

---

## Agent Personas

Seven role definitions in `agents/` tell each agent exactly what to do and when:

| Agent | Invoked When | Key Responsibility |
|---|---|---|
| `orchestrator.md` | Every session start | Classify task, select pipeline |
| `planner.md` | Large or ambiguous tasks | Decompose into ordered steps |
| `developer.md` | All code changes | Write code within graph rules |
| `reviewer.md` | `stability: high` or `do_not_revert` files | Flag rule violations |
| `tester.md` | After every code change | Run the test suite |
| `builder.md` | After tests pass | Lint, type-check |
| `documenter.md` | End of every session | Update graph, roadmap, log |

---

## Project Structure

```
.agents/
├── PROTOCOL.md              # Session protocol — read this first
├── config.example.yaml      # Config template
├── config.yaml              # Your config (git-ignored)
├── roadmap.yaml             # Phase tracker (auto-created, git-ignored)
├── mcp-server/
│   ├── server.py            # MCP server entry point
│   └── tools/
│       ├── graph.py
│       ├── roadmap.py
│       ├── changesets.py
│       ├── search.py
│       ├── playbook.py
│       └── code_reader.py
├── indexer/
│   ├── index_codebase.py    # Build/update ChromaDB index
│   ├── chunker.py           # AST-based code chunker
│   └── graph_generator.py   # Auto-generate graph stubs
├── requirements.txt         # Python dependencies
├── agents/                  # Role definitions
│   ├── orchestrator.md
│   ├── planner.md
│   ├── developer.md
│   ├── reviewer.md
│   ├── tester.md
│   ├── builder.md
│   └── documenter.md
├── rules/                   # Engineering standards
│   ├── master_rule.md
│   ├── coding-standards.md
│   ├── testing-standards.md
│   └── ...13 more
├── graph/
│   ├── _schema.yaml         # Node/edge schema reference
│   └── changesets/
├── hooks/
│   └── install-hooks.sh
├── logs/                    # Session logs (git-ignored)
└── codeindex/               # ChromaDB files (git-ignored)
```

---

## Language Support

| Feature | Python | TypeScript | Go | Rust |
|---|---|---|---|---|
| Semantic code search | ✅ | ✅ | ✅ | ✅ |
| Context graph + blast radius | ✅ | ✅ | ✅ | ✅ |
| Roadmap + changesets | ✅ | ✅ | ✅ | ✅ |
| Session logs + decision search | ✅ | ✅ | ✅ | ✅ |
| `get_signature` / `get_code` | ✅ | ❌ | ❌ | ❌ |
| Auto-generated graph stubs | ✅ | ❌ | ❌ | ❌ |
| AST-based chunking | ✅ | ⚠️ regex | ⚠️ regex | ⚠️ regex |

All session management, graph, roadmap, and search features work for any language.
Only `get_signature`, `get_code`, and auto-generated graph stubs are Python-specific.

---

## Requirements

- Python 3.10+
- ChromaDB
- sentence-transformers
- PyYAML

```bash
pip install -r .agents/requirements.txt
```

---

## Background

Want to understand the full story behind why this was built, the design decisions, what didn't work, and how it compares to other tools in the ecosystem?

Read the full write-up: [How We Cut AI Coding Agent Token Usage by 92%](docs/how-i-built-persistent-memory-for-ai-agents.md)

---

## Contributing

Contributions are welcome — this is an early-stage open source project and there's a lot of room to grow.

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide: forking, branch naming, commit format, and PR process.

**Good first areas:**
- Tree-sitter support for TypeScript / Go / Rust (unlocks `get_signature` and graph auto-generation)
- Additional playbook entries for common task types
- IDE-specific setup guides
- Bug reports and edge case fixes

**Reporting a bug?** → [Open a bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md)

**Requesting a feature?** → [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md)

**Found a security issue?** → Read [SECURITY.md](SECURITY.md) — please don't use public issues for vulnerabilities.

Please open an issue before submitting a large PR so we can discuss the approach first.

---

## FAQ

Common questions about setup, usage, architecture, and troubleshooting — see [FAQ.md](FAQ.md).

---

## Roadmap

See what's built, what's coming next, and what's being considered — see [ROADMAP.md](ROADMAP.md).

Want to influence priorities? [Open a feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md) or upvote existing ones.

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to maintain a respectful and welcoming environment.

---

## License

MIT — free to use, modify, and distribute.
