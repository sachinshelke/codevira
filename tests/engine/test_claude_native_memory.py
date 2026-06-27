"""Test that Claude Code's native PostToolUse correctly populates memory fanout."""

from __future__ import annotations

import json
import pytest

from mcp_server.engine import memory_fanout
from mcp_server.engine.wiring import claude_code_hooks
import io
import sys


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Reset fanout buffer
    memory_fanout.reset_buffer()

    # Mock environment
    monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)

    # Mock stdout/stdin
    def _run_handler(event_name: str, raw_input: dict) -> tuple[int, str, str]:
        stdin_buf = io.StringIO(json.dumps(raw_input))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)
        rc = claude_code_hooks.handle(event_name)
        return rc, stdout_buf.getvalue(), stderr_buf.getvalue()

    return _run_handler, tmp_path


def test_native_edit_triggers_memory_fanout(_isolate):
    """Proves that a native Claude Code Edit tool (via PostToolUse) reaches memory_fanout."""
    _run_handler, tmp_path = _isolate
    proj = tmp_path / "proj"
    proj.mkdir()

    # Buffer should be initially empty
    assert memory_fanout.buffer_size() == 0

    # Emit a native Edit tool post-use event.
    rc, stdout, stderr = _run_handler(
        "PostToolUse",
        {
            "session_id": "session-123",
            "cwd": str(proj),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(proj / "test_file.py")},
            "tool_result": "Success",
        },
    )

    # The event should have populated the memory_fanout buffer
    assert rc == 0
    assert memory_fanout.buffer_size() == 1

    # Buffer is flushed automatically on size 20 or interpreter exit.
    # To test durability, we explicitly trigger the flush mechanism and read the persistent store.
    memory_fanout.flush()
    assert memory_fanout.buffer_size() == 0

    from mcp_server.tools.working import get_working_context

    # Assert durability via the actual store the MCP tool reads from
    working_data = get_working_context(top_k=5)
    working_str = str(working_data)
    assert "Edit: touched" in working_str
    assert "test_file.py" in working_str
