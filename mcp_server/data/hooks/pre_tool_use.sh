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

# Resolve the codevira binary. We prefer the pipx install location, then
# fall back to whatever is on PATH. If we can't find it, allow silently —
# the user shouldn't be blocked by a misconfigured hook.
if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
else
  printf '{"continue": true}\n'
  exit 0
fi

exec "${CODEVIRA}" engine handle PreToolUse
