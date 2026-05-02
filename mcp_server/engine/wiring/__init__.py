"""
wiring — adapters that translate external interception points into
HookEvents and back.

Two adapters in v2.0:

  - ``claude_code_hooks``: ingest Claude Code's hook script JSON input
    on stdin, run engine.dispatch, write back the JSON the hook protocol
    expects on stdout (and exit code).

  - ``mcp_dispatch``: invoked from inside the MCP server's ``call_tool``
    handler. Receives tool name + args + project root, runs engine.dispatch
    pre-call (to allow blocking), forwards the result back to call_tool.

Future: a Cursor adapter will land here when Cursor ships hook APIs.
"""
