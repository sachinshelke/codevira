# Contributing to Codevira

Thank you for your interest in contributing! This is an early-stage open source project and every contribution — bug fixes, new features, documentation, or ideas — is genuinely appreciated.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Contributing with an AI Agent](#contributing-with-an-ai-agent)
- [Development Discipline (Pillar 3)](#development-discipline-pillar-3)
- [How to Report a Bug](#how-to-report-a-bug)
- [How to Request a Feature](#how-to-request-a-feature)
- [How to Contribute Code](#how-to-contribute-code)
- [Development Setup](#development-setup)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Commit Message Format](#commit-message-format)
- [Release Process](#release-process)
- [Good First Issues](#good-first-issues)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to uphold a respectful and welcoming environment for everyone.

---

## Development Discipline (Pillar 3)

Codevira ships its own AI development discipline scaffold. If you (or
an AI agent helping you) make changes to this repo, this scaffold
enforces quality at three layers:

### 1. Skills (conversational guidance — when using Claude Code)

Auto-discovered from `.claude/skills/`:

- **`development-discipline`** — triggers on any Edit/Write. Forces a
  `CONTEXT → PURPOSE → REASON → CODE` sequence before any code change.
  Sub-rules: reuse-first, single-source-of-truth, blast-radius-aware,
  minimal-diff, test-as-evidence.
- **`open-source-quality`** — triggers on commit/push/PR. Enforces
  Conventional Commits, atomic commits, ruff+mypy clean, docstrings,
  actionable error messages, CHANGELOG.md entries, backwards-compat.
- **`release-readiness`** — triggers on release-related phrases.
  Walks the G1–G5 gauntlet.
- **`epistemic-honesty`** — triggers on definitive claims, solution
  proposals, or bug diagnoses. Forces confidence calibration with
  inline evidence; refuses unverified "done" claims.

### 2. Pre-commit hooks (run automatically on every `git commit`)

Configured in `.pre-commit-config.yaml`:

- `ruff check --fix` — lint
- `ruff format` — format
- `mypy mcp_server indexer` — type-check
- Hygiene: trailing whitespace, large files, merge conflicts, private keys

Install: `make dev` (or `pip install -e ".[dev]" && pre-commit install`).

### 3. Release gauntlet (`make release-gauntlet`)

Five gates that must pass before a release reaches PyPI:

| Gate | What | How |
|---|---|---|
| **G1** | Unit tests | `make test-unit` |
| **G2** | First-contact e2e | `make test-e2e` against 4 fixtures |
| **G3** | Real-IDE smoke | `scripts/check_real_ide_smoke.sh` (stub today) |
| **G4** | Crash-log clean | `codevira report` shows 0 CRASH |
| **G5** | Human verification | Maintainer confirms on a real machine |

The PreToolUse hook (`.claude/hooks/pre-release-block.sh`) refuses
`twine upload` / `pipx publish` / `gh release create --draft=false`
unless `.release-evidence/<version>.json` shows all 5 gates pass.

See [`docs/release-process.md`](docs/release-process.md) for the full
foolproof release walkthrough.

### Why this exists

v2.0.0 shipped to PyPI with 23 silent-failure bugs (cataloged in
ROADMAP.md as A–O) because "all unit tests pass" was used as a release
signal. It wasn't enough. The discipline scaffold makes the gates
bypass-proof: skills are conversational; hooks + Makefile + CI are
hard walls that physically refuse unverified releases.

If you're working on this repo with Claude Code, the skills will
trigger automatically. If you're working without an AI agent, the
Makefile + hooks + CI still enforce the same discipline at the tool
layer.

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

## Release Process

Codevira uses a 5-gate release gauntlet (G1–G5). The full walkthrough
lives in [`docs/release-process.md`](docs/release-process.md). Short
version:

```bash
make release-verify-version    # version coherence + git state
make release-gauntlet          # G1 (unit) + G2 (e2e) + G3/G4 stubs
make release-build             # python -m build → dist/
make release-dry-run           # twine check dist/*
# ─── Manual G5: install on a real machine, verify, then ──────────────
#     edit .release-evidence/<version>.json and set:
#       "G5_human_confirmed": true
make release-publish           # twine upload (hook will verify gates)
make release-smoke             # post-release: install from PyPI, verify
```

The PreToolUse hook (`.claude/hooks/pre-release-block.sh`) refuses
`twine upload` / `pipx publish` / `gh release ... --draft=false`
without all 5 gates green in the evidence file. Bypass requires
explicit `CODEVIRA_RELEASE_OVERRIDE=1` and is logged to
`.release-evidence/overrides.log` for audit.

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
