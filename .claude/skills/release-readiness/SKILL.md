---
name: release-readiness
description: |
  Use this skill whenever the conversation mentions releasing, shipping,
  publishing, promoting to PyPI, "ship it", "release X.Y.Z", "ready to
  release", "let's publish", or any phrase suggesting moving codevira
  to production. Walks the 5-gate release gauntlet (G1-G5) and refuses
  to proceed without evidence at every gate. Never produces release
  commands without confirming the gauntlet passed.
---

# Release readiness — the bypass-proof release gauntlet

v2.0.0 shipped to PyPI with 23 silent-failure bugs because nothing
forced gauntlet-pass evidence before publish. This skill makes the
gauntlet mandatory at the CONVERSATION layer (skills); the Makefile
+ hook + CI enforce it at the TOOL layer. Both must agree before any
release proceeds.

## When this skill triggers

Trigger phrases:

- "ready to release"
- "let's ship"
- "publish to PyPI"
- "promote to production"
- "release X.Y.Z"
- "cut a release"
- "tag the version"
- "ship it"
- Any mention of `twine upload`, `gh release create --draft=false`,
  `pipx publish`, etc.

When this skill triggers, you MUST walk the 5-gate gauntlet below
before producing any actual release command.

## The 5 gates

Defined in `codevira.discipline.yaml::release_gates`. Each must pass.

### G1 — Unit tests

```bash
make test-unit
# OR: pytest tests/ -q --ignore=tests/e2e
```

Pass criterion: exit code 0, no test failures or unexpected skips.

### G2 — First-contact e2e

```bash
make test-e2e
# OR: pytest tests/e2e/test_first_contact.py -v
```

Pass criterion: all 4 fixtures pass (docs_only, code_only_python,
polyglot, monorepo). This is the gate that catches bugs A–O. If G2
shows failures, those failures ARE the v2.1 work list — don't
release until they're fixed.

### G3 — Real-IDE smoke

```bash
scripts/check_real_ide_smoke.sh
```

Pass criterion: codevira appears connected in Claude Code AND Claude
Desktop AND at least one other IDE. `tools/list` returns in <1s.

**Today's state:** the script is real and G3 has recorded `true` since
3.1.0. It was a stub before that, recording `"skipped"` — and this file
used to say that was "tolerable for v2.0.x but BLOCKING for v2.1".
It never became blocking. Six releases shipped with G3 unverified,
because a sentence in a skill is not an enforcement point.

It is enforced now: `pre-release-block.sh` refuses any gate that is not
`true`, with one stated exception (G4 may be `"warn"`). `"skipped"`
blocks.

### G4 — Crash-log clean

```bash
CRASH_LOG="${CODEVIRA_HOME:-$HOME/.codevira}/logs/crashes.log"
test ! -e "$CRASH_LOG" || test "$(grep -c '^CRASH:' "$CRASH_LOG")" = "0"
```

Read the log FILE, not a CLI surface. This gate used to run
`codevira report | grep -c CRASH`, and `report` is not a codevira
subcommand: it exited 2 with a usage error, `2>/dev/null` swallowed it,
grep counted an empty stream, and the answer was always 0. G4 could not
fail. It reported "no crashes" for 4.1.0 with three CRASH entries in the
log, and every `G4_crash_log_clean` in every past evidence file was
meaningless. A gate that depends on a CLI surface can be retired by a
rename without anyone noticing.

Pass criterion: zero CRASH entries in `~/.codevira/logs/crashes.log`
after the e2e gauntlet has run. Catches the Chroma HNSW writer
corruption pattern that UDAP hit with 41 crashes from one session.

### G5 — Human-in-the-loop

**No automation can satisfy this gate.** The maintainer:

1. Runs `pipx install codevira==<version>` on a real machine (NOT
   the dev machine — fresh state).
2. Tests against THEIR real projects (NOT the test fixtures).
3. Confirms cross-tool memory works (Claude Code → Cursor → recall).
4. Edits `.release-evidence/<version>.json` and sets:
   ```
   "G5_human_confirmed": true
   ```

The PreToolUse hook physically refuses `twine upload` until this
field is `true`.

## The mandatory walkthrough

When this skill triggers, output in this order:

```
▸ Release-readiness gauntlet for v<X.Y.Z>

CONTEXT:
  - Version in pyproject.toml: <X.Y.Z>
  - Current branch: <name>
  - Last commit: <sha> <subject>

PURPOSE:
  - User wants to: <one sentence>
  - This means: <concrete outcome>
  - This does NOT mean: <adjacent thing>

GATES STATUS:
  G1 unit tests:      [ ] Run `make test-unit` and report exit code.
  G2 first-contact:   [ ] Run `make test-e2e` and report PASS/FAIL per fixture.
  G3 real-IDE smoke:  [ ] Run scripts/check_real_ide_smoke.sh. "skipped" BLOCKS.
  G4 crash-log clean: [ ] grep -c '^CRASH:' in ~/.codevira/logs/crashes.log (expect 0).
  G5 human confirmed: [ ] Maintainer must run manually and edit evidence file.

EVIDENCE FILE:
  Path: .release-evidence/<X.Y.Z>.json
  Status: [ ] exists / [ ] does not exist

UNTIL ALL 5 GATES ARE GREEN, DO NOT propose `twine upload` or
`gh release ... --draft=false`. The PreToolUse hook will reject
those commands anyway, but the discipline is: don't even ATTEMPT
the release command without evidence.
```

## Foolproof release sequence

After confirming the gauntlet structure with the user, the actual
sequence is:

```bash
# 1. Pre-flight: version coherence + git state.
make release-verify-version
# Checks: clean working tree, on main/release branch, in sync with
# origin, version matches across pyproject.toml + __init__.py + CHANGELOG.

# 2. Run G1–G4 (G5 is human-only).
make release-gauntlet
# Generates .release-evidence/<version>.json with G1, G2, G3, G4
# results. G5_human_confirmed is set to false.

# 3. Build distribution artifacts.
make release-build
# Cleans dist/, runs `python -m build`, produces wheel + sdist.

# 4. Validate the artifacts BEFORE upload.
make release-dry-run
# Runs `twine check dist/*` — verifies PyPI-ready metadata.

# 5. STOP. Human verification (G5).
# Install on a real machine, test against real projects, then edit
# .release-evidence/<version>.json and set G5_human_confirmed=true.

# 6. Publish (hook will verify all 5 gates before allowing upload).
make release-publish
# OR: twine upload dist/*

# 7. Post-release: smoke test from PyPI itself.
make release-smoke
# Fresh venv, pip install codevira==<version> from PyPI, verify
# --version reports the right value.
```

## Anti-patterns this skill refuses

- "Tests pass, ready to ship" without showing G2 fixture results.
- "I ran the gauntlet" without referring to the evidence file.
- Proposing `twine upload` without evidence file existing.
- Setting `G5_human_confirmed=true` yourself. You're not the human.
- Skipping G3/G4 because "they're optional." They are not optional and
  never were. `"skipped"` in evidence means the gate could not run, and
  the publish hook now refuses it. The single tolerated non-`true`
  verdict is G4 = `"warn"`.
- Reporting a gate as passed from the exit status of a command you did
  not watch run. G4 passed for years on a subcommand that did not exist.

## Rollback plan

If the release is published and a critical bug surfaces:

1. **Yank the PyPI release** (don't delete — yank marks it as not
   for new installs, but doesn't break existing pins):
   ```bash
   twine yank codevira==<bad-version>
   ```
2. Cut a hotfix branch from the tag, fix, re-run the full gauntlet
   for `<bad-version>+1`, release the patch.
3. Update CHANGELOG.md `### Removed` section noting the yank and
   the replacement version.

Document the rollback in the evidence file:
`.release-evidence/<bad-version>.rollback.md`.

## Why this exists

The trust loss: *"i'm loosing confidence on you that everything
whenever we are releasing on production you always missed many thing
even after asking multiple round of testing."*

The structural fix is: gates with evidence, not gates with promises.
Skills are the conversational layer of that fix. Hooks + Makefile +
CI are the hard wall. They reinforce each other; without skills the
hard wall is silent, without the hard wall the skills are bypassable.
