"""test_binding_e2e.py — PROVE the project binding through the real MCP
protocol, not a fake session.

The unit tests in test_project_binding.py exercise the resolver with a
stub ``session``. This test connects a REAL MCP client to the REAL
codevira server over the SDK's in-memory transport, has the client
advertise a workspace root via the roots capability, calls an actual
tool, and asserts the server bound to that root — even though the process
cwd points somewhere else. This is the evidence that the fix works with a
genuine client (Claude Code / Cursor / Windsurf all speak this protocol).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ListRootsResult, Root


def _reset_binding_globals(monkeypatch) -> None:
    """Reset the per-process binding globals via monkeypatch so they are
    AUTO-RESTORED at teardown — otherwise the project_dir_override set by
    this test (a deleted tmp path) leaks into later tests."""
    import mcp_server.paths as paths
    import mcp_server.server as srv

    monkeypatch.setattr(srv, "_roots_bind_attempted", False)
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.delenv("CODEVIRA_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEVIRA_PROJECT_ROOT", raising=False)


async def _call_one_tool_with_roots(project_root: Path) -> None:
    from mcp_server.server import server

    async def list_roots_cb(context):  # noqa: ANN001 — SDK callback
        return ListRootsResult(roots=[Root(uri=f"file://{project_root}", name="proj")])

    async with create_connected_server_and_client_session(
        server, list_roots_callback=list_roots_cb
    ) as client:
        await client.initialize()
        # Any tool triggers _bind_project_from_client_roots at the top of
        # call_tool. list_tags is lightweight and side-effect-free.
        await client.call_tool("list_tags", {})


def test_server_binds_to_client_root_over_real_protocol(tmp_path, monkeypatch):
    # The real project the client has open — with .codevira so the
    # conservative gate accepts it.
    project = tmp_path / "realproj"
    (project / ".codevira").mkdir(parents=True)

    # Isolate the global DB so nothing touches the real ~/.codevira.
    home = tmp_path / "home" / ".codevira"
    home.mkdir(parents=True)
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )

    # cwd is a DIFFERENT directory — so binding to `project` can only come
    # from the client's roots, not cwd discovery. This is the crux.
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    _reset_binding_globals(monkeypatch)

    asyncio.run(_call_one_tool_with_roots(project))

    # Evidence: the server pinned the client's root, not cwd.
    import mcp_server.paths as paths

    assert paths._project_dir_override == project.resolve(), (
        "server did not bind to the client's workspace root via MCP roots"
    )


def test_server_does_not_bind_to_git_only_root(tmp_path, monkeypatch):
    """Conservative gate, end-to-end: a workspace root that's a git repo
    but NOT an initialized codevira project must NOT be bound (no
    .codevira) — the server leaves the binding to cwd discovery."""
    project = tmp_path / "monorepo"
    (project / ".git").mkdir(parents=True)  # git, but no .codevira

    home = tmp_path / "home" / ".codevira"
    home.mkdir(parents=True)
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    _reset_binding_globals(monkeypatch)

    asyncio.run(_call_one_tool_with_roots(project))

    import mcp_server.paths as paths

    assert paths._project_dir_override is None, (
        "a git-only root must not hijack the binding"
    )


def test_explicit_pin_wins_over_client_roots(tmp_path, monkeypatch):
    """M1: an explicit CODEVIRA_PROJECT_DIR pin must NEVER be overridden by
    the client's workspace roots. This is the core safety guarantee — if it
    regressed, client roots could silently hijack a deliberately pinned
    project. The whole suite would otherwise pass with the guarantee
    inverted."""
    pinned = tmp_path / "pinned"
    (pinned / ".codevira").mkdir(parents=True)
    other_root = tmp_path / "other"
    (other_root / ".codevira").mkdir(parents=True)

    home = tmp_path / "home" / ".codevira"
    home.mkdir(parents=True)
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path", lambda: home / "global.db"
    )
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    _reset_binding_globals(monkeypatch)
    # Explicit pin to `pinned`; the client advertises a DIFFERENT root.
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(pinned))

    asyncio.run(_call_one_tool_with_roots(other_root))

    import mcp_server.paths as paths

    # The roots-binding path must have been skipped entirely (pin respected),
    # and project resolution must land on the pinned dir, not the client root.
    assert paths._project_dir_override is None
    assert paths.get_project_root() == pinned.resolve()
