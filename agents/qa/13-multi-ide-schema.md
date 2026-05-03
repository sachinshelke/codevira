# QA Angle 13 — Multi-IDE Schema Verification

**Subagent type:** Explore (with WebFetch capability)
**Time-box:** 30 min per IDE; ~3 hrs for all 7 tier-1 IDEs
**Catches:** Per-IDE protocol mismatches (Hero 5/9 silently broken)

## Prompt

```
You are verifying that codevira's wiring for {ide_name} matches that
IDE's actual current schema. Don't trust the implementer's mental
model — fetch the IDE's official docs and field-by-field compare.

Steps:

1. **Fetch the official source-of-truth** for {ide_name}:
   - Claude Code:    https://code.claude.com/docs/en/hooks
   - Cursor:         https://cursor.com/docs/agent/mcp (or current MCP docs)
   - Windsurf:       https://docs.codeium.com/windsurf/mcp
   - Antigravity:    https://gemini.google.com/docs (or vendor's page)
   - Codex CLI:      https://github.com/openai/codex (AGENTS.md spec)
   - GitHub Copilot: https://docs.github.com/copilot/customizing-copilot
   - Claude Desktop: https://claude.ai/desktop or claude.com docs

2. **Extract the exact schema** for the integration we're building:
   - For lifecycle hooks: stdin JSON shape, stdout JSON shape, exit codes
   - For MCP server config: which JSON file, exact field names, examples
   - For instructions files: filename, content rules, max length, parsing
     order
   - For each: what changes between IDE versions (compat warnings)

3. **Open codevira's wiring** for {ide_name} (e.g., for Claude Code:
   `mcp_server/engine/wiring/claude_code_hooks.py`; for Cursor: the
   AGENTS.md generator in `mcp_server/agents_md.py`; etc.).

4. **Compare every field name, every JSON shape**:
   - Field names — case-sensitive, snake vs camel?
   - Required vs optional?
   - Nesting structure (R5 caught additionalContext at wrong level)
   - Value types (string vs object vs array)
   - Edge cases the IDE handles silently (e.g., trailing newlines)

5. **Output a compatibility report**:
   - **[MATCH]** for each field that aligns
   - **[DRIFT]** for each field that doesn't — with severity
   - **[GAP]** for IDE features we don't yet handle but should
   - **[NEW]** for IDE features added since our implementation that we
     should adopt

Cap at 1500 words. Cite the exact docs URL for every claim.
```

## Trigger

Step 0 of any hero that integrates with a new IDE. Re-run quarterly
once shipped (IDEs change schemas).

## Expected output

R5 (Claude Code) found 4 protocol mismatches that R1-R4 missed
because all prior testing was synthetic. Per-IDE runs likely surface
similar count for first audit.
