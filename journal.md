2026-03-10
Action: Fix Antigravity MCP Config format
Result: Success
Reason: The prior mcp_config.json incorrectly specified a directory as the python execution target causing a `__main__` missing error. Config updated to use `python -m mcp_server --project-dir ...` as directed by the tool's updated CLI configuration format.
Decision reinforced: Always follow the recommended execution mode for Python modules (`-m modulename`).
2026-03-10
Action: Update public README for MCP config formats
Result: Success
Reason: The previous configuration snippet using `codevira-mcp` works but fails globally if Python `PATH` is not available in tools like Cursor and Antigravity. The new instructions introduce the `uvx` fast-path for users, alongside the robust explicit `/path/to/venv/...python -m mcp_server` approach.
Decision reinforced: Provide foolproof absolute paths and executable fallbacks (`uvx`) to prevent early user configuration friction.
2026-03-10
Action: Update public README and FAQ for Multi-Project Roadmaps
Result: Success
Reason: The user observed an agent returning a roadmap from a different project in a global MCP client. I added an FAQ entry and a prominent warning in the README explaining how Global setups require multiple unique servers to route requests correctly per-project.
Decision reinforced: Address global client architectural pain points openly in the documentation (FAQ).
2026-03-10
Action: Update Antigravity `mcp_config.json` for local uninstalled execution
Result: Success
Reason: The user encountered a `No module named mcp_server.__main__` error in Antigravity for the `agent-mcp` repository. Because the code is not installed via `pip install -e .` in that venv, Python couldn't resolve the `mcp_server` module. Fixed by pre-pending `PYTHONPATH` using `/usr/bin/env` in the `mcp_config.json` command.
Decision reinforced: Local, non-pip-installed execution requires explicit PYTHONPATH injection, especially in global clients.
2026-03-10
Action: Revert PYTHONPATH hack in Antigravity config
Result: Success
Reason: The user pointed out that `PYTHONPATH` injection was a hack and uncharacteristic of earlier discussions. Replaced the `env` hack with the standard, clean absolute script execution path (`path/to/venv/python path/to/module/__main__.py`).
Decision reinforced: Avoid OS-level env hacks in JSON configs; rely on absolute python script paths for local uninstalled execution.
2026-03-10
Action: Retrieve local roadmap via CLI/File
Result: Success
Reason: The user requested the roadmap, but the AI's MCP tool suite had not yet refreshed to include `codevira-agent`. Fell back to reading the newly generated `.codevira/roadmap.yaml` directly.
2026-03-10
Action: Discovered root cause of `--project-dir` ignoring bug
Result: Success
Reason: The Antigravity MCP config used `python -m mcp_server --project-dir`. The main module for `server.py` does not parse `sys.argv`, bypassing `argparse` completely. Only the `codevira-mcp` CLI (`cli.py`) handles `--project-dir`. As a result, global clients silently ignored the project argument and created fallback data folders. Switched `mcp_config.json` to use the `codevira-mcp` executable directly to ensure isolated directory resolution works correctly.
2026-03-10
Action: Commit and Push v1.1.2 Docs Update
Result: Success
Reason: The user requested not to bump the PyPI version to 1.1.3 since the changes were just documentation additions. Reverted pyproject.toml to 1.1.2, added all docs/FAQ updates, committed with a detailed message outlining the Global MCP configuration issue, and tagged the release `v1.1.2` directly on GitHub.
2026-03-10
Action: Update CHANGELOG.md for v1.1.2 Docs
Result: Success
Reason: The user requested tracking the `1.1.2` release in the changelog. Appended the multi-project global client guide and gitignore additions to the changelog and pushed to main.

2026-03-10
Action: Draft GitHub Reply and Update Roadmap
Result: Success
Reason: The user requested to draft a reply for Issue #3 and add global installation to tracking. Added to ROADMAP.md and pushed to main.
2026-03-10
Action: Begin Codevira Website Development
Result: Success
Reason: The user requested a website for Codevira to be published on github.io. Initiating planning phase for a static landing page with premium aesthetics.
