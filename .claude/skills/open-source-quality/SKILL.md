---
name: open-source-quality
description: |
  Use this skill before committing, pushing, or opening a PR. Triggers
  on phrases like "commit", "git commit", "push", "open PR", "create
  pull request", "finalize this change". Enforces conventional commit
  messages, atomic commits, code style (ruff/mypy), public-API
  docstrings, actionable error messages, changelog discipline, and
  backwards-compat warnings. Refuses to declare commits ready without
  passing these checks.
---

# Open-source quality — non-skippable checks at commit time

The codevira repo is open-source. Every commit ends up in `git log`
that future contributors read to learn the codebase. Bad commits =
unreadable history = onboarding pain for everyone who comes after.

When this skill triggers, walk through the checklist below BEFORE
calling the Bash tool with `git commit`. Skip a step → commit is
not ready.

## Commit message discipline

### Conventional Commits format

Every commit message follows: `type: short imperative summary`

Types:
- `feat:` — new feature (user-visible)
- `fix:` — bug fix (user-visible)
- `docs:` — documentation only
- `refactor:` — code restructure, no behavior change
- `test:` — adding or fixing tests
- `chore:` — build, tooling, deps
- `release:` — version bump for publishing
- `perf:` — performance improvement
- `style:` — formatting / whitespace

Subject line:
- Imperative mood: "add foo" not "added foo"
- < 70 characters
- No trailing period
- Lowercase after the type

### Atomic commits

One logical change per commit. Examples:

- ✗ "fix bug and refactor tests and update docs" → 3 commits
- ✓ "fix(indexer): single matcher between configure and index"
- ✓ "test(indexer): regression test for split-config bug"
- ✓ "docs: explain shared file matcher in indexer README"

If your diff spans 3 unrelated concerns, split into 3 commits before
pushing. Use `git add -p` (interactive staging) or commit-by-file.

### Body explains WHY

Subject is WHAT. Body is WHY.

```
fix(indexer): single matcher between configure and index

Previously configure used discover_source_files() but index used a
separate hand-rolled matcher in cmd_incremental. They diverged: docs-
only repos passed configure but failed indexing silently.

The split was a latent bug since v1.6 when discover_source_files
landed. Unifying eliminates the entire class of "discovery vs
indexing" mismatches surfaced as Bug A in the 2026-05-15 audit.

Co-Authored-By: <name>
```

## Code style

Before commit, ALL of these must pass:

```bash
make lint          # ruff check mcp_server indexer
make format        # ruff format (no diff after)
make type-check    # mypy mcp_server indexer
```

If any fails, fix before commit. The `.pre-commit-config.yaml`
hook auto-runs these — bypassing via `--no-verify` is a discipline
breach. Don't.

## Public API surface

Every new/changed PUBLIC function or class (no `_` prefix) must have:

### Full docstring

```python
def my_function(arg1: str, arg2: int = 5) -> dict[str, Any]:
    """One-line summary of what this does.

    Longer explanation if needed. Why this exists, when to use it.

    Args:
        arg1: What this parameter is for.
        arg2: What this parameter is for. Defaults to 5.

    Returns:
        A dict with keys: 'foo' (str), 'bar' (int).

    Raises:
        ValueError: if arg1 is empty.

    Example:
        >>> my_function("hello", 3)
        {'foo': 'hello', 'bar': 3}
    """
```

### Type annotations on signature

Required for every public function. `Any` is acceptable only with
a comment explaining why a more specific type isn't possible.

### Public MCP tools

If you add a tool to `mcp_server/tools/*.py`:

- The tool's docstring is what AI agents see in `tools/list`. Make
  it useful: explain what it returns, when to use it vs alternatives,
  and what `full=true` adds (if applicable).
- Tool must follow the codevira convention: summary by default,
  `full=true` for complete data, `fix_command` on errors.
- Register the tool in `mcp_server/server.py` AND in
  `mcp_server/tool_visibility.py` if it should appear in the
  AI-facing list (the 23-tool default surface).

## Error messages — actionable

Every new error message must answer 3 questions:

```
WHAT failed:  "Cannot read config at /path/to/file"
WHY:          "File does not exist"
FIX:          "Run `codevira init` to create it, or check the path."
```

Combined: `"Cannot read config at /path/to/file: file does not exist. Run `codevira init` to create it."`

The `fix_command` pattern in codevira's MCP tools is exactly this.

## Changelog discipline

Every user-visible change adds an entry to `CHANGELOG.md` under
`## [Unreleased]`:

```markdown
## [Unreleased]

### Fixed
- (#123) Configure and index now share a single file matcher.
  Previously docs-only projects produced 0 chunks silently.

### Added
- ...

### Changed
- ...

### Removed
- ...

### Deprecated
- ...
```

When a release is cut, `[Unreleased]` is promoted to `[X.Y.Z] —
YYYY-MM-DD`.

## Backwards compatibility

Any change to MCP tool signature, CLI flag, or config schema is a
**potential breaking change**. Discipline:

1. Don't remove. Deprecate first.
2. Add a deprecation warning that fires for one full minor version.
3. Document the deprecation in CHANGELOG.md under `### Deprecated`.
4. Remove in the NEXT minor version (or major bump).

Example: removing `--project-dir` flag in favor of `--project`.
- v2.1: `--project` is the new spelling; `--project-dir` still works
  but prints a deprecation warning.
- v2.2: `--project-dir` is removed.

## License + dependency hygiene

New dependency in `pyproject.toml`?

1. Justify it in the commit message body. Why is this dep needed?
   Could existing code do it?
2. Verify the license is MIT-compatible (or another approved
   permissive license). Run: `pip-licenses --packages <new-dep>`.
3. Pin a sensible version range (e.g. `>=1.0,<2.0`).
4. Note it in CHANGELOG.md `### Changed` so users see the new
   transitive deps coming with their next upgrade.

## PR-ready checklist

Before declaring a PR ready for review (or opening one), confirm:

- [ ] Commit message follows Conventional Commits format
- [ ] One logical change per commit
- [ ] `make ci` passes locally (lint + test-unit + test-e2e)
- [ ] New public APIs have full docstrings + type hints
- [ ] New error messages answer what / why / fix
- [ ] CHANGELOG.md `[Unreleased]` has an entry
- [ ] No new deps without justification
- [ ] No `--no-verify` bypasses in commits

## Why this exists

Codevira is open-source. The biggest contributors don't exist yet —
they'll arrive in 6 months and read `git log` to figure out how to
fix bugs. If every commit looks like `"updates"` with no body,
they'll bounce. If every commit follows the discipline above, they'll
land a PR in 2 hours instead of 2 days.

This skill enforces the discipline so the open-source story doesn't
depend on the founder remembering to do it manually every time.
