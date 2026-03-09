"""
Allows running the MCP server as:
    python -m mcp_server [--project-dir <path>] [init|index|status]

This is the recommended approach when `codevira-mcp` is not in the MCP
host's PATH (common on macOS with Homebrew or user-level pip installs).

MCP config example:
    {
      "mcpServers": {
        "codevira": {
          "command": "python",
          "args": ["-m", "mcp_server", "--project-dir", "/path/to/project"]
        }
      }
    }
"""
from mcp_server.cli import main

main()
