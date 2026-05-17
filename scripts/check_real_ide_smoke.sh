#!/bin/bash
#
# check_real_ide_smoke.sh — G3 of the release gauntlet.
#
# Verifies codevira appears connected in real running IDEs (Claude Code,
# Claude Desktop, Cursor, Windsurf, Antigravity). Currently a STUB.
# Filling this in is a v2.1 reliability item — until then it exits 1
# so the gauntlet records G3 as "skipped" rather than "passed."
#
# What this script SHOULD do (when fully implemented):
#
#  1. For each detected IDE config (~/.claude.json, ~/Library/Application
#     Support/Claude/claude_desktop_config.json, ~/.cursor/mcp.json, etc.):
#     - Verify codevira is registered.
#     - Verify the registered command path actually exists.
#
#  2. Spawn an MCP stdio server (`codevira --project-dir <fixture>`):
#     - Send an `initialize` request.
#     - Send `tools/list` and measure time-to-response.
#     - Assert response time < 1 second.
#     - Assert tools count > 20.
#     - Clean shutdown.
#
#  3. If a launchd daemon is configured (v2.2 multi-project HTTPS):
#     - Verify the daemon is reachable at https://localhost:8443/mcp.
#     - Send the same initialize + tools/list and assert <50ms.
#
# Exit codes:
#   0 — all IDE smoke tests pass.
#   1 — at least one IDE smoke test failed.
#   2 — stub state (current behavior).
#
# When this script is filled in, the gauntlet's G3 step will produce
# a real true/false in the evidence file instead of "skipped."

set -uo pipefail

echo "G3 stub: scripts/check_real_ide_smoke.sh is not yet implemented."
echo ""
echo "What this needs to test (v2.1 backlog):"
echo "  - codevira registered in each detected IDE config"
echo "  - MCP stdio handshake completes in <1s (Claude Desktop timeout)"
echo "  - tools/list returns >20 tools"
echo "  - No HNSW segment writer corruption on the project's Chroma store"
echo "  - codevira binary on PATH resolves to current pipx install"
echo ""
echo "Until this script is filled in, the gauntlet records G3 as 'skipped'."
echo "v2.1 launch gate: G3 must produce a real result before v2.1.0 ships."
exit 2
