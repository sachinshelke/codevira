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
   git clone https://github.com/sachinshelke/codevira.git
   cd Codevira
   ```
3. **Install** dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. **Create a branch** for your change:
   ```bash
   git checkout -b feat/your-feature-name
   ```
5. Make your changes, then **open a Pull Request**.

---

## Contributing with an AI Agent

**AI-assisted contributions are fully welcome here — in fact, this project is built for exactly that workflow.**

Codevira exists to make AI-assisted coding better. It gives your agent persistent memory, blast-radius awareness, and a structured session log. Using it while contributing to this repo is not just allowed — it's the best way to contribute.

Here's how a great AI-assisted contribution looks:

### Set up Codevira on your fork

```bash
# After cloning your fork, build the index so your agent has full context
cp config.example.yaml config.yaml
python indexer/index_codebase.py --full --generate-graph --bootstrap-roadmap
```

Connect your AI tool to `.agents/mcp-server/server.py` as described in the README.
Now your agent knows every file's role, rules, and dependencies before touching anything.

### Let the agent orient before coding

At the start of the session, your agent should call:

```
get_roadmap()                        → understand the project state
get_node("mcp-server/tools/graph.py") → read the rules for the file it will change
get_impact("mcp-server/tools/graph.py") → know what else might be affected
search_decisions("topic")            → check what's already been decided
```

This takes seconds and prevents the agent from making decisions that contradict existing design choices.

### Keep the PR scope clean

AI agents are powerful — they can change many files quickly. For a PR to be reviewable, keep it focused:

- **Start a changeset** before the agent begins: `start_changeset(id, description, files)` — this defines the scope upfront
- **One concern per PR** — if the agent discovers additional improvements while working, open a separate issue instead of expanding the PR
- **Review every file** the agent changed before submitting — you are responsible for the PR, not the agent

### Include the session decisions in your PR

When your agent calls `write_session_log()` at the end of the session, it captures the key decisions made. Paste the `decisions` list into your PR description. This tells reviewers *why* the code is the way it is — not just *what* changed.

Example PR description with AI session context:
```
## What
Added tree-sitter chunker for TypeScript files.

## Agent session decisions
- Used tree-sitter over regex because it handles template literals and decorators correctly
- Kept Python AST chunker as the default; TypeScript chunker activates via config language: typescript
- Fallback to line-based splitting when tree-sitter parse fails (graceful degradation)
```

### Why this works better

When you use Codevira while contributing to Codevira, you get:
- Your agent never re-reads files it already knows about
- Blast radius is checked before every change — no accidental side effects
- Decisions are captured automatically — PR descriptions write themselves
- Session logs become part of the project's institutional memory

This is the workflow Codevira is designed to enable. Contributing this way is the best demonstration of what the project does.

---

## How to Report a Bug

1. Check [existing issues](https://github.com/sachinshelke/codevira/issues) first — it may already be reported.
2. Open a [new bug report](https://github.com/sachinshelke/codevira/issues/new?template=bug_report.md).
3. Fill in the template — the more detail, the faster we can fix it.

**Good bug reports include:**
- What you did (exact steps to reproduce)
- What you expected to happen
- What actually happened (error message, stack trace)
- Your environment (OS, Python version, AI tool used)

---

## How to Request a Feature

1. Check [existing issues](https://github.com/sachinshelke/codevira/issues) to avoid duplicates.
2. Open a [feature request](https://github.com/sachinshelke/codevira/issues/new?template=feature_request.md).
3. Describe the problem you're trying to solve, not just the solution — this helps us find the best approach together.

For large features, please open an issue for discussion **before** writing code. This avoids wasted effort if the approach doesn't fit the project direction.

---

## How to Contribute Code

### 1. Fork and clone

```bash
git clone https://github.com/sachinshelke/codevira.git
cd Codevira
```

### 2. Create a branch

Use a descriptive branch name:

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
- If you add a new MCP tool, register it in `mcp-server/server.py`
- If you change tool behavior, update the relevant section in `README.md`

### 4. Test your changes

```bash
# Verify the MCP server starts and all tools register correctly
python mcp-server/server.py

# If you modified the indexer, test it on a small project
python indexer/index_codebase.py --full
python indexer/index_codebase.py --status
```

### 5. Commit and push

```bash
git add .
git commit -m "feat(indexer): add tree-sitter support for TypeScript"
git push origin feat/typescript-chunker
```

### 6. Open a Pull Request

Go to GitHub and open a PR from your branch. Fill in the PR template — describe what changed and why.

---

## Development Setup

```bash
# Clone
git clone https://github.com/sachinshelke/codevira.git
cd Codevira

# Install dependencies
pip install -r requirements.txt

# Optional: install in a virtual environment
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
.venv\Scripts\activate       # Windows
pip install -r requirements.txt

# Verify MCP server starts
python mcp-server/server.py
```

**Recommended Python version:** 3.10 or higher.

---

## Pull Request Guidelines

- **One PR per concern** — don't bundle unrelated changes
- **Keep PRs small** — easier to review, faster to merge
- **Write a clear description** — what changed, why, and how to test it
- **Link related issues** — use `Closes #123` in the PR description to auto-close issues
- **Be responsive** — if a reviewer asks for changes, try to respond within a few days

PRs that pass review will be squash-merged to keep the main branch history clean.

---

## Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

<optional longer description>
```

**Types:**
| Type | When to use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation change |
| `refactor` | Code change that doesn't fix a bug or add a feature |
| `test` | Adding or updating tests |
| `chore` | Tooling, config, dependencies |

**Examples:**
```
feat(indexer): add tree-sitter chunker for TypeScript
fix(roadmap): auto-create stub when roadmap.yaml is missing
docs(readme): add Windsurf setup instructions
refactor(graph): simplify _infer_graph_file logic
```

---

## Good First Issues

New to the project? Look for issues tagged [`good first issue`](https://github.com/sachinshelke/codevira/issues?q=label%3A%22good+first+issue%22).

Areas particularly welcoming to new contributors:

- **Language support** — tree-sitter chunker for TypeScript, Go, or Rust
- **Playbook entries** — add new task types to `mcp-server/tools/playbook.py`
- **IDE setup guides** — detailed setup for specific AI tools
- **Documentation** — examples, tutorials, clarifications
- **Bug fixes** — any open bug report

---

## Questions?

Open a [Discussion](https://github.com/sachinshelke/codevira/discussions) or comment on a relevant issue.
We're happy to help you get started.
