# Codevira Release Process — Foolproof Walkthrough

This is the canonical guide for releasing codevira to PyPI. It's
designed to be bypass-proof: every gate has tooling that enforces it.
Skipping a step requires explicit override flags that are logged for
audit.

**Why this exists.** v2.0.0 shipped to PyPI with 23 silent-failure
bugs because we trusted "all unit tests pass" as a release signal.
This process replaces trust with evidence.

---

## The 5 gates

| Gate | What it checks | Pass criterion | Enforced by |
|---|---|---|---|
| **G1** | Unit tests pass | `make test-unit` exit 0 | `make release-gauntlet` |
| **G2** | First-contact e2e (4 fixtures) | `make test-e2e` exit 0 | `make release-gauntlet` + GitHub Actions |
| **G3** | Real-IDE smoke | `scripts/check_real_ide_smoke.sh` exit 0 | (stub — must be filled for v2.1 launch) |
| **G4** | Crash-log clean | `codevira report \| grep -c CRASH` is 0 | `make release-gauntlet` |
| **G5** | Human verification | Maintainer set `G5_human_confirmed: true` | `.claude/hooks/pre-release-block.sh` |

All 5 must be true in `.release-evidence/<version>.json` for the
PreToolUse hook to allow `twine upload`.

---

## Pre-release checklist (do these in order)

### 0. Pre-flight

Before starting a release:

- [ ] All bugs targeted for this release are merged to main.
- [ ] CHANGELOG.md `[Unreleased]` section reflects what's actually
      changing.
- [ ] You've decided the version number (semver: patch / minor / major).

### 1. Bump version

Edit ONE source of truth:

```bash
# Bump in pyproject.toml
python3 -c "
import re
src = open('pyproject.toml').read()
src = re.sub(r'version\s*=\s*\"[^\"]+\"', 'version = \"NEW_VERSION_HERE\"', src, count=1)
open('pyproject.toml', 'w').write(src)
"
```

If `mcp_server/__init__.py` declares `__version__`, bump it too
(release-verify-version will catch drift).

### 2. Promote `[Unreleased]` → `[X.Y.Z]` in CHANGELOG

Edit `CHANGELOG.md`:

```markdown
## [Unreleased]

## [X.Y.Z] — YYYY-MM-DD

### Added
- ...
```

### 3. Commit the version bump

```bash
git add pyproject.toml mcp_server/__init__.py CHANGELOG.md
git commit -m "release: bump to X.Y.Z

Promote [Unreleased] changelog entries to [X.Y.Z]. See CHANGELOG.md
for the full list of changes since the previous tag."
git push origin main
```

### 4. Run the verification gates

```bash
make release-verify-version
```

This checks:
- Working tree is clean.
- Branch is `main` (or `release/*`).
- Local is in sync with origin.
- Version matches across `pyproject.toml` + `mcp_server/__init__.py`.
- CHANGELOG.md has `## [X.Y.Z]` entry.
- If tag `vX.Y.Z` exists, it points at HEAD.

Fix any failure before proceeding.

### 5. Run the gauntlet (G1–G4)

```bash
make release-gauntlet
```

This runs G1 (unit), G2 (e2e), G3 (stub → "skipped"), G4 (crash log)
and writes `.release-evidence/X.Y.Z.json`. If any gate fails, the
gauntlet exits non-zero and no evidence file is finalized.

Inspect the result:

```bash
make release-evidence
# Or directly:
cat .release-evidence/X.Y.Z.json
```

Expected structure:

```json
{
  "version": "X.Y.Z",
  "timestamp": "2026-05-16T20:00:00Z",
  "G1_unit_tests": true,
  "G2_first_contact": true,
  "G3_real_ide_smoke": "skipped",
  "G4_crash_log_clean": "skipped",
  "G5_human_confirmed": false,
  "note": "G5 must be set true by hand after maintainer verification on a real machine."
}
```

### 6. Build distribution artifacts

```bash
make release-build
```

This:
- Cleans `dist/`, `build/`, `*.egg-info/`.
- Runs `python -m build` to produce wheel + sdist.
- Lists the built artifacts.

### 7. Validate the build (dry-run)

```bash
make release-dry-run
```

This runs `twine check dist/*` to verify the metadata is PyPI-valid
(README renders, version is valid, no missing fields). Also confirms
the wheel filename contains `X.Y.Z` (catches mid-build version drift).

### 8. Manual verification (G5)

**This step CANNOT be automated.** You must do it on a real machine
that is NOT your dev machine. Recommended steps:

```bash
# On a clean test machine OR a fresh user account
pipx install codevira==X.Y.Z --pip-args="--pre"
# (Or after release: pipx install codevira==X.Y.Z)

# Verify the binary works
codevira --version
codevira doctor

# Cd into a real project and run the first-contact flow
cd ~/some-real-project
codevira init
codevira index
codevira status

# Verify the cross-tool memory works:
# - Open Claude Code, ask it to make a decision
# - Quit Claude Code completely
# - Open Cursor in the same project
# - Ask Cursor to recall the decision
# - The decision should surface (proving cross-tool memory works)
```

If all of the above behaves as expected, edit the evidence file:

```bash
# .release-evidence/X.Y.Z.json
python3 -c "
import json
p = '.release-evidence/X.Y.Z.json'
d = json.load(open(p))
d['G5_human_confirmed'] = True
d['G5_verified_by'] = 'YOUR-NAME'
d['G5_verified_at'] = 'YYYY-MM-DDTHH:MM:SSZ'
d['G5_notes'] = 'Tested on <machine>, fresh pipx install, against <project name>.'
json.dump(d, open(p, 'w'), indent=2)
"
```

### 9. Publish to PyPI

```bash
make release-publish
# OR equivalently:
twine upload dist/*
```

The PreToolUse hook will allow this command **only if**
`.release-evidence/X.Y.Z.json` shows all 5 gates pass. If anything
is missing or `G5_human_confirmed=false`, the hook blocks with a
detailed error message.

### 10. Tag and push

After successful upload:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z

See CHANGELOG.md for changes since the previous tag."
git push origin vX.Y.Z
```

### 11. Create GitHub release

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-from-tag \
  --draft  # opens as draft first
# Review on GitHub UI, then:
gh release edit vX.Y.Z --draft=false
```

The hook also blocks `gh release ... --draft=false` without evidence,
so this works the same way.

### 12. Post-release smoke

After ~30 seconds for PyPI to propagate:

```bash
make release-smoke
```

This installs `codevira==X.Y.Z` in a temporary venv from PyPI and
verifies `--version` reports the right value. Catches the worst
failure mode: a release that uploaded but is broken on actual install.

---

## What to do if something goes wrong

### Gauntlet failed

The gauntlet generates evidence ONLY if G1–G4 pass. If you see
`✗ G2 FAILED` and no evidence file, that's correct behavior — the
release is blocked at the source. Fix the failing tests, commit,
re-run.

### Hook is blocking but you're sure it's wrong

```bash
CODEVIRA_RELEASE_OVERRIDE=1 twine upload dist/*
```

The override is logged to `.release-evidence/overrides.log`. Use ONLY
when:
- A test is genuinely a false positive (rare).
- You're shipping a security hotfix and the gauntlet timing is wrong.
- You've manually walked through G1–G5 outside the make targets.

Document the override in the rollback notes.

### A bad release made it to PyPI

PyPI doesn't allow re-uploading a version (immutable). Two options:

1. **Yank the bad version** (preferred — keeps existing pins working):
   ```bash
   # PyPI web UI → manage your release → yank
   # Or via twine if your account has the permission:
   twine yank codevira X.Y.Z --message "Critical bug — install X.Y.Z+1 instead"
   ```
2. **Cut an immediate patch** (X.Y.Z+1) following this whole process
   again with the bug fixed.

Document the failure in `.release-evidence/X.Y.Z.rollback.md` with:
- What broke (specific bug + affected users).
- When the yank happened.
- What X.Y.Z+1 fixes.
- Lessons learned (added to a future gauntlet step if needed).

---

## What's intentionally NOT automated

| Gate | Why human-required |
|---|---|
| **G5** | A computer cannot verify "the cross-tool memory feels right in real use." That requires a human running real workflows on real projects. |
| **CHANGELOG promotion** | Deciding what's user-visible vs internal is editorial. |
| **Version number** | Semver requires judgment (is this a breaking change?). |
| **PyPI yank decision** | Whether to yank, communicate, or hotfix depends on user impact. |

---

## Append-only audit trail

Every release leaves:

- `.release-evidence/<version>.json` — gates evidence.
- `.release-evidence/audit.log` — every hook ALLOW (gate-passing
  release commands that ran).
- `.release-evidence/overrides.log` — every hook bypass.
- Git tag — points at the exact released commit.
- PyPI upload timestamp — set by PyPI when twine succeeded.

These are gitignored locally (per-machine state) but the audit log
should be reviewed periodically — if you see overrides without a
documented reason, that's a process leak to plug.

---

## Reference: the Makefile targets

```
make help                       # show all targets
make dev                        # install [dev] deps + pre-commit hooks
make ci                         # mirrors GitHub Actions CI
make release-verify-version     # gate 0: version coherence + git state
make release-gauntlet           # gates G1-G4
make release-build              # build dist/
make release-dry-run            # twine check dist/*
make release-publish            # twine upload (hook-gated)
make release-smoke              # post-publish: pip install from PyPI
make release-full               # verify-version → gauntlet → build → dry-run
```

For more on the discipline scaffold philosophy, see
[CONTRIBUTING.md § Development Discipline](../CONTRIBUTING.md#development-discipline-pillar-3).
