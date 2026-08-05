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

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Spawn budgets + timing instrumentation
# ---------------------------------------------------------------------------
# 2026-08-03: G1.7 twice failed the release gauntlet on timing alone (PR #17,
# runs 30751330963 and 30765384673 — two different tests, two different
# commits, both green on re-run of the identical commit). Both failures
# reported a bare `None` ("no tools/list response"), which says nothing about
# how long we actually waited or which phase consumed the budget.
#
# That matters more than an ordinary flake: G1.7 is release-blocking, and a
# gate that can't explain itself trains people to re-run until green — exactly
# how the Antigravity-class regression this file exists to catch (issue #10)
# would slip through. So every spawn is timestamped per phase and the timeline
# is printed on any failure. See _SpawnResult.diagnostics().
#
# Measured reference (local, macOS, python3.13, at this commit's parent
# df0081f, __pycache__ deliberately purged so bytecode compile is paid on
# every spawn):
#     spawn -> initialize response   0.90s   <- ~82% of the budget
#     initialize -> tools/call resp  0.11s
#     tools/call -> process exit     0.09s
#     total per spawn                1.10s;  the whole file 7.4s
# Spawns #2 and #3 measured within 0.06s of #1, so bytecode compilation is
# NOT the variable cost.
#
# Two things measured while adding this that argue AGAINST "CI is just slow",
# and are the reason the timeout was not simply raised:
#
#   1. Sweeping the budget from 0.30s to 1.00s in 5ms steps never once
#      produced "initialize answered, follow-up did not". Once the server
#      starts answering, the responses land within ~10ms of each other
#      (invalid-root spawn: id=1 at 0.788s, id=3 at 0.795s). A uniformly
#      slow runner preserves that ratio, so a timeout slicing between the
#      two — twice, on two different tests — is implausible.
#   2. subprocess.communicate() does NOT lose partial stdout when it times
#      out (verified: the second communicate() after kill() returns it), so
#      the missing responses on CI were genuinely never emitted.
#
# Which leaves an early exit — the server stopping on its own, under budget,
# after answering initialize — as the leading hypothesis. The old harness
# reported that identically to a timeout, which is why two re-runs produced
# no information. diagnostics() now separates the two verdicts explicitly.
# UNVERIFIED against an actual CI run; that is what the next failure is for.
#
# Steady-state budget. 30.0s is not a new number — it is what three of the
# five call sites already passed explicitly; the other two silently used a
# 25.0s default that was never chosen against a measurement. Unified so there
# is one number to reason about instead of an unexplained 25-vs-30 split.
_SPAWN_TIMEOUT_S = 30.0

# The FIRST spawn in a session is not comparable to the ones after it: it pays
# interpreter start plus OS page-cache misses across the whole mcp / pydantic /
# anyio import tree on a runner that has never executed this path. One cold
# start should not have to fit the same window as a warm one, so it gets its
# own budget rather than inflating the budget for every spawn.
#
# UNVERIFIED: 30.0s is provisional — we have no CI-side measurement yet, only
# the local numbers above (where the page cache was already warm from pytest
# itself, so local runs cannot show this effect at all). The point of the
# instrumentation is that the next failure prints the real number instead of
# `None`; tighten this constant once a CI timeline exists.
_COLD_START_GRACE_S = 30.0

# Set once the first subprocess of the session has been spawned.
_first_spawn_done = False


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


def _request_ids(inputs: str) -> set[int]:
    """Ids the caller expects an answer for, read back out of ``inputs``.

    Derived rather than passed so every call site is covered without one of
    them being forgotten — a spawn that forgot to declare its ids would
    silently go back to closing stdin early, which is exactly the failure
    mode being removed. Notifications carry no id and are skipped.
    """
    ids: set[int] = set()
    for line in inputs.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed-input tests pass junk on purpose
        rid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(rid, int):
            ids.add(rid)
    return ids


@dataclasses.dataclass
class _SpawnResult:
    """Outcome of one sandboxed spawn, with a per-phase wall-clock timeline.

    ``timeline`` is a list of ``(elapsed_s, event)`` appended from both the
    main thread and the two reader threads, so it is rendered sorted by
    timestamp rather than by insertion order.
    """

    returncode: int
    stdout: str
    stderr: str
    timeline: list[tuple[float, str]]
    response_ids: list[int]
    output_times: list[float]
    timed_out: bool
    budget_s: float
    elapsed_s: float
    cold_start: bool
    stdin_open_when_answered: bool | None = None

    def diagnostics(self) -> str:
        """Phase timeline + verdict, for inclusion in every assertion message.

        A failing release gate has to say WHERE the time went. The two PR #17
        failures said only that a response was ``None``, which is consistent
        with "server died during import", "server was slow to start" and
        "server started fine then stalled on one request" — three different
        bugs with three different fixes.
        """
        budget_desc = f"{self.budget_s:.1f}s"
        if self.cold_start:
            budget_desc += (
                f" = {_SPAWN_TIMEOUT_S:.1f}s steady-state"
                f" + {_COLD_START_GRACE_S:.1f}s first-spawn cold-start grace"
            )
        lines = [f"--- spawn timeline (budget {budget_desc}) ---"]
        for elapsed, event in sorted(self.timeline, key=lambda row: row[0]):
            lines.append(f"  {elapsed:8.3f}s  {event}")
        lines.append(f"  {self.elapsed_s:8.3f}s  [total wall clock]")

        if self.response_ids:
            lines.append(f"  responses seen, in order: {self.response_ids}")
        else:
            lines.append(
                "  responses seen: NONE — the server produced no JSON-RPC output at all"
            )

        if self.timed_out:
            last_out = max(self.output_times) if self.output_times else None
            if last_out is None:
                lines.append(
                    f"  VERDICT: TIMED OUT at {self.budget_s:.1f}s having never "
                    f"produced a single line of stdout. The cost is upstream of "
                    f"request handling — interpreter start, imports, or server "
                    f"init. Raising the budget is the right lever here."
                )
            else:
                silence = self.elapsed_s - last_out
                lines.append(
                    f"  VERDICT: TIMED OUT at {self.budget_s:.1f}s. Last output at "
                    f"{last_out:.3f}s, then {silence:.1f}s of silence. The server "
                    f"was already answering, so this is NOT spawn/import cost — "
                    f"raising the budget would hide it. Look at what the "
                    f"un-answered request does."
                )
        else:
            # The failure mode the old harness could not distinguish from a
            # timeout: the server EXITED on its own, under budget, having
            # answered only some of the requests. Same symptom ("response was
            # None"), completely different fix — the budget is innocent.
            lines.append(
                f"  VERDICT: did NOT time out — the process exited on its own "
                f"after {self.elapsed_s:.3f}s of a {self.budget_s:.1f}s budget "
                f"(rc={self.returncode}). If a response is missing here, the "
                f"budget is NOT implicated: the server stopped early. Check "
                f"stderr and the exit code."
            )
        if self.stderr.strip():
            lines.append(f"--- stderr (last 800 chars) ---\n{self.stderr[-800:]}")
        return "\n".join(lines)


def _spawn_codevira_mcp(
    project_dir: Path,
    home_dir: Path,
    *,
    strip_dyld: bool = True,
    block_torch: bool = False,
    inputs: str = "",
    timeout_s: float = _SPAWN_TIMEOUT_S,
    allow_cold_start_grace: bool = True,
) -> _SpawnResult:
    """Spawn codevira via the package's main entrypoint as a subprocess.

    The point is to use a CLEAN env — strip DYLD_*, PYTHONPATH (other
    than PYTHONPATH=REPO_ROOT so the dev tree is importable), and any
    other shell augmentation. Approximates a sandboxed parent.

    If ``block_torch`` is True, we point sys.path at a shim that hides
    the torch / chromadb modules so the import attempts fail. This
    simulates "torch dylib can't load" without actually breaking the
    underlying install.

    ``timeout_s`` is the STEADY-STATE budget; the first spawn of the session
    additionally gets ``_COLD_START_GRACE_S``. Pass
    ``allow_cold_start_grace=False`` to opt out (the instrumentation tests
    need a budget they can actually exceed).

    Returns a :class:`_SpawnResult` — pass ``.diagnostics()`` into any
    assertion message so a failure reports its phase timeline.
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

    global _first_spawn_done
    cold_start = allow_cold_start_grace and not _first_spawn_done
    _first_spawn_done = True
    budget_s = timeout_s + (_COLD_START_GRACE_S if cold_start else 0.0)

    timeline: list[tuple[float, str]] = []
    response_ids: list[int] = []
    output_times: list[float] = []
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    t0 = time.monotonic()

    def _mark(event: str, *, is_output: bool = False) -> None:
        # list.append is atomic under the GIL; each row carries its own
        # timestamp, so cross-thread interleaving is fine (rendered sorted).
        elapsed = time.monotonic() - t0
        timeline.append((elapsed, event))
        if is_output:
            output_times.append(elapsed)

    # Read stdout line-by-line on a thread rather than via communicate(), which
    # is all-or-nothing: it can only tell us the TOTAL wall time, never which
    # phase spent it. Timestamping each JSON-RPC line as it arrives is the
    # whole point — it separates "never started" from "started then stalled".
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.cli", "--project-dir", str(project_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        cwd=str(project_dir),
    )

    def _pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                _mark(f"non-JSON stdout line: {stripped[:70]!r}", is_output=True)
                continue
            req_id = msg.get("id")
            if req_id is None:
                _mark(f"notification {msg.get('method')!r}", is_output=True)
                continue
            response_ids.append(req_id)
            kind = "result" if "result" in msg else "JSON-RPC error"
            _mark(f"response id={req_id} ({kind})", is_output=True)

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        stderr_chunks.append(proc.stderr.read())

    readers = [
        threading.Thread(target=_pump_stdout, name="stdout-pump", daemon=True),
        threading.Thread(target=_pump_stderr, name="stderr-pump", daemon=True),
    ]
    for reader in readers:
        reader.start()

    expected_ids = _request_ids(inputs)
    stdin_open_when_answered: bool | None = None

    timed_out = False
    try:
        # Every `inputs` in this file is well under 1 KB, so this fits the pipe
        # buffer and cannot deadlock against a child that has not started
        # reading yet. Revisit if a test ever pipes a large payload.
        assert proc.stdin is not None
        try:
            proc.stdin.write(inputs)
            proc.stdin.flush()
            _mark(f"stdin written ({len(inputs.splitlines())} messages)")
        except (BrokenPipeError, OSError) as exc:
            _mark(f"stdin write FAILED ({exc!r}) — child exited before reading")

        # Hold stdin OPEN until the answers arrive.
        #
        # Closing it immediately after the write is what made this test flake
        # (CI 2026-08-03, run 30810510988): the mcp SDK's stdio transport
        # builds an UNBUFFERED channel and wraps the reader in
        # `async with read_stream_writer:`, so EOF closes the channel and
        # tears the session down. A request already dispatched but not yet
        # answered loses its response — observed as `responses seen: [1]`
        # then `process exit rc=0` at 0.665s of a 30s budget, i.e. a clean
        # early exit, NOT the timeout the budget was there to catch.
        #
        # That loop is the SDK's, not ours — we hand it `stdio_server()` and
        # never touch the read side. It is also not reachable in production:
        # a real MCP client keeps stdin open for the life of the session, and
        # a client that HAS closed stdin is no longer waiting for a reply.
        # So the artificial trigger is what gets removed, and the assertion
        # keeps its teeth: a genuinely missing response still fails, it just
        # fails on the budget instead of on a shutdown race.
        deadline = time.monotonic() + budget_s
        if expected_ids:
            while time.monotonic() < deadline:
                if expected_ids.issubset(set(response_ids)):
                    # Real fd state, not a log line: a mark can be emitted by
                    # a later close() and would pass regardless.
                    stdin_open_when_answered = not proc.stdin.closed
                    _mark(
                        f"all {len(expected_ids)} expected response(s) received "
                        f"(stdin_open={stdin_open_when_answered})"
                    )
                    break
                if proc.poll() is not None:
                    _mark(f"child exited early rc={proc.returncode} while awaiting ids")
                    break
                time.sleep(0.02)
            else:
                _mark("budget exhausted awaiting responses")

        try:
            proc.stdin.close()
            _mark("stdin closed")
        except (BrokenPipeError, OSError):
            pass

        # REMAINING budget, never a fresh one. A floor here would hand out
        # time beyond `budget_s` and make an over-budget spawn look on-time,
        # silently disarming the timeout assertions this harness exists for.
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        _mark(f"process exit rc={proc.returncode}")
    except subprocess.TimeoutExpired:
        timed_out = True
        _mark(f"TIMEOUT at the {budget_s:.1f}s budget — sending SIGKILL")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL ignored
            _mark("child did not die within 10s of SIGKILL")
    finally:
        for reader in readers:
            reader.join(timeout=10)
            if reader.is_alive():  # pragma: no cover - pipe still held open
                _mark(f"{reader.name} still alive after join — output truncated")

    elapsed_s = time.monotonic() - t0
    result = _SpawnResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        timeline=timeline,
        response_ids=response_ids,
        output_times=output_times,
        timed_out=timed_out,
        budget_s=budget_s,
        elapsed_s=elapsed_s,
        cold_start=cold_start,
        stdin_open_when_answered=stdin_open_when_answered,
    )
    if timed_out:
        # Belt-and-braces: surface the timeline even if a caller forgets to
        # thread diagnostics() into its assertion message.
        print(result.diagnostics(), file=sys.stderr)
    return result


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
        res = _spawn_codevira_mcp(project, home, inputs=inputs)
        responses = _parse_jsonrpc_responses(res.stdout)
        assert (
            responses
        ), f"no JSON-RPC response from codevira in sanitized env.\n{res.diagnostics()}"
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
        res = _spawn_codevira_mcp(
            project,
            home,
            block_torch=True,
            inputs=inputs,
        )
        responses = _parse_jsonrpc_responses(res.stdout)
        tools_list_resp = next(
            (r for r in responses if r.get("id") == 3),
            None,
        )
        assert (
            tools_list_resp is not None
        ), f"no tools/list response when torch is blocked.\n{res.diagnostics()}"
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
        res = _spawn_codevira_mcp(
            project,
            home,
            block_torch=True,
            inputs=inputs,
        )
        responses = _parse_jsonrpc_responses(res.stdout)
        call_resp = next((r for r in responses if r.get("id") == 3), None)
        assert (
            call_resp is not None
        ), f"no list_decisions response when torch blocked.\n{res.diagnostics()}"
        # The response is a CallToolResult — content is a list of
        # TextContent. Unpack the text and verify it's a JSON object
        # with the v2.1.2 list_decisions shape.
        assert "result" in call_resp, f"list_decisions errored: {call_resp}"
        content = call_resp["result"].get("content", [])
        assert content, f"empty content from list_decisions: {call_resp}"
        payload = json.loads(content[0]["text"])
        assert "count" in payload, f"list_decisions payload missing 'count': {payload}"
        assert "decisions" in payload

    def test_invalid_root_degrades_gracefully_instead_of_crashing(self, tmp_path):
        """v3.7.1 fix A: launching with a forbidden/system project root — the
        exact condition Antigravity creates (it spawns the MCP server at cwd=/
        with no cwd/roots support) — must NOT sys.exit(1) with an EOF that
        kills every codevira tool. The server must complete the MCP
        ``initialize`` handshake and serve an inert 'open a project' hint.

        Before this fix, server.main() printed 'is a system directory' and
        sys.exit(1) before the handshake, so the client saw `initialize: EOF`.
        """
        home = tmp_path / "home"
        home.mkdir()
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "badroot", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request(
                "tools/call",
                {"name": "get_session_context", "arguments": {}},
                req_id=3,
            )
        )
        # Spawn with a FORBIDDEN root ("/") — the Antigravity crash condition.
        res = _spawn_codevira_mcp(Path("/"), home, inputs=inputs)
        responses = _parse_jsonrpc_responses(res.stdout)
        init = next((r for r in responses if r.get("id") == 1), None)
        assert init is not None and "result" in init, (
            f"initialize did NOT complete — server crashed on an invalid root "
            f"instead of degrading.\n{res.diagnostics()}"
        )
        assert init["result"].get("serverInfo", {}).get("name") == "codevira"
        # A tool call must return an inert hint, not a crash / no-response.
        call = next((r for r in responses if r.get("id") == 3), None)
        assert call is not None and "result" in call, (
            f"tool call did not return under an invalid root: {call}.\n"
            f"{res.diagnostics()}"
        )
        payload = json.loads(call["result"]["content"][0]["text"])
        assert (
            payload.get("not_opted_in") is True or payload.get("no_project") is True
        ), f"expected an inert 'open a project' hint under invalid root, got: {payload}"

    def test_non_opted_project_creates_no_ghost_dir(self, tmp_path):
        """v3.7.0 opt-in regression guard: launching the real MCP server in a
        project the user never `codevira init`-ed must NOT create a centralized
        ~/.codevira/projects/<key>/ ghost dir. The dominant vector was the
        startup outcome-analysis thread opening the graph.db (invisible to unit
        tests that don't spawn the real server). See test_opt_in.py for the
        per-gate unit coverage.
        """
        home = tmp_path / "home"
        home.mkdir()
        project = tmp_path / "ghostproj"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'ghostproj'\nversion = '0.0.1'\n"
        )
        # NOTE: deliberately NO .codevira/config.yaml — this project is NOT
        # opted in.
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ghost", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request(
                "tools/call",
                {"name": "get_impact", "arguments": {"file_path": "pyproject.toml"}},
                req_id=3,
            )
        )
        res = _spawn_codevira_mcp(project, home, inputs=inputs)
        # The tool call returns the opt-in hint...
        responses = _parse_jsonrpc_responses(res.stdout)
        call_resp = next((r for r in responses if r.get("id") == 3), None)
        assert call_resp is not None, f"no get_impact response.\n{res.diagnostics()}"
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        assert (
            payload.get("not_opted_in") is True
        ), f"expected not_opted_in hint for un-init'd project, got: {payload}"
        # ...and NOTHING is adopted: no centralized ghost dir, no in-repo store.
        projects_dir = home / ".codevira" / "projects"
        ghosts = list(projects_dir.iterdir()) if projects_dir.is_dir() else []
        assert not ghosts, f"opt-in leak: server created ghost dir(s): {ghosts}"
        assert not (
            project / ".codevira"
        ).exists(), "opt-in leak: in-repo store created"


class TestSpawnInstrumentation:
    """Guard the diagnostics themselves.

    G1.7 is release-blocking, so when it fails it has to say WHY. These tests
    fail if the per-phase timeline regresses back to a bare ``None`` — the
    state that made PR #17's two failures (runs 30751330963, 30765384673)
    undiagnosable and cost two re-runs of a merge-blocking gate.
    """

    def test_diagnostics_names_the_stalled_phase_on_partial_output(self):
        """Reproduces run 30765384673's exact shape — initialize answered,
        the follow-up never did — and asserts the report distinguishes it
        from a server that never started.

        These two need opposite fixes: 'never started' means raise the
        budget, 'started then stalled' means the budget is innocent. A bare
        `None` cannot tell them apart, which is why the first instinct on
        PR #17 was to bump the timeout.
        """
        stalled = _SpawnResult(
            returncode=-9,
            stdout='{"jsonrpc":"2.0","id":1,"result":{}}\n',
            stderr="",
            timeline=[(0.001, "stdin written"), (3.204, "response id=1 (result)")],
            response_ids=[1],
            output_times=[3.204],
            timed_out=True,
            budget_s=30.0,
            elapsed_s=30.1,
            cold_start=False,
        )
        report = stalled.diagnostics()
        assert "TIMED OUT" in report
        assert "3.204" in report, "must report WHEN the last output arrived"
        assert "26.9s of silence" in report, "must quantify the stall"
        assert "responses seen, in order: [1]" in report
        assert (
            "NOT spawn/import cost" in report
        ), "partial-output timeouts must steer away from raising the budget"

        never_started = dataclasses.replace(
            stalled,
            stdout="",
            timeline=[(0.001, "stdin written")],
            response_ids=[],
            output_times=[],
        )
        report = never_started.diagnostics()
        assert "never produced a single line of stdout" in report
        assert "Raising the budget is the right lever" in report

    def test_diagnostics_separates_an_early_exit_from_a_timeout(self):
        """The failure mode the old harness could not name.

        Measured while writing this: once the server starts answering,
        initialize and the follow-up land ~10ms apart, and communicate()
        does not drop partial output on timeout. So a missing follow-up on
        CI most likely means the server EXITED, not that it ran long. That
        must not read as a timeout, or the fix goes to the wrong place.
        """
        early_exit = _SpawnResult(
            returncode=1,
            stdout='{"jsonrpc":"2.0","id":1,"result":{}}\n',
            stderr="Traceback ...\n",
            timeline=[
                (0.001, "stdin written"),
                (0.780, "response id=1 (result)"),
                (0.812, "process exit rc=1"),
            ],
            response_ids=[1],
            output_times=[0.780],
            timed_out=False,
            budget_s=30.0,
            elapsed_s=0.812,
            cold_start=False,
        )
        report = early_exit.diagnostics()
        assert "did NOT time out" in report
        assert "rc=1" in report
        assert "budget is NOT implicated" in report
        assert "TIMED OUT" not in report, (
            "an early exit must never be reported as a timeout — that is the "
            "confusion that cost PR #17 two re-runs"
        )

    def test_diagnostics_shows_the_cold_start_split(self):
        """The first-spawn budget must be legible as two separate numbers, so
        nobody reads a 60s budget as 'someone doubled the timeout'.
        """
        res = _SpawnResult(
            returncode=0,
            stdout="",
            stderr="",
            timeline=[],
            response_ids=[],
            output_times=[],
            timed_out=False,
            budget_s=_SPAWN_TIMEOUT_S + _COLD_START_GRACE_S,
            elapsed_s=1.0,
            cold_start=True,
        )
        report = res.diagnostics()
        assert "steady-state" in report and "cold-start grace" in report
        assert f"{_SPAWN_TIMEOUT_S:.1f}s steady-state" in report

    def test_timeout_reports_elapsed_time_instead_of_swallowing_it(
        self, sandboxed_project
    ):
        """A real spawn given an unmeetable budget must come back marked as
        timed out, with a wall-clock number — not silently as 'no response'.

        Cold-start grace is disabled here; with it, the budget would be
        ~60s and this spawn (~1s locally) would simply succeed.
        """
        project, home = sandboxed_project
        res = _spawn_codevira_mcp(
            project,
            home,
            inputs=_mcp_request("initialize", {"protocolVersion": "2024-11-05"}),
            timeout_s=0.05,
            allow_cold_start_grace=False,
        )
        assert res.timed_out is True
        assert res.elapsed_s >= 0.05
        assert res.budget_s == 0.05, "explicit budget must not be silently inflated"
        assert "TIMED OUT" in res.diagnostics()

    def test_successful_spawn_attributes_time_to_each_phase(self, sandboxed_project):
        """The happy path must still record the timeline, because that is the
        baseline a future CI failure gets compared against.
        """
        project, home = sandboxed_project
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "timing", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request("tools/list", req_id=3)
        )
        res = _spawn_codevira_mcp(project, home, inputs=inputs)
        assert res.timed_out is False, res.diagnostics()
        assert res.response_ids == [1, 3], res.diagnostics()

        events = [event for _, event in sorted(res.timeline, key=lambda r: r[0])]
        assert any(e.startswith("response id=1") for e in events), res.diagnostics()
        assert any(e.startswith("response id=3") for e in events), res.diagnostics()
        assert any(e.startswith("process exit") for e in events), res.diagnostics()

        # The timings must be real and ordered — spawn -> initialize -> tools/list.
        init_at = next(t for t, e in res.timeline if e.startswith("response id=1"))
        list_at = next(t for t, e in res.timeline if e.startswith("response id=3"))
        assert 0 < init_at <= list_at <= res.elapsed_s, res.diagnostics()


class TestStdinIsHeldOpenUntilAnswered:
    """The harness must not close stdin while a request is still in flight.

    CI run 30810510988 (2026-08-03, commit c87006a) failed here with
    `responses seen: [1]` and `process exit rc=0` at 0.665s of a 30s budget
    — a clean early exit, not the timeout the budget exists to catch.

    Cause: the mcp SDK's stdio transport builds an UNBUFFERED channel and
    wraps the reader in `async with read_stream_writer:`, so EOF on stdin
    closes the channel and tears the session down. A request already
    dispatched but not yet answered loses its response. That loop belongs to
    the SDK — this repo hands it `stdio_server()` and never touches the read
    side — and it is unreachable in production, because a real MCP client
    holds stdin open for the session and a client that has closed stdin is
    no longer waiting for a reply.

    So the fix removes the artificial trigger, and this test pins the
    invariant that makes it work. It fails against the pre-fix harness,
    where "stdin written + closed" is the FIRST timeline row.
    """

    def test_stdin_closes_only_after_the_expected_responses(
        self, sandboxed_project
    ) -> None:
        project, home = sandboxed_project
        inputs = (
            _mcp_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "order", "version": "0.0.1"},
                },
                req_id=1,
            )
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request("tools/list", req_id=3)
        )
        res = _spawn_codevira_mcp(project, home, inputs=inputs)

        # The REAL fd state at the moment the last expected response landed.
        # An earlier version of this test compared timeline row ORDER, and it
        # passed against the pre-fix harness too — a later close() emits the
        # "stdin closed" row regardless, so the ordering held while the actual
        # bug was still present. Assert on the descriptor, not on a log line.
        assert (
            res.stdin_open_when_answered is not None
        ), f"responses never all arrived\n{res.diagnostics()}"
        assert res.stdin_open_when_answered is True, (
            f"stdin was already CLOSED when the responses arrived — the SDK "
            f"tears the session down on EOF and an in-flight request loses its "
            f"reply\n{res.diagnostics()}"
        )

    def test_request_ids_are_derived_from_the_payload(self) -> None:
        """Deriving beats declaring: a call site that forgot to pass its ids
        would silently revert to closing stdin early."""
        inputs = (
            _mcp_request("initialize", {}, req_id=1)
            + _mcp_request("notifications/initialized", req_id=None)
            + _mcp_request("tools/list", req_id=3)
        )
        assert _request_ids(inputs) == {1, 3}, "notifications carry no id"

    def test_malformed_input_lines_are_skipped_not_fatal(self) -> None:
        """Some tests pipe junk on purpose; id-derivation must survive it."""
        assert _request_ids('not json\n{"jsonrpc":"2.0","id":7}\n\n') == {7}
