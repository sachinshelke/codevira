#!/usr/bin/env bash
# codevira PostToolUse hook — invoked by Claude Code after any AI tool call.
#
# Used for telemetry (token meter, decision logging, style checks). Most
# verdicts are `allow`; this hook rarely blocks but may inject context
# for the next AI turn.
#
# Bug 18 hardening: capture codevira stdout; fall back to no-op when not
# valid JSON. Never block.

if [[ "${CODEVIRA_ENGINE:-1}" == "0" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

# v3.0: file-based engine disable. Same effect as CODEVIRA_ENGINE=0
# but persists across shells. Toggle with `codevira engine disable` /
# `codevira engine enable`.
if [[ -f "${HOME}/.codevira/engine.disabled" ]]; then
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

RESPONSE="$("${CODEVIRA}" engine handle PostToolUse 2>/dev/null)"
RC=$?

if [[ -z "$RESPONSE" || "${RESPONSE:0:1}" != "{" ]]; then
  printf '{"continue": true}\n'
  exit 0
fi

printf '%s\n' "$RESPONSE"
exit "$RC"
