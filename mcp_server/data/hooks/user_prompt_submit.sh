#!/usr/bin/env bash
# codevira UserPromptSubmit hook — invoked by Claude Code when the user
# submits a prompt to the AI.
#
# Heroes that listen here:
#   - Hero 3 (Scope Contract Lock): parses intent, sets scope contract
#   - Hero 5 (Cross-Session Consistency): active recall of related decisions
#   - Hero 9 (Proactive Intent Inference): pre-fetches likely context

if [[ -x "${HOME}/.local/bin/codevira" ]]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
elif command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
else
  printf '{"continue": true}\n'
  exit 0
fi

exec "${CODEVIRA}" engine handle UserPromptSubmit
