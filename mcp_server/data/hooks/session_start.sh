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
#
# Bug 18 hardening: capture codevira stdout; fall back to no-op when not
# valid JSON (stale binary, missing, crash). Never block session start.

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

RESPONSE="$("${CODEVIRA}" engine handle SessionStart 2>/dev/null)"
RC=$?

if [[ -z "$RESPONSE" || "${RESPONSE:0:1}" != "{" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

printf '%s\n' "$RESPONSE"
exit "$RC"
