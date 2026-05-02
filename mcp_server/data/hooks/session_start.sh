#!/usr/bin/env bash
# codevira SessionStart hook — invoked by Claude Code at the beginning
# of every new AI session.
#
# Heroes that listen here:
#   - Hero 5 (Cross-Session Consistency): inject get_session_context()
#   - Hero 9 (Proactive Intent Inference): pre-fetch likely-needed context
#
# The hook returns `additionalContext` which Claude Code includes in the
# AI's first turn — making memory automatic without the user prompting.

if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
else
  printf '{"continue": true}\n'
  exit 0
fi

exec "${CODEVIRA}" engine handle SessionStart
