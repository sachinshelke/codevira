"""
Tests for mcp_server/server.py -- call_tool dispatch, error handling, and serialization.

Covers:
  - call_tool dispatches known tools correctly (get_roadmap, get_node, search_codebase, add_phase)
  - call_tool returns error dict for unknown tool names
  - call_tool catches exceptions and returns structured error
  - call_tool runs ensure_project_initialized before dispatch
  - crash logger failure does not break dispatch
  - dict result is serialized as JSON in TextContent
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pre-seed sys.modules with mock mcp package so mcp_server.server can import
# without the real mcp package installed.  This must happen before any
# mcp_server.server import.
# ---------------------------------------------------------------------------

_modules_to_mock = [
    # mcp package (not installed in test env)
    "mcp",
    "mcp.server",
    "mcp.server.stdio",
    "mcp.server.streamable_http_manager",
    "mcp.types",
    # tree-sitter (optional, not installed in test env)
    "tree_sitter_language_pack",
    "tree_sitter",
]

_mock_mods_installed: dict[str, types.ModuleType] = {}
for _mod_name in _modules_to_mock:
    if _mod_name not in sys.modules:
        _m = types.ModuleType(_mod_name)
        sys.modules[_mod_name] = _m
        _mock_mods_installed[_mod_name] = _m

# Provide the symbols that server.py expects at module level:
#   from mcp.server import Server
#   from mcp.types import Tool, TextContent
_mock_text_content = type("TextContent", (), {
    "__init__": lambda self, **kw: self.__dict__.update(kw),
})
_mock_tool = type("Tool", (), {
    "__init__": lambda self, **kw: self.__dict__.update(kw),
})
_mock_server_cls = MagicMock()
# The Server("codevira") call returns an object with decorators
_mock_server_instance = MagicMock()
_mock_server_cls.return_value = _mock_server_instance
# Make the decorators pass through (register nothing, return the original function)
_mock_server_instance.call_tool.return_value = lambda fn: fn
_mock_server_instance.list_tools.return_value = lambda fn: fn
_mock_server_instance.list_prompts.return_value = lambda fn: fn
_mock_server_instance.get_prompt.return_value = lambda fn: fn

sys.modules["mcp.server"].Server = _mock_server_cls
sys.modules["mcp.types"].Tool = _mock_tool
sys.modules["mcp.types"].TextContent = _mock_text_content

# tree_sitter_language_pack stub: needs get_language, get_parser
_ts_mod = sys.modules["tree_sitter_language_pack"]
_ts_mod.get_language = MagicMock(return_value=None)
_ts_mod.get_parser = MagicMock(return_value=None)

# tree_sitter stub: needs Node class
_tree_sitter_mod = sys.modules["tree_sitter"]
_tree_sitter_mod.Node = type("Node", (), {})

# Now we can safely import
from mcp_server.server import call_tool  # noqa: E402


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Dispatch to known tools
# ---------------------------------------------------------------------------

class TestCallToolDispatch:
    def test_dispatch_get_roadmap(self):
        """get_roadmap dispatches correctly with no arguments."""
        sentinel = {"phase": 5, "name": "Test Phase"}
        with patch("mcp_server.server.get_roadmap", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        mock_fn.assert_called_once()
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_get_node(self):
        """get_node dispatches with the correct file_path argument."""
        sentinel = {"role": "API handler", "layer": "api"}
        with patch("mcp_server.server.get_node", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_node", {"file_path": "src/api.py"}))
        mock_fn.assert_called_once_with("src/api.py")
        parsed = json.loads(result[0].text)
        assert parsed["role"] == "API handler"

    def test_dispatch_search_codebase(self):
        """search_codebase dispatches with query and optional limit."""
        sentinel = {"query": "auth", "matches": []}
        with patch("mcp_server.server.search_codebase", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("search_codebase", {"query": "auth", "limit": 3}))
        mock_fn.assert_called_once_with("auth", top_k=3)
        parsed = json.loads(result[0].text)
        assert parsed["query"] == "auth"

    def test_dispatch_search_codebase_default_limit(self):
        """search_codebase uses default limit=5 when not provided."""
        sentinel = {"query": "db", "matches": []}
        with patch("mcp_server.server.search_codebase", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("search_codebase", {"query": "db"}))
        mock_fn.assert_called_once_with("db", top_k=5)

    def test_dispatch_add_phase(self):
        """add_phase dispatches with all required and optional arguments."""
        sentinel = {"status": "added", "phase": 20}
        with patch("mcp_server.server.add_phase", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("add_phase", {
                "phase": 20,
                "name": "Refactor Auth",
                "description": "Refactor auth module",
                "priority": "high",
                "files": ["src/auth.py"],
                "effort": "~3 hours",
            }))
        mock_fn.assert_called_once_with(
            phase=20,
            name="Refactor Auth",
            description="Refactor auth module",
            priority="high",
            depends_on=None,
            files=["src/auth.py"],
            effort="~3 hours",
        )
        parsed = json.loads(result[0].text)
        assert parsed["status"] == "added"

    def test_dispatch_get_impact(self):
        """get_impact dispatches with file_path."""
        sentinel = {"file": "src/core.py", "blast_radius": 3}
        with patch("mcp_server.server.get_impact", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_impact", {"file_path": "src/core.py"}))
        mock_fn.assert_called_once_with("src/core.py")
        parsed = json.loads(result[0].text)
        assert parsed["blast_radius"] == 3

    def test_dispatch_write_session_log(self):
        """write_session_log dispatches with all required fields."""
        sentinel = {"status": "Session test-abc logged to SQLite Memory."}
        with patch("mcp_server.server.write_session_log", return_value=sentinel) as mock_fn, \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("write_session_log", {
                "session_id": "test-abc",
                "task": "Fix bug",
                "phase": "3",
                "files_changed": ["a.py"],
                "decisions": [{"decision": "use retry", "file_path": "a.py", "context": "reliability"}],
                "next_steps": ["deploy"],
            }))
        mock_fn.assert_called_once()
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["session_id"] == "test-abc"
        assert call_kwargs["task"] == "Fix bug"


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------

class TestCallToolUnknown:
    def test_unknown_tool_returns_error(self):
        """Calling a nonexistent tool returns an error dict."""
        with patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("nonexistent_tool", {}))
        parsed = json.loads(result[0].text)
        assert "error" in parsed
        assert "nonexistent_tool" in parsed["error"]

    def test_unknown_tool_still_returns_text_content(self):
        """Even error results are wrapped in TextContent."""
        with patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("no_such_thing", {"arg": 1}))
        assert len(result) == 1
        assert result[0].type == "text"


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

class TestCallToolExceptionHandling:
    def test_exception_returns_structured_error(self):
        """When a tool raises an exception, call_tool returns an error dict with tool name."""
        with patch("mcp_server.server.get_roadmap", side_effect=RuntimeError("db locked")), \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert "error" in parsed
        assert "db locked" in parsed["error"]
        assert parsed["tool"] == "get_roadmap"

    def test_exception_does_not_crash_server(self):
        """call_tool always returns a list of TextContent, never raises."""
        with patch("mcp_server.server.get_node", side_effect=FileNotFoundError("missing")), \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_node", {"file_path": "nope.py"}))
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "error" in parsed

    def test_exception_in_tool_with_crash_logger(self):
        """When a tool raises and crash_logger is importable, log_crash is called."""
        with patch("mcp_server.server.get_roadmap", side_effect=ValueError("bad")), \
             patch("mcp_server.auto_init.ensure_project_initialized"), \
             patch("mcp_server.crash_logger.log_crash") as mock_log:
            result = _run(call_tool("get_roadmap", {}))
        assert mock_log.called
        parsed = json.loads(result[0].text)
        assert parsed["error"] == "bad"


# ---------------------------------------------------------------------------
# ensure_project_initialized
# ---------------------------------------------------------------------------

class TestCallToolAutoInit:
    def test_ensure_project_initialized_called(self):
        """ensure_project_initialized is called before tool dispatch."""
        with patch("mcp_server.server.get_roadmap", return_value={"ok": True}), \
             patch("mcp_server.auto_init.ensure_project_initialized") as mock_init:
            _run(call_tool("get_roadmap", {}))
        mock_init.assert_called_once()

    def test_auto_init_failure_does_not_block_dispatch(self):
        """If ensure_project_initialized raises, the tool still executes."""
        sentinel = {"phase": 1}
        with patch("mcp_server.server.get_roadmap", return_value=sentinel), \
             patch("mcp_server.auto_init.ensure_project_initialized", side_effect=RuntimeError("init boom")):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert parsed == sentinel


# ---------------------------------------------------------------------------
# Crash logger resilience
# ---------------------------------------------------------------------------

class TestCrashLoggerResilience:
    def test_crash_logger_failure_does_not_break_dispatch(self):
        """If crash_logger.log_crash itself fails, the error response still comes through."""
        with patch("mcp_server.server.get_node", side_effect=RuntimeError("boom")), \
             patch("mcp_server.auto_init.ensure_project_initialized"), \
             patch("mcp_server.crash_logger.log_crash", side_effect=Exception("logger broken")):
            result = _run(call_tool("get_node", {"file_path": "x.py"}))
        parsed = json.loads(result[0].text)
        assert "error" in parsed
        assert "boom" in parsed["error"]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestCallToolSerialization:
    def test_dict_result_serialized_as_json(self):
        """Tool returning a dict gets serialized to JSON in TextContent."""
        data = {"key": "value", "nested": {"a": [1, 2, 3]}}
        with patch("mcp_server.server.get_roadmap", return_value=data), \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert parsed == data

    def test_result_is_text_content_type(self):
        """Result items have type='text'."""
        with patch("mcp_server.server.get_roadmap", return_value={}), \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        assert result[0].type == "text"

    def test_result_is_list_of_one_element(self):
        """call_tool always returns a single-element list."""
        with patch("mcp_server.server.get_roadmap", return_value={"a": 1}), \
             patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        assert len(result) == 1
