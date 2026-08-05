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
#
# False positives are their own failure mode. This hook's block message
# advertises CODEVIRA_RELEASE_OVERRIDE=1 as the escape hatch, so every
# innocent command it refuses trains the maintainer to reach for the
# override. A guard that cries wolf stops being a wall. Hence: match on
# the command being EXECUTED, never on quoted prose that merely names a
# release (see the detector below).
#
# Scope note: this is a guard against an *accidental* release, not a
# sandbox. It does not defend against someone deliberately obfuscating a
# publish command, and it isn't trying to.

set -uo pipefail   # NOTE: no `e` — we handle errors explicitly so a
                   # python3 stderr or grep fail doesn't kill the hook.

# ─── Detect release-relevant commands ──────────────────────────────────────
#
# KEEP IN SYNC with codevira.discipline.yaml `block_commands:`.
# The shell hook hardcodes the list for defense-in-depth (works even
# if YAML is missing/corrupt) and performance (fires on every bash
# call, so no YAML parsing per-call).
# tests/test_pre_release_block_hook.py asserts the two lists agree.
#
# Matching is token-based, not substring-based:
#
#   1. Heredoc bodies are dropped, so `git commit -m "$(cat <<'EOF' …)"`
#      is judged on `git commit -m …`, not on the commit message.
#   2. Comments are dropped, quote-aware and the way bash does it — a
#      `#` only starts one where a word starts, so `a#b` stays a word.
#      (shlex's own comment handling would eat the rest of the line.)
#   3. What's left is split into shell tokens, so a quoted string is ONE
#      token — `"…blocked twine upload…"` can never look like the two
#      adjacent tokens `twine` `upload`.
#   4. Release patterns are matched as adjacent token runs within a
#      single command segment (split on ; && || |), and we recurse into
#      `sh -c "…"` so a nested shell can't launder a real publish.
#
# Unparseable input (unbalanced quotes, no python3) falls back to the old
# substring match: paranoid, occasionally wrong, but never lets a release
# through on a technicality.
#
# The detector is held in a variable rather than a sibling file so the
# hook stays a single self-contained script. It is read via `read -d ''`
# and NOT `$(cat <<'PYEOF' …)`: bash 3.2 (what macOS ships) tracks quotes
# while scanning for the closing paren of a command substitution, so a
# heredoc body containing an odd number of `'` — which the regex below
# does — is a parse error there. A plain heredoc is taken verbatim.
# `read` hits EOF without its NUL delimiter and returns 1; that is the
# normal path, hence `|| true`.
IFS= read -r -d '' _PY_DETECT <<'PYEOF' || true
import json
import os
import re
import shlex
import sys

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}

# Openers we recognise: <<EOF, <<-EOF, <<'EOF', <<"EOF". Deliberately not
# <<< (here-string): that has no body to strip.
_HEREDOC = re.compile(
    r"""<<-?[ \t]*(?:'([^']*)'|"([^"]*)"|([A-Za-z_][A-Za-z0-9_-]*))"""
)

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}"}


def strip_heredocs(cmd):
    """Drop heredoc bodies, keeping the lines that carry real commands.

    An opener with no matching terminator is left alone rather than
    swallowing the rest of the command — over-stripping would hide a
    genuine release, which is the failure we can least afford.
    """
    lines = cmd.split("\n")
    kept = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in _HEREDOC.finditer(line):
            delim = match.group(1) or match.group(2) or match.group(3)
            if not delim:
                continue
            j = i
            while j < len(lines) and lines[j].strip() != delim:
                j += 1
            if j < len(lines):
                i = j + 1  # skip body + terminator
    return "\n".join(kept)


def strip_comments(cmd):
    """Drop shell comments, quote-aware.

    We cannot leave this to shlex: it ends a token at ANY unquoted `#`
    and discards the rest of the LINE, so `echo a#b && twine upload`
    lexes to ['echo', 'a'] and the release vanishes. bash only starts a
    comment where a word starts, so `a#b` is a literal word. We follow
    bash and set `commenters = ""` on the lexer.

    Runs AFTER strip_heredocs so apostrophes in heredoc prose ("doesn't")
    can't leave this scanner stuck in a bogus quote state.
    """
    out = []
    quote = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            out.append(ch)
            # In double quotes a backslash escapes the next character;
            # in single quotes nothing does.
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
        elif ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(cmd[i + 1])
            i += 2
        elif ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
        elif ch == "#" and (not out or out[-1] in " \t\n;&|()"):
            # A comment. Skip to the newline but keep it, so line
            # structure (and any heredoc terminator) survives.
            nl = cmd.find("\n", i)
            if nl == -1:
                break
            i = nl
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def tokenize(cmd):
    """Shell-ish tokens. Raises ValueError on unbalanced quotes."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # handled by strip_comments, which matches bash
    return list(lexer)


def segments(tokens):
    """Split a token list into individual commands on shell separators."""
    current = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def has_run(tokens, run):
    n = len(run)
    return any(tuple(tokens[i:i + n]) == run for i in range(len(tokens) - n + 1))


def segment_is_release(tokens):
    # `twine upload` also covers `python -m twine upload`,
    # `uv run twine upload`, `poetry run twine upload`, …
    if has_run(tokens, ("twine", "upload")):
        return True
    if has_run(tokens, ("pipx", "publish")):
        return True
    if has_run(tokens, ("pip", "publish")):
        return True
    if (
        has_run(tokens, ("gh", "release", "edit"))
        or has_run(tokens, ("gh", "release", "create"))
    ) and "--draft=false" in tokens:
        return True
    if "make" in tokens and "release-publish" in tokens:
        return True
    return False


def substring_fallback(cmd):
    """Last resort when the command can't be tokenized."""
    pairs = [
        ("twine upload", None),
        ("pipx publish", None),
        ("pip publish", None),
        ("make release-publish", None),
        ("gh release edit", "--draft=false"),
        ("gh release create", "--draft=false"),
    ]
    return any(a in cmd and (b is None or b in cmd) for a, b in pairs)


def is_release(cmd, depth=0):
    if depth > 3:
        return False
    try:
        tokens = tokenize(strip_comments(strip_heredocs(cmd)))
    except ValueError:
        return substring_fallback(cmd)
    for seg in segments(tokens):
        if segment_is_release(seg):
            return True
        # `bash -c "twine upload dist/*"` — the payload is a quoted token,
        # so judge it as a command in its own right.
        if os.path.basename(seg[0]) in _SHELLS:
            for arg in seg[1:]:
                if not arg.startswith("-") and is_release(arg, depth + 1):
                    return True
    return False


try:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "") or ""
    if payload.get("tool_name", "") != "Bash":
        verdict = "ALLOW"
        command = ""
    else:
        verdict = "RELEASE" if is_release(command) else "ALLOW"
except Exception:
    # Malformed JSON / unreadable stdin. Say so; bash falls back to
    # matching the raw payload rather than assuming innocence.
    verdict, command = "ERROR", ""

sys.stdout.write(verdict + "\n" + command)
PYEOF

# Bash-level fallback, used only when python3 is unavailable or the
# detector itself failed. Substring-based — the very thing that produced
# false positives — but a missing python3 is rare and a missed release is
# worse than a spurious block on that path.
is_release_command_fallback() {
  local cmd="$1"
  case "$cmd" in
    *"twine upload"*)                       return 0 ;;
    *"gh release edit"*"--draft=false"*)    return 0 ;;
    *"gh release create"*"--draft=false"*)  return 0 ;;
    *"pipx publish"*)                       return 0 ;;
    *"pip publish"*)                        return 0 ;;
    *"make release-publish"*)               return 0 ;;
    *) return 1 ;;
  esac
}

# ─── Read tool-call JSON from stdin ────────────────────────────────────────
INPUT=$(cat)

# One python3 process per Bash call: it parses the JSON *and* decides.
# Output is "<verdict>\n<command>", so the raw command survives for the
# error messages and the audit log.
if command -v python3 >/dev/null 2>&1; then
  DETECT=$(printf '%s' "$INPUT" | python3 -c "$_PY_DETECT" 2>/dev/null || printf 'ERROR\n')
else
  DETECT="ERROR"
fi

VERDICT="${DETECT%%$'\n'*}"
case "$DETECT" in
  *$'\n'*) COMMAND="${DETECT#*$'\n'}" ;;
  *)       COMMAND="" ;;
esac

case "$VERDICT" in
  ALLOW)
    exit 0
    ;;
  RELEASE)
    : # fall through to the evidence check
    ;;
  *)
    # Couldn't parse or couldn't decide. Match the raw payload — it
    # contains the command text even when the JSON is malformed.
    if ! is_release_command_fallback "$INPUT"; then
      exit 0
    fi
    COMMAND="$INPUT"
    ;;
esac

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
