# Rule 012: Git & CI/CD Governance

## 1. Branching Strategy

- **Trunk-Based Development with Feature Branches**: `main` is always production-ready and protected.
- **Naming Convention**: `feat/`, `fix/`, `refactor/`, `docs/`, followed by a concise description (e.g., `feat/core-event-bus`).
- **No Direct Commits**: All changes must go through a Pull Request/Merge Request flow.

## 2. Commit Standards (Conventional Commits)

- **Format**: `<type>(<scope>): <subject>` — Example: `feat(core): implement circuit breaker`.
- **Accountability**: Every commit should reference the changeset or task it addresses (see session logs in `.agents/logs/`).
- **Atomic Commits**: Each commit should represent a single logical change.

## 3. CI/CD Gates (The Quality Bar)

- **Verification First**: Code CANNOT be merged unless:
    - `pytest` passes with 100% success.
    - `scripts/verify_architecture.py` returns 0 violations.
    - `ruff` (or equivalent linter) returns 0 errors.
- **Evidence of Work**: Every PR/significant change must include a `walkthrough.md` demonstrating the changes and test results.

## 4. Versioning (SemVer)

- **Strict SemVer**: Follow `MAJOR.MINOR.PATCH` rules.
- **Breaking Changes**: MAJOR bumps require architectural review and explicit amendment to `PLATFORM_ARCHITECTURE.md`.

## 5. Automated Release Process

- **Tagging**: Releases are triggered by git tags (e.g., `v0.8.0`).
- **Changelog**: `CHANGELOG.md` must be updated BEFORE the version tag is created.
- **Immutable Artifacts**: Production docker images must be tagged with the version and commit SHA, never just `latest`.