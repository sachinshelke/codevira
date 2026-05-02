#!/usr/bin/env bash
# codevira Stop hook — invoked by Claude Code at session end.
#
# Heroes that listen here:
#   - Hero 6 (Token Budget Live View): persist final session token summary
#   - Hero 10 (AI Promotion Score): record session outcomes for learning
#
# Best-effort; the AI session is already over so blocking is meaningless.
# This hook is for cleanup + persistence.

if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
else
  printf '{"continue": true}\n'
  exit 0
fi

exec "${CODEVIRA}" engine handle Stop
