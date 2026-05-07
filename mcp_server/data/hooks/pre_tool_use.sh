#!/usr/bin/env bash
# codevira PreToolUse hook — invoked by Claude Code before any AI tool call.
#
# Reads JSON from stdin, runs the codevira engine, writes the
# Claude-Code-compatible response on stdout.
#
# Exit codes (per Claude Code hook protocol):
#   0 → allow / continue
#   2 → blocked (Claude Code shows the message and prevents the tool)
#
# This script is INSTALLED by `codevira hooks install` to ~/.claude/hooks/
# (with namespace prefix `codevira-`) and registered in ~/.claude/settings.json.
#
# Performance: must complete in <50ms p95. Engine handles fast-rejection
# of disabled policies and short-circuits when CODEVIRA_ENGINE=0.
#
# Bug 18 hardening: when codevira is missing or stale (no `engine`
# subcommand), we MUST NOT propagate the binary's nonzero exit code to
# Claude Code — that surfaces as "operation blocked by hook" and stops
# the user. Strategy: capture codevira's stdout. If it looks like JSON
# (starts with `{`), forward it AND propagate the engine's exit code
# (so legitimate exit-2 blocks still work). If stdout is empty / not
# JSON (argparse error, missing binary, crash), emit a no-op JSON and
# exit 0 — the user is never blocked by a tooling problem.

if [[ "${CODEVIRA_ENGINE:-1}" == "0" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

CODEVIRA=""
if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
fi

if [[ -z "$CODEVIRA" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

# Capture engine response. Stdin (the Claude Code event JSON) flows
# through the subshell to codevira normally.
RESPONSE="$("${CODEVIRA}" engine handle PreToolUse 2>/dev/null)"
RC=$?

# Validate: response must be non-empty and look like JSON.
if [[ -z "$RESPONSE" || "${RESPONSE:0:1}" != "{" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

# Live engine response → forward verbatim, preserve exit code so
# legitimate exit-2 blocks still propagate.
printf '%s\n' "$RESPONSE"
exit "$RC"
