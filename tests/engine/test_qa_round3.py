"""Round-3 QA: regression tests for two P1s and one P2 found in round 3.

Round-3 Findings:
  P1 #1 — mcp_dispatch.pre_call/post_call adapters were never invoked
          from mcp_server.server.call_tool. Acceptance criterion claimed
          "demo policy works through MCP dispatch wiring" but the
          integration into the actual MCP server was missing. Fixed by
          wiring pre_call before dispatch + post_call after.
  P1 #2 — Real hook latency (process spawn + Python startup + import) is
          ~100ms, 3× over the 50ms spec target. Added shell-script fast
          path: when CODEVIRA_ENGINE=0, skip the entire Python invocation.
  P2 #1 — Spec said diff > 10MB bail; code bails at 100KB. Documentation
          drift; addressed by updating spec to match code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# =====================================================================
# Round-3 P1 #1: MCP dispatch IS wired into call_tool
# =====================================================================

class TestMCPCallToolWiresEngine:
    """The MCP server's call_tool must invoke pre_call before dispatching
    and post_call after. Without this, the engine is a no-op for MCP
    tool calls — heroes that depend on PRE_TOOL_USE/POST_TOOL_USE would
    silently never fire on MCP-side tool invocations.
    """

    def test_call_tool_source_imports_pre_call(self):
        """Static check: server.call_tool must import pre_call."""
        server_src = (Path(__file__).parent.parent.parent
                      / "mcp_server" / "server.py").read_text()
        assert "from mcp_server.engine.wiring.mcp_dispatch import pre_call" in server_src
        # And must invoke it inside call_tool, not just import
        # (the import lives inside the call_tool function body)
        assert "pre_call(name, arguments)" in server_src

    def test_call_tool_source_imports_post_call(self):
        """Static check: server.call_tool must import + invoke post_call."""
        server_src = (Path(__file__).parent.parent.parent
                      / "mcp_server" / "server.py").read_text()
        assert "from mcp_server.engine.wiring.mcp_dispatch import post_call" in server_src
        assert "post_call(name, arguments, result)" in server_src

    def test_call_tool_blocks_when_engine_returns_block(self):
        """Behavioral check: a registered policy that blocks PRE_TOOL_USE
        causes call_tool to return the block message instead of
        dispatching to the tool implementation."""
        from mcp_server.engine import (
            EventType, Policy, PolicyVerdict, register_policy, reset_policies,
        )

        class HardBlocker(Policy):
            name = "test_hard_blocker"
            handles = (EventType.PRE_TOOL_USE,)
            def evaluate(self, event):
                return PolicyVerdict.block("blocked by test policy")

        reset_policies()
        register_policy(HardBlocker())
        try:
            # Import after registration so call_tool sees our policy.
            # We can't easily call the async call_tool directly without
            # an event loop, so we verify the behavior by directly testing
            # the wiring layer with the same input call_tool would build.
            from mcp_server.engine.wiring.mcp_dispatch import pre_call
            verdict = pre_call("get_node", {"file_path": "src/foo.py"})
            assert verdict.is_blocking()
            assert "blocked by test policy" in (verdict.message or "")
        finally:
            reset_policies()

    def test_engine_failure_does_not_break_call_tool(self):
        """If the engine wiring raises, call_tool continues normally.
        Static check: the engine wiring is wrapped in try/except in
        server.py so a buggy policy can never break tool dispatch.

        We verify by walking lines: between the pre_call line and the
        nearest preceding non-blank statement, there must be a `try:`
        at a smaller indent. (Week-4 R2 added register_default_policies()
        between the try: and pre_call, so a fixed 200-byte window was
        too tight.)
        """
        server_src = (Path(__file__).parent.parent.parent
                      / "mcp_server" / "server.py").read_text()
        lines = server_src.splitlines()
        # Find the line with the pre_call invocation
        pre_idx = next(
            (i for i, l in enumerate(lines) if "pre_call(name, arguments)" in l),
            -1,
        )
        assert pre_idx > 0, "pre_call site not found"
        # Walk backwards looking for `try:` at a strictly smaller indent.
        pre_line = lines[pre_idx]
        pre_indent = len(pre_line) - len(pre_line.lstrip())
        found_try = False
        for j in range(pre_idx - 1, max(0, pre_idx - 50), -1):
            line = lines[j]
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if indent < pre_indent and stripped.startswith("try"):
                found_try = True
                break
            if indent < pre_indent:  # different control structure — stop
                break
        assert found_try, (
            "pre_call(name, arguments) must be inside a try/except"
        )


# =====================================================================
# Round-3 P1 #2: Shell-script fast path skips Python on CODEVIRA_ENGINE=0
# =====================================================================

class TestHookFastPath:
    """The 5 hook shell scripts must short-circuit when the engine is
    explicitly disabled, saving ~100ms of Python startup per hook fire.
    """

    HOOK_DIR = (Path(__file__).parent.parent.parent
                / "mcp_server" / "data" / "hooks")

    HOOKS = [
        "pre_tool_use.sh",
        "post_tool_use.sh",
        "session_start.sh",
        "user_prompt_submit.sh",
        "stop.sh",
    ]

    def test_all_hooks_have_fast_path(self):
        """Each of the 5 hook scripts must check CODEVIRA_ENGINE=0
        before doing anything else."""
        for hook in self.HOOKS:
            path = self.HOOK_DIR / hook
            assert path.exists(), f"Hook script missing: {hook}"
            content = path.read_text()
            assert 'CODEVIRA_ENGINE' in content, f"{hook} missing fast path"
            assert '"0"' in content, f"{hook} missing 0 check"

    @staticmethod
    def _bash_path() -> str:
        """Find bash. macOS has it at /bin/bash; Linux usually /bin/bash too."""
        import shutil
        return shutil.which("bash") or "/bin/bash"

    def _hook_env(self) -> dict[str, str]:
        """Build a minimal env that includes a working PATH for bash + codevira."""
        import os
        # Inherit a real PATH (so bash, etc., can be found) but force
        # CODEVIRA_ENGINE=0 to take the fast path.
        env = dict(os.environ)
        env["CODEVIRA_ENGINE"] = "0"
        return env

    def test_fast_path_short_circuits_to_continue_true(self):
        """When CODEVIRA_ENGINE=0, the fast path prints {"continue": true}
        and exits 0 — without invoking codevira at all."""
        hook = self.HOOK_DIR / "pre_tool_use.sh"
        result = subprocess.run(
            [self._bash_path(), str(hook)],
            input="",
            capture_output=True,
            text=True,
            env=self._hook_env(),
            timeout=5,
        )
        assert result.returncode == 0, (
            f"hook returned {result.returncode}; stderr: {result.stderr}"
        )
        payload = json.loads(result.stdout.strip())
        assert payload["continue"] is True

    def test_fast_path_runs_in_under_100ms(self):
        """The fast path skips Python's import. Should complete well
        under the full-stack 135ms p95 we measured WITHOUT the fast path.
        Bound at 100ms — generous to allow for CI variance, but tight
        enough to catch a regression where the Python invocation crept
        back in."""
        import time
        hook = self.HOOK_DIR / "pre_tool_use.sh"
        env = self._hook_env()
        bash = self._bash_path()
        # Warmup
        subprocess.run([bash, str(hook)], input="", capture_output=True,
                       env=env, timeout=5)
        # Measure 10 runs
        elapsed_samples = []
        for _ in range(10):
            t0 = time.perf_counter()
            subprocess.run([bash, str(hook)], input="", capture_output=True,
                           env=env, timeout=5)
            elapsed_samples.append((time.perf_counter() - t0) * 1000)
        median = sorted(elapsed_samples)[len(elapsed_samples) // 2]
        # Fast path should be MUCH faster than the full Python stack
        # (which we measured at ~100ms p50). 100ms is the upper bound;
        # if fast path takes >100ms, Python is being invoked somewhere
        # it shouldn't be.
        assert median < 100, (
            f"Fast path took {median:.1f}ms (expected <100ms — "
            f"Python startup may have crept in)"
        )


# =====================================================================
# Round-3 P2 #1: doc drift on size cap (informational test)
# =====================================================================

class TestSizeCapDocsMatch:
    """The spec said 10 MB cap; code says 100 KB. Verify they now agree."""

    def test_code_size_cap_value(self):
        from indexer.fix_history import _MAX_CHANGE_BYTES
        assert _MAX_CHANGE_BYTES == 100_000

    def test_spec_documents_actual_value(self):
        spec = (Path(__file__).parent.parent.parent
                / "docs" / "heroes" / "00-engine.md").read_text()
        # After spec update, must mention either 100 KB or 100,000 bytes.
        # If still says 10 MB, doc drift remains.
        has_correct = (
            "100 KB" in spec or "100KB" in spec
            or "100_000" in spec or "100,000" in spec
        )
        assert has_correct, (
            "docs/heroes/00-engine.md still references the wrong size cap. "
            "Either update spec to match code (100 KB) or update code to "
            "match spec (10 MB)."
        )
