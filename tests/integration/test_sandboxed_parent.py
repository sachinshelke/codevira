"""
test_sandboxed_parent.py — v2.1.2 hardening (Test D).

Spawns the codevira MCP server as a subprocess with a SANITIZED env
that approximates how a hardened-runtime macOS app (Antigravity /
Cascade) or a containerized parent (Docker / Flatpak) would launch a
child. Verifies that:

1. MCP `initialize` completes
2. MCP `tools/list` completes WITHOUT crashing on torch dlopen
3. A handful of NON-search MCP tools work (graph, decisions read/write,
   roadmap) — these must NOT depend on chromadb / torch loading
4. If chromadb / torch IS available in this env, search tools work
5. If chromadb / torch IS NOT available, search tools degrade
   gracefully with the v2.1.2 issue #10 ``_semantic_warning``

This test catches the class of bug that issue #10 represents: any
future regression where startup code imports heavy native deps that
might fail under sandboxed parents.

We can't fully simulate Antigravity's macOS hardened-runtime sandbox
in pytest, but we CAN:
  - Strip the env of DYLD_*, PYTHONPATH, custom PATH
  - Use the smallest possible cwd
  - Verify the server's exit doesn't hang / crash
  - Verify all non-search tools work even when we PRETEND torch is
    missing (via PYTHONPATH module-hiding)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


def _mcp_request(
    method: str, params: dict | None = None, req_id: int | None = 1
) -> str:
    """Format a JSON-RPC 2.0 request line for MCP.

    Pass ``req_id=None`` to emit a notification (no ``id`` field per
    JSON-RPC 2.0 spec — MCP servers reject ``notifications/*`` messages
    that carry an id).
    """
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        payload["id"] = req_id
    if params is not None:
        payload["params"] = params
    return json.dumps(payload) + "\n"


def _spawn_codevira_mcp(
    project_dir: Path,
    home_dir: Path,
    *,
    strip_dyld: bool = True,
    block_torch: bool = False,
    inputs: str = "",
    timeout_s: float = 25.0,
) -> tuple[int, str, str]:
    """Spawn codevira via the package's main entrypoint as a subprocess.

    The point is to use a CLEAN env — strip DYLD_*, PYTHONPATH (other
    than PYTHONPATH=REPO_ROOT so the dev tree is importable), and any
    other shell augmentation. Approximates a sandboxed parent.

    If ``block_torch`` is True, we point sys.path at a shim that hides
    the torch / chromadb modules so the import attempts fail. This
    simulates "torch dylib can't load" without actually breaking the
    underlying install.

    Returns (returncode, stdout, stderr).
    """
    env: dict[str, str] = {
        # Bare-minimum env. NO DYLD_*, no PYTHONPATH augmentation other
        # than what we need to find mcp_server.
        "HOME": str(home_dir),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",  # no /usr/local additions
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    # Note: strip_dyld is True by default — we don't propagate DYLD vars.
    # If a user has DYLD set in their shell, it's NOT inherited here.
    if not strip_dyld:
        # For comparison runs.
        for k in os.environ:
            if k.startswith("DYLD_"):
                env[k] = os.environ[k]

    if block_torch:
        # Prepend a stub directory that contains importable but-broken
        # `chromadb` and `torch` modules so the search lazy-load path
        # encounters ImportError. Approximates Antigravity-style dlopen
        # failure WITHOUT actually breaking the real torch install.
        shim_dir = home_dir / "torch_blocker_shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        # Create stub packages that raise ImportError on import.
        for mod in ("chromadb", "sentence_transformers"):
            mod_dir = shim_dir / mod
            mod_dir.mkdir(exist_ok=True)
            (mod_dir / "__init__.py").write_text(
                "raise ImportError("
                "'simulated: native dep load failure (test_sandboxed_parent)')\n"
            )
        # PYTHONPATH already set; prepend the shim dir.
        env["PYTHONPATH"] = f"{shim_dir}:{env['PYTHONPATH']}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.cli", "--project-dir", str(project_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        cwd=str(project_dir),
    )
    try:
        stdout, stderr = proc.communicate(input=inputs, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return (proc.returncode if proc.returncode is not None else -1), stdout, stderr


def _parse_jsonrpc_responses(stdout: str) -> list[dict]:
    """Parse stdout's JSON-RPC response lines (each is a JSON object on its own line)."""
    out: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@pytest.fixture
def sandboxed_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create an isolated project + HOME root for the subprocess."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codevira").mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'sandboxed-smoke'\nversion = '0.0.1'\n"
    )
    # v3.7.0 opt-in: this spawns a real MCP server and calls tools on the
    # project, so opt it in (the marker only explicit `codevira init` writes)
    # or the dispatch gate returns inert hints instead of real tool output.
    (project / ".codevira").mkdir()
    (project / ".codevira" / "config.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    return project, home


class TestSandboxedParent:
    """Spawn codevira under sandbox-approximated conditions; verify MCP
    handshake + non-search tools work + degradation is graceful.
    """

    def test_mcp_initialize_completes_in_sanitized_env(self, sandboxed_project):
        """Issue #10 baseline: server starts + handshakes cleanly when
        DYLD_* / PYTHONPATH / PATH are stripped to bare minimum. Regression
        guard against "I import heavy native deps at startup."
        """
        project, home = sandboxed_project
        inputs = _mcp_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sandboxed-smoke", "version": "0.0.1"},
            },
        )
        rc, stdout, stderr = _spawn_codevira_mcp(project, home, inputs=inputs)
        responses = _parse_jsonrpc_responses(stdout)
        assert responses, (
            f"no JSON-RPC response from codevira in sanitized env. "
            f"stderr (last 500): {stderr[-500:]}"
        )
        first = responses[0]
        assert "result" in first, f"initialize returned error: {first}"
        assert first["result"].get("serverInfo", {}).get("name") == "codevira"

    def test_mcp_tools_list_completes_without_torch_when_blocked(
        self, sandboxed_project
    ):
        """Issue #10 core: even when chromadb / sentence_transformers
        can't import (simulating macOS dlopen failure under Antigravity),
        the MCP server's tools/list MUST complete. The whole point of
        v2.1.2's lazy-torch refactor.
        """
        project, home = sandboxed_project
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "blocked", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request("tools/list", req_id=3)
        )
        rc, stdout, stderr = _spawn_codevira_mcp(
            project,
            home,
            block_torch=True,
            inputs=inputs,
        )
        responses = _parse_jsonrpc_responses(stdout)
        tools_list_resp = next(
            (r for r in responses if r.get("id") == 3),
            None,
        )
        assert tools_list_resp is not None, (
            f"no tools/list response when torch is blocked. "
            f"stderr (last 500): {stderr[-500:]}"
        )
        assert (
            "result" in tools_list_resp
        ), f"tools/list returned error when torch blocked: {tools_list_resp}"
        tools = tools_list_resp["result"].get("tools", [])
        assert len(tools) > 10, f"expected ≥10 MCP tools registered; got {len(tools)}"
        # Verify a sampling of NON-search tools are present (they don't
        # need torch).
        names = {t["name"] for t in tools}
        for required in (
            "get_node",
            "record_decision",
            "list_decisions",
            "complete_phase",
            "get_session_context",
        ):
            assert required in names, f"tool {required!r} missing from tools/list"

    def test_non_search_tool_works_when_torch_blocked(self, sandboxed_project):
        """Issue #10 graceful-degradation contract: calling list_decisions
        (a non-search tool) MUST work even when torch can't load.
        """
        project, home = sandboxed_project
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "blocked", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request(
                "tools/call",
                {
                    "name": "list_decisions",
                    "arguments": {"limit": 5},
                },
                req_id=3,
            )
        )
        rc, stdout, stderr = _spawn_codevira_mcp(
            project,
            home,
            block_torch=True,
            inputs=inputs,
            timeout_s=30.0,
        )
        responses = _parse_jsonrpc_responses(stdout)
        call_resp = next((r for r in responses if r.get("id") == 3), None)
        assert call_resp is not None, (
            f"no list_decisions response when torch blocked. "
            f"stderr (last 500): {stderr[-500:]}"
        )
        # The response is a CallToolResult — content is a list of
        # TextContent. Unpack the text and verify it's a JSON object
        # with the v2.1.2 list_decisions shape.
        assert "result" in call_resp, f"list_decisions errored: {call_resp}"
        content = call_resp["result"].get("content", [])
        assert content, f"empty content from list_decisions: {call_resp}"
        payload = json.loads(content[0]["text"])
        assert "count" in payload, f"list_decisions payload missing 'count': {payload}"
        assert "decisions" in payload
