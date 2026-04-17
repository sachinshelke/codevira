# Contributing to Codevira

Thank you for your interest in contributing! This is an early-stage open source project and every contribution — bug fixes, new features, documentation, or ideas — is genuinely appreciated.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Contributing with an AI Agent](#contributing-with-an-ai-agent)
- [How to Report a Bug](#how-to-report-a-bug)
- [How to Request a Feature](#how-to-request-a-feature)
- [How to Contribute Code](#how-to-contribute-code)
- [Development Setup](#development-setup)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Commit Message Format](#commit-message-format)
- [Good First Issues](#good-first-issues)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to uphold a respectful and welcoming environment for everyone.

---

## Getting Started

1. **Fork** the repository on GitHub
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/codevira.git
   cd codevira
   ```
3. **Install** in development mode:
   ```bash
   pip install -e .
   ```
4. **Create a branch** for your change:
   ```bash
   git checkout -b feat/your-feature-name
   ```
5. Make your changes, then **open a Pull Request**.

---

## Contributing with an AI Agent

**AI-assisted contributions are fully welcome — in fact, this project is built for exactly that workflow.**

Codevira exists to make AI-assisted coding better. Using it while contributing to this repo is the best way to contribute.

### Set up Codevira on your fork

```bash
# After cloning your fork
cd codevira
pip install -e .
codevira init
```

This auto-detects the project, builds the index, and configures your AI tool. Your agent now has full context of the codebase.

### Let the agent orient before coding

At the start of the session, your agent should call:

```
get_roadmap()                         -> understand the project state
get_node("mcp_server/tools/graph.py") -> read the rules for the file it will change
get_impact("mcp_server/tools/graph.py") -> know what else might be affected
search_decisions("topic")             -> check what's already been decided
```

### Keep the PR scope clean

AI agents can change many files quickly. For a PR to be reviewable:

- **Start a changeset** before the agent begins: `start_changeset(id, description, files)`
- **One concern per PR** — if the agent discovers additional improvements, open a separate issue
- **Review every file** the agent changed before submitting

### Include session decisions in your PR

When your agent calls `write_session_log()`, it captures key decisions. Include them in your PR description:

```
## What
Added tree-sitter chunker for TypeScript files.

## Agent session decisions
- Used tree-sitter over regex because it handles template literals and decorators correctly
- Kept Python AST chunker as the default; TypeScript chunker activates via config
- Fallback to line-based splitting when tree-sitter parse fails
```

---

## How to Report a Bug

1. Check [existing issues](https://github.com/sachinshelke/codevira/issues) first.
2. Open a [new bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md).
3. Include: OS, Python version, AI tool, and full error message.

---

## How to Request a Feature

1. Check [existing issues](https://github.com/sachinshelke/codevira/issues) to avoid duplicates.
2. Open a [feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md).
3. Describe the **problem** you're trying to solve, not just the solution.

For large features, please open an issue for discussion **before** writing code.

---

## How to Contribute Code

### 1. Fork and clone

```bash
git clone https://github.com/YOUR-USERNAME/codevira.git
cd codevira
```

### 2. Create a branch

```bash
git checkout -b feat/typescript-chunker
git checkout -b fix/graph-stale-detection
git checkout -b docs/cursor-setup-guide
```

Branch prefixes:
| Prefix | Use for |
|---|---|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation only |
| `refactor/` | Code restructuring, no behavior change |
| `test/` | Adding or fixing tests |

### 3. Make your changes

- Keep changes focused — one feature or fix per PR
- Follow the existing code style
- If you add a new MCP tool, register it in `mcp_server/server.py`
- If you change tool behavior, update the relevant section in `README.md`

### 4. Test your changes

```bash
# Run the test suite
python -m pytest tests/ -v

# Verify the MCP server starts and all tools register
python -m mcp_server

# If you modified the indexer, test on the project itself
codevira index --full
codevira status
```

### 5. Commit and push

```bash
git add .
git commit -m "feat(indexer): add tree-sitter support for TypeScript"
git push origin feat/typescript-chunker
```

### 6. Open a Pull Request

Go to GitHub and open a PR from your branch. Fill in the template.

---

## Development Setup

```bash
# Clone
git clone https://github.com/sachinshelke/codevira.git
cd codevira

# Install in development mode (editable, with all optional deps)
pip install -e .

# Verify MCP server starts
python -m mcp_server

# Run tests
python -m pytest tests/ -v

# Initialize Codevira on this project (for AI-assisted development)
codevira init
```

**Recommended Python version:** 3.10 or higher.

### Project layout

```
codevira/
├── mcp_server/            # MCP server package
│   ├── server.py          # Server entry point + tool registration
│   ├── cli.py             # CLI: init, index, status
│   ├── detect.py          # Zero-config language/dir auto-detection
│   ├── ide_inject.py      # Auto-inject MCP config into IDEs
│   ├── paths.py           # Centralized path resolution
│   ├── global_sync.py     # Cross-project memory sync
│   ├── prompts.py         # MCP workflow prompts
│   ├── tools/             # MCP tool implementations
│   │   ├── graph.py
│   │   ├── roadmap.py
│   │   ├── search.py
│   │   ├── learning.py
│   │   ├── code_reader.py
│   │   └── playbook.py
│   └── data/              # Bundled assets (agents, rules, config template)
├── indexer/               # Indexing and analysis
│   ├── index_codebase.py  # Build/update index + background watcher
│   ├── chunker.py         # AST-based code chunker
│   ├── treesitter_parser.py
│   ├── sqlite_graph.py    # SQLite graph database
│   ├── graph_generator.py # Auto-generate graph stubs + symbols
│   ├── global_db.py       # Cross-project global database
│   ├── outcome_tracker.py # Git-based feedback loop
│   └── rule_learner.py    # Automatic rule inference
├── tests/                 # Test suite
├── pyproject.toml         # Package config
└── README.md
```

---

## Pull Request Guidelines

- **One PR per concern** — don't bundle unrelated changes
- **Keep PRs small** — easier to review, faster to merge
- **Write a clear description** — what changed, why, and how to test it
- **Link related issues** — use `Closes #123`
- **Be responsive** — respond to review comments within a few days

PRs that pass review will be squash-merged to keep the main branch history clean.

---

## Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>
```

**Types:**
| Type | When to use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation change |
| `refactor` | Code restructuring |
| `test` | Adding or updating tests |
| `chore` | Tooling, config, dependencies |

**Examples:**
```
feat(indexer): add tree-sitter chunker for TypeScript
fix(roadmap): auto-create stub when roadmap is missing
docs(readme): update quick start for v1.5 zero-config
refactor(graph): migrate from YAML to SQLite
```

---

## Good First Issues

New to the project? Look for issues tagged [`good first issue`](https://github.com/sachinshelke/codevira/issues?q=label%3A%22good+first+issue%22).

Areas welcoming to new contributors:

- **Documentation** — examples, tutorials, setup guides for specific tools
- **Playbook entries** — add new task types to `mcp_server/tools/playbook.py`
- **Bug fixes** — any open bug report
- **Test coverage** — add tests for edge cases

---

## Questions?

Open a [Discussion](https://github.com/sachinshelke/codevira/discussions) or comment on a relevant issue.
