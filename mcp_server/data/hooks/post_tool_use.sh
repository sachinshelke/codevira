#!/usr/bin/env bash
# codevira PostToolUse hook — invoked by Claude Code after any AI tool call.
#
# Used for telemetry (token meter, decision logging, style checks). Most
# verdicts are `allow`; this hook rarely blocks but may inject context
# for the next AI turn.

if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
else
  printf '{"continue": true}\n'
  exit 0
fi

exec "${CODEVIRA}" engine handle PostToolUse
