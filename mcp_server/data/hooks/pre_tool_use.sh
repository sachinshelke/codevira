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

# Round-3 QA fast path: if the engine is explicitly disabled, skip the
# full Python invocation. Saves ~100ms (the cost of process spawn +
# Python interpreter startup + module imports). Without this, every
# Claude Code tool call pays Python-startup tax even when the user has
# turned codevira off via CODEVIRA_ENGINE=0.
if [[ "${CODEVIRA_ENGINE:-1}" == "0" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

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
