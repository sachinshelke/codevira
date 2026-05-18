"""
Codevira — Persistent memory layer for AI coding agents.

Local-first MCP server that gives every AI tool you use (Claude Code,
Cursor, Windsurf, Antigravity) shared persistent memory of your codebase:
context graph, decision log, roadmap, and adaptive learning.

Public CLI entry point: `codevira` (see `mcp_server.cli:main`).

For programmatic use, prefer the CLI or the MCP protocol over importing
internals — internal APIs may change between minor versions.
"""

from mcp_server.cli import main

__all__ = ["main"]
__version__ = "2.1.2"
