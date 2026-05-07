#!/usr/bin/env bash
# codevira UserPromptSubmit hook — invoked by Claude Code when the user
# submits a prompt to the AI.
#
# Heroes that listen here:
#   - Hero 3 (Scope Contract Lock): parses intent, sets scope contract
#   - Hero 5 (Cross-Session Consistency): active recall of related decisions
#   - Hero 9 (Proactive Intent Inference): pre-fetches likely context
#
# Bug 18 hardening: capture codevira's stdout; if it's not valid-looking
# JSON (binary missing, stale codevira without `engine` subcommand, crash),
# emit `{"continue": true}` and exit 0 so the user is never blocked. If
# stdout IS valid JSON, forward it verbatim and preserve the engine's
# exit code (so legitimate blocks still work).

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

RESPONSE="$("${CODEVIRA}" engine handle UserPromptSubmit 2>/dev/null)"
RC=$?

if [[ -z "$RESPONSE" || "${RESPONSE:0:1}" != "{" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

printf '%s\n' "$RESPONSE"
exit "$RC"
