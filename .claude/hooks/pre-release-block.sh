#!/bin/bash
#
# pre-release-block.sh — PreToolUse hook (hard wall against unverified releases)
#
# Why this exists: v2.0.0 shipped to PyPI with 23 silent-failure bugs
# (A–O) because no mechanism existed to refuse a release that hadn't
# passed the gauntlet. Skills are conversational guidance; this hook
# is a hard wall — Claude Code physically cannot execute the blocked
# command without the evidence file in place.
#
# Wired into Claude Code via .claude/settings.json:
#
#   {
#     "hooks": {
#       "PreToolUse": [
#         {
#           "matcher": "Bash",
#           "hooks": [{"type": "command", "command": ".claude/hooks/pre-release-block.sh"}]
#         }
#       ]
#     }
#   }
#
# Hook contract:
#   - Reads JSON from stdin describing the tool call
#   - Exit 0 → allow
#   - Exit 2 → block, with stderr message shown to Claude
#
# Failure-mode policy:
#   * For UNRELATED Bash commands (ls, mkdir, etc.) → ALWAYS allow.
#     The hook must never block innocent commands due to its own
#     internal errors. If we can't parse stdin, we let the command
#     through (innocent until proven release).
#   * For CONFIRMED release commands (twine upload, etc.) → demand
#     evidence. If python3 is missing or evidence is malformed, we
#     fail closed and block the release.
#
# Rationale: blocking `ls` because the hook can't parse JSON is a
# productivity disaster. Blocking `twine upload` because we can't
# verify the gauntlet is the entire point.

set -uo pipefail   # NOTE: no `e` — we handle errors explicitly so a
                   # python3 stderr or grep fail doesn't kill the hook.

# ─── Read tool-call JSON from stdin ────────────────────────────────────────
INPUT=$(cat)

# ─── Extract tool name + command (best effort; allow on parse error) ──────
# We use python3 to parse JSON. If python3 is missing OR the JSON is
# malformed, we treat this as "not a release command" and exit 0.
# Defense in depth: even if parsing fails, the secondary string match
# on `twine upload` etc. below catches release commands.

if command -v python3 >/dev/null 2>&1; then
  TOOL_NAME=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_name", ""))' 2>/dev/null || echo "")
  COMMAND=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input", {}).get("command", ""))' 2>/dev/null || echo "")
else
  # No python3 → can't parse. Fall back: treat the whole stdin as the
  # candidate command for substring matching. This is paranoid but
  # ensures the hook still catches `twine upload` even without python3.
  TOOL_NAME="Bash"
  COMMAND="$INPUT"
fi

# Not a Bash tool call → pass through.
if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

# ─── Detect release-relevant commands ──────────────────────────────────────
#
# KEEP IN SYNC with codevira.discipline.yaml `block_commands:`.
# The shell hook hardcodes the list for defense-in-depth (works even
# if YAML is missing/corrupt) and performance (fires on every bash
# call, so no YAML parsing per-call).

is_release_command() {
  local cmd="$1"
  case "$cmd" in
    *"twine upload"*)                       return 0 ;;
    *"python -m twine upload"*)             return 0 ;;
    *"python3 -m twine upload"*)            return 0 ;;
    *"gh release edit"*"--draft=false"*)    return 0 ;;
    *"gh release create"*"--draft=false"*)  return 0 ;;
    *"pipx publish"*)                       return 0 ;;
    *"pip publish"*)                        return 0 ;;
    *"make release-publish"*)               return 0 ;;
    *) return 1 ;;
  esac
}

if ! is_release_command "$COMMAND"; then
  exit 0
fi

# ─── It IS a release command. From here on, fail closed. ───────────────────

# Explicit override (logged for audit).
if [ "${CODEVIRA_RELEASE_OVERRIDE:-}" = "1" ]; then
  REPO_ROOT_FOR_LOG="${CLAUDE_PROJECT_DIR:-$(pwd)}"
  mkdir -p "$REPO_ROOT_FOR_LOG/.release-evidence"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) OVERRIDE: $COMMAND" \
    >> "$REPO_ROOT_FOR_LOG/.release-evidence/overrides.log"
  exit 0
fi

# Now that we know we're blocking a release, python3 is non-negotiable.
if ! command -v python3 >/dev/null 2>&1; then
  echo "RELEASE BLOCKED: python3 not on PATH — cannot verify gauntlet evidence." >&2
  echo "  Install python3 or use CODEVIRA_RELEASE_OVERRIDE=1 (logged)." >&2
  exit 2
fi

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
EVIDENCE_DIR="$REPO_ROOT/.release-evidence"

# Read version from pyproject.toml via env-var-passed path (no string
# interpolation into python source — eliminates path injection risk).
VERSION=$(REPO_ROOT="$REPO_ROOT" python3 -c '
import os, re
try:
    src = open(os.path.join(os.environ["REPO_ROOT"], "pyproject.toml")).read()
    m = re.search(r"version\s*=\s*\"([^\"]+)\"", src)
    print(m.group(1) if m else "")
except Exception:
    print("")
' 2>/dev/null || echo "")

if [ -z "$VERSION" ]; then
  echo "RELEASE BLOCKED: cannot determine version from pyproject.toml" >&2
  echo "  Run from the repo root, or fix the version field in pyproject.toml." >&2
  exit 2
fi

EVIDENCE_FILE="$EVIDENCE_DIR/$VERSION.json"

if [ ! -f "$EVIDENCE_FILE" ]; then
  cat >&2 <<EOF
RELEASE BLOCKED for v$VERSION

Command attempted: $COMMAND

No release-evidence file found at:
  $EVIDENCE_FILE

This means the release gauntlet (G1–G5) has not been run for this
version. Why this matters: v2.0.0 shipped to PyPI with 23 silent-
failure bugs because nothing forced gauntlet-pass evidence before
publish. This hook prevents that recurrence.

To proceed:

  1. Run the gauntlet:
       make release-gauntlet

  2. Run G5 (human verification) on a real machine:
       - install on a fresh fixture project
       - verify behavior matches expectations
       - edit $EVIDENCE_FILE and set:
           "G5_human_confirmed": true

  3. Re-run the original command. The hook will allow it once the
     evidence shows G1–G5 all pass.

If you BELIEVE this block is wrong (e.g. you're building a non-release
artifact and twine matched accidentally), bypass with explicit override:
  CODEVIRA_RELEASE_OVERRIDE=1 <command>
The override is logged to .release-evidence/overrides.log for review.
EOF
  exit 2
fi

# Evidence file exists. Verify all gates pass — env-var-passed path.
ALL_PASS=$(EVIDENCE_FILE="$EVIDENCE_FILE" python3 -c '
import json, os, sys
try:
    d = json.load(open(os.environ["EVIDENCE_FILE"]))
    g1 = d.get("G1_unit_tests") is True
    g2 = d.get("G2_first_contact") is True
    g3 = d.get("G3_real_ide_smoke") in (True, "skipped")
    # G4 accepts True, "skipped", or "warn" — crash log is historical
    # state, not a release blocker for a release that may FIX the crashes.
    # Warn surfaces to the user via Makefile output; release is allowed.
    g4 = d.get("G4_crash_log_clean") in (True, "skipped", "warn")
    g5 = d.get("G5_human_confirmed") is True
    print("all_pass" if (g1 and g2 and g3 and g4 and g5) else "missing")
except Exception:
    print("parse_error")
' 2>/dev/null || echo "parse_error")

case "$ALL_PASS" in
  all_pass)
    mkdir -p "$EVIDENCE_DIR"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALLOW v$VERSION: $COMMAND" >> "$EVIDENCE_DIR/audit.log"
    exit 0
    ;;
  *)
    cat >&2 <<EOF
RELEASE BLOCKED for v$VERSION

Command attempted: $COMMAND

Evidence file exists but not all gates pass:
  $EVIDENCE_FILE

Specifically, G5 (human-in-the-loop confirmation) requires the
maintainer to verify on a real machine and explicitly set:
  "G5_human_confirmed": true

Open $EVIDENCE_FILE and set that field to true after verification.
EOF
    exit 2
    ;;
esac
