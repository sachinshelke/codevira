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
_mock_text_content = type(
    "TextContent",
    (),
    {
        "__init__": lambda self, **kw: self.__dict__.update(kw),
    },
)
_mock_tool = type(
    "Tool",
    (),
    {
        "__init__": lambda self, **kw: self.__dict__.update(kw),
    },
)
_mock_server_cls = MagicMock()
# The Server("codevira") call returns an object with decorators
_mock_server_instance = MagicMock()
_mock_server_cls.return_value = _mock_server_instance
# Make the decorators pass through (register nothing, return the original function).
# When new @server.<decorator>() handlers are added in mcp_server/server.py
# (e.g., Hero 8 added list_resources + read_resource), they MUST be added
# here too — otherwise the decorator returns a MagicMock that shadows the
# real async function, which breaks any later test that imports the
# module-level handler reference. (Symptom: "ValueError: a coroutine was
# expected, got <MagicMock ...>".)
_mock_server_instance.call_tool.return_value = lambda fn: fn
_mock_server_instance.list_tools.return_value = lambda fn: fn
_mock_server_instance.list_prompts.return_value = lambda fn: fn
_mock_server_instance.get_prompt.return_value = lambda fn: fn
_mock_server_instance.list_resources.return_value = lambda fn: fn
_mock_server_instance.read_resource.return_value = lambda fn: fn

sys.modules["mcp.server"].Server = _mock_server_cls
sys.modules["mcp.types"].Tool = _mock_tool
sys.modules["mcp.types"].TextContent = _mock_text_content
# Hero 8 added @server.list_resources() / @server.read_resource() handlers
# whose bodies do `from mcp.types import Resource` and `from pydantic
# import AnyUrl`. Stub Resource here for parity with Tool / TextContent.
# (When this list grows, the symptom is "ImportError: cannot import name
# X from mcp.types" during test_decision_replay collection.)
_mock_resource = type(
    "Resource",
    (),
    {
        "__init__": lambda self, **kw: self.__dict__.update(kw),
    },
)
sys.modules["mcp.types"].Resource = _mock_resource

# tree_sitter_language_pack stub: needs get_language, get_parser.
#
# Only stub when the real module ISN'T already loaded. test_server's tests
# don't actually call into tree-sitter (mcp_server/tools/code_reader uses
# it but test_server tests cover server.py main loop, not code_reader).
# Stubbing the real module's functions globally would corrupt
# test_treesitter_parser.py's tests that run later. Skip the stub when
# the real attributes already exist.
_ts_mod = sys.modules["tree_sitter_language_pack"]
if not hasattr(_ts_mod, "get_language"):
    _ts_mod.get_language = MagicMock(return_value=None)
if not hasattr(_ts_mod, "get_parser"):
    _ts_mod.get_parser = MagicMock(return_value=None)

# tree_sitter stub: needs Node class
_tree_sitter_mod = sys.modules["tree_sitter"]
if not hasattr(_tree_sitter_mod, "Node"):
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
        with patch(
            "mcp_server.server.get_roadmap", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        mock_fn.assert_called_once()
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_get_node(self):
        """get_node dispatches with the correct file_path argument."""
        sentinel = {"role": "API handler", "layer": "api"}
        with patch(
            "mcp_server.server.get_node", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_node", {"file_path": "src/api.py"}))
        mock_fn.assert_called_once_with("src/api.py", full=False)
        parsed = json.loads(result[0].text)
        assert parsed["role"] == "API handler"

    @pytest.mark.skip(
        reason="v2.2.0: tests deprecated feature (search_codebase / _check_search_deps / graph.db backend)"
    )
    def test_dispatch_search_codebase(self):
        """search_codebase dispatches with query and optional limit."""
        sentinel = {"query": "auth", "matches": []}
        with patch(
            "mcp_server.server.search_codebase", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("search_codebase", {"query": "auth", "limit": 3}))
        mock_fn.assert_called_once_with("auth", top_k=3, include_content=False)
        parsed = json.loads(result[0].text)
        assert parsed["query"] == "auth"

    @pytest.mark.skip(
        reason="v2.2.0: tests deprecated feature (search_codebase / _check_search_deps / graph.db backend)"
    )
    def test_dispatch_search_codebase_default_limit(self):
        """search_codebase uses default limit=5 when not provided."""
        sentinel = {"query": "db", "matches": []}
        with patch(
            "mcp_server.server.search_codebase", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("search_codebase", {"query": "db"}))
        mock_fn.assert_called_once_with("db", top_k=5, include_content=False)

    def test_dispatch_add_phase(self):
        """add_phase dispatches with all required and optional arguments."""
        sentinel = {"status": "added", "phase": 20}
        with patch(
            "mcp_server.server.add_phase", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "add_phase",
                    {
                        "phase": 20,
                        "name": "Refactor Auth",
                        "description": "Refactor auth module",
                        "priority": "high",
                        "files": ["src/auth.py"],
                        "effort": "~3 hours",
                    },
                )
            )
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
        with patch(
            "mcp_server.server.get_impact", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_impact", {"file_path": "src/core.py"}))
        mock_fn.assert_called_once_with("src/core.py", limit=10, summary_only=False)
        parsed = json.loads(result[0].text)
        assert parsed["blast_radius"] == 3

    def test_dispatch_write_session_log(self):
        """write_session_log dispatches with all required fields."""
        sentinel = {"status": "Session test-abc logged to SQLite Memory."}
        with patch(
            "mcp_server.server.write_session_log", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(
                call_tool(
                    "write_session_log",
                    {
                        "session_id": "test-abc",
                        "task": "Fix bug",
                        "phase": "3",
                        "files_changed": ["a.py"],
                        "decisions": [
                            {
                                "decision": "use retry",
                                "file_path": "a.py",
                                "context": "reliability",
                            }
                        ],
                        "next_steps": ["deploy"],
                    },
                )
            )
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
        with patch(
            "mcp_server.server.get_roadmap", side_effect=RuntimeError("db locked")
        ), patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert "error" in parsed
        assert "db locked" in parsed["error"]
        assert parsed["tool"] == "get_roadmap"

    def test_exception_does_not_crash_server(self):
        """call_tool always returns a list of TextContent, never raises."""
        with patch(
            "mcp_server.server.get_node", side_effect=FileNotFoundError("missing")
        ), patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_node", {"file_path": "nope.py"}))
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "error" in parsed

    def test_exception_in_tool_with_crash_logger(self):
        """When a tool raises and crash_logger is importable, log_crash is called."""
        with patch(
            "mcp_server.server.get_roadmap", side_effect=ValueError("bad")
        ), patch("mcp_server.auto_init.ensure_project_initialized"), patch(
            "mcp_server.crash_logger.log_crash"
        ) as mock_log:
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
        with patch("mcp_server.server.get_roadmap", return_value={"ok": True}), patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ) as mock_init:
            _run(call_tool("get_roadmap", {}))
        mock_init.assert_called_once()

    def test_auto_init_failure_does_not_block_dispatch(self):
        """If ensure_project_initialized raises, the tool still executes."""
        sentinel = {"phase": 1}
        with patch("mcp_server.server.get_roadmap", return_value=sentinel), patch(
            "mcp_server.auto_init.ensure_project_initialized",
            side_effect=RuntimeError("init boom"),
        ):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert parsed == sentinel


# ---------------------------------------------------------------------------
# Crash logger resilience
# ---------------------------------------------------------------------------


class TestCrashLoggerResilience:
    def test_crash_logger_failure_does_not_break_dispatch(self):
        """If crash_logger.log_crash itself fails, the error response still comes through."""
        with patch(
            "mcp_server.server.get_node", side_effect=RuntimeError("boom")
        ), patch("mcp_server.auto_init.ensure_project_initialized"), patch(
            "mcp_server.crash_logger.log_crash", side_effect=Exception("logger broken")
        ):
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
        with patch("mcp_server.server.get_roadmap", return_value=data), patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            result = _run(call_tool("get_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert parsed == data

    def test_result_is_text_content_type(self):
        """Result items have type='text'."""
        with patch("mcp_server.server.get_roadmap", return_value={}), patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            result = _run(call_tool("get_roadmap", {}))
        assert result[0].type == "text"

    def test_result_is_list_of_one_element(self):
        """call_tool always returns a single-element list."""
        with patch("mcp_server.server.get_roadmap", return_value={"a": 1}), patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            result = _run(call_tool("get_roadmap", {}))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Additional tool dispatch routes
# ---------------------------------------------------------------------------


class TestCallToolAdditionalRoutes:
    """Tests for tool dispatch routes not covered by TestCallToolDispatch."""

    def test_dispatch_list_nodes_no_filters(self):
        """list_nodes dispatches with default pagination."""
        sentinel = {"nodes": []}
        with patch(
            "mcp_server.server.list_nodes", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("list_nodes", {}))
        mock_fn.assert_called_once_with(
            layer=None, do_not_revert=None, stability=None, limit=50, offset=0
        )
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_list_nodes_with_filters(self):
        """list_nodes dispatches with layer and stability filters."""
        sentinel = {"nodes": ["a.py"]}
        with patch(
            "mcp_server.server.list_nodes", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "list_nodes",
                    {"layer": "api", "stability": "high", "do_not_revert": True},
                )
            )
        mock_fn.assert_called_once_with(
            layer="api", do_not_revert=True, stability="high", limit=50, offset=0
        )
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_complete_phase(self):
        """complete_phase dispatches with phase_number and key_decisions
        plus v2.1.2 backfill/completed_at/git_ref defaults."""
        sentinel = {"status": "completed", "phase": 5}
        with patch(
            "mcp_server.server.complete_phase", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "complete_phase",
                    {
                        "phase_number": 5,
                        "key_decisions": ["Used retry pattern"],
                    },
                )
            )
        mock_fn.assert_called_once_with(
            phase_number=5,
            key_decisions=["Used retry pattern"],
            backfill=False,
            completed_at=None,
            git_ref=None,
        )
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["status"] == "completed"

    def test_dispatch_update_phase_status(self):
        """update_phase_status dispatches with status and optional blocker."""
        sentinel = {"status": "in_progress"}
        with patch(
            "mcp_server.server.update_phase_status", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("update_phase_status", {"status": "in_progress"}))
        mock_fn.assert_called_once_with(
            status="in_progress", blocker=None, started=None
        )
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["status"] == "in_progress"

    def test_dispatch_update_phase_status_blocked(self):
        """update_phase_status passes blocker when status=blocked."""
        sentinel = {"status": "blocked"}
        with patch(
            "mcp_server.server.update_phase_status", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "update_phase_status",
                    {
                        "status": "blocked",
                        "blocker": "Waiting on API key",
                    },
                )
            )
        mock_fn.assert_called_once_with(
            status="blocked", blocker="Waiting on API key", started=None
        )
        assert len(result) == 1

    # v2.2.0+: get_preferences / get_learned_rules dispatch tests removed
    # (the tools were deleted along with the preferences + learned_rules
    # feature per the 2026-05-22 surface-cut audit).

    def test_dispatch_get_project_maturity(self):
        """get_project_maturity dispatches with no arguments."""
        sentinel = {"score": 72, "sessions": 15}
        with patch(
            "mcp_server.server.learning_get_project_maturity", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_project_maturity", {}))
        mock_fn.assert_called_once()
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["score"] == 72

    def test_dispatch_get_session_context(self):
        """get_session_context dispatches with no arguments."""
        sentinel = {"roadmap": {}, "rules": []}
        with patch(
            "mcp_server.server.learning_get_session_context", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_session_context", {}))
        mock_fn.assert_called_once()
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "roadmap" in parsed

    def test_dispatch_export_graph_default(self):
        """export_graph dispatches with default format."""
        sentinel = {"diagram": "graph LR ..."}
        with patch(
            "mcp_server.server.export_graph", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("export_graph", {}))
        mock_fn.assert_called_once_with(format="mermaid", scope=None)
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_export_graph_with_scope(self):
        """export_graph dispatches with format=dot and a scope."""
        sentinel = {"diagram": "digraph { ... }"}
        with patch(
            "mcp_server.server.export_graph", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool("export_graph", {"format": "dot", "scope": "src/services/"})
            )
        mock_fn.assert_called_once_with(format="dot", scope="src/services/")
        assert len(result) == 1

    # v2.2.0+: changeset dispatch tests removed (whole feature deleted —
    # never reached real usage per the 2026-05-22 audit).

    def test_dispatch_get_playbook(self):
        """get_playbook dispatches with task_type."""
        sentinel = {"rules": ["Always write tests first"]}
        with patch(
            "mcp_server.server.get_playbook", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_playbook", {"task_type": "add_route"}))
        mock_fn.assert_called_once_with("add_route")
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "rules" in parsed

    def test_dispatch_get_decision_confidence(self):
        """get_decision_confidence dispatches with file_path and pattern."""
        sentinel = {"confidence": 0.85}
        with patch(
            "mcp_server.server.learning_get_decision_confidence", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "get_decision_confidence",
                    {"file_path": "src/api.py", "pattern": "src/"},
                )
            )
        mock_fn.assert_called_once_with(file_path="src/api.py", pattern="src/")
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["confidence"] == 0.85

    def test_dispatch_get_decision_confidence_no_args(self):
        """get_decision_confidence dispatches with no arguments."""
        sentinel = {"confidence": 0.5}
        with patch(
            "mcp_server.server.learning_get_decision_confidence", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_decision_confidence", {}))
        mock_fn.assert_called_once_with(file_path=None, pattern=None)
        assert len(result) == 1

    def test_dispatch_refresh_index(self):
        """refresh_index dispatches with file_paths."""
        sentinel = {"reindexed": 3}
        with patch(
            "mcp_server.server.refresh_index", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("refresh_index", {"file_paths": ["a.py", "b.py"]}))
        mock_fn.assert_called_once_with(file_paths=["a.py", "b.py"])
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["reindexed"] == 3

    def test_dispatch_refresh_index_no_args(self):
        """refresh_index dispatches with empty list when no file_paths provided."""
        sentinel = {"reindexed": 0}
        with patch(
            "mcp_server.server.refresh_index", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("refresh_index", {}))
        mock_fn.assert_called_once_with(file_paths=[])
        assert len(result) == 1

    def test_dispatch_refresh_graph(self):
        """refresh_graph dispatches with file_paths."""
        sentinel = {"generated": 2}
        with patch(
            "mcp_server.server.refresh_graph", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("refresh_graph", {"file_paths": ["new.py"]}))
        mock_fn.assert_called_once_with(file_paths=["new.py"])
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["generated"] == 2

    def test_dispatch_refresh_graph_no_args(self):
        """refresh_graph dispatches with None when no file_paths provided."""
        sentinel = {"generated": 5}
        with patch(
            "mcp_server.server.refresh_graph", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("refresh_graph", {}))
        mock_fn.assert_called_once_with(file_paths=None)
        assert len(result) == 1

    def test_dispatch_get_history(self):
        """get_history dispatches with file_path (+ v2.1.2 Item 25 since=)."""
        sentinel = {"commits": [{"hash": "abc123"}]}
        with patch(
            "mcp_server.server.get_history", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(call_tool("get_history", {"file_path": "src/core.py"}))
        mock_fn.assert_called_once_with("src/core.py", limit=5, full=False, since=None)
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["commits"][0]["hash"] == "abc123"

    def test_dispatch_update_next_action(self):
        """update_next_action dispatches with next_action string."""
        sentinel = {"status": "updated"}
        with patch(
            "mcp_server.server.update_next_action", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool("update_next_action", {"next_action": "Deploy to staging"})
            )
        mock_fn.assert_called_once_with("Deploy to staging")
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["status"] == "updated"

    def test_dispatch_defer_phase(self):
        """defer_phase dispatches with phase_number and reason."""
        sentinel = {"status": "deferred", "phase": 12}
        with patch(
            "mcp_server.server.defer_phase", return_value=sentinel
        ) as mock_fn, patch("mcp_server.auto_init.ensure_project_initialized"):
            result = _run(
                call_tool(
                    "defer_phase",
                    {
                        "phase_number": 12,
                        "reason": "Blocked by API redesign",
                    },
                )
            )
        mock_fn.assert_called_once_with(
            phase_number=12, reason="Blocked by API redesign"
        )
        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["status"] == "deferred"


# ---------------------------------------------------------------------------
# Missing dispatch coverage (lines 800, 802, 855, 866, 879, 904, 906, 914,
# 937, 943, 948)
# ---------------------------------------------------------------------------


class TestCallToolMissingDispatches:
    def test_dispatch_get_full_roadmap(self):
        sentinel = {"phases": []}
        with patch("mcp_server.server.get_full_roadmap", return_value=sentinel), patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            result = _run(call_tool("get_full_roadmap", {}))
        parsed = json.loads(result[0].text)
        assert parsed == sentinel

    def test_dispatch_update_phase_status(self):
        sentinel = {"status": "updated"}
        with patch(
            "mcp_server.server.update_phase_status", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("update_phase_status", {"status": "in_progress"}))
        m.assert_called_once_with(status="in_progress", blocker=None, started=None)

    def test_dispatch_defer_phase(self):
        sentinel = {"deferred": True}
        with patch("mcp_server.server.defer_phase", return_value=sentinel) as m, patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            _run(call_tool("defer_phase", {"phase_number": 3, "reason": "blocked"}))
        m.assert_called_once_with(phase_number=3, reason="blocked")

    def test_dispatch_complete_phase(self):
        sentinel = {"completed": True}
        with patch(
            "mcp_server.server.complete_phase", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(
                call_tool(
                    "complete_phase",
                    {"phase_number": 2, "key_decisions": ["Used REST"]},
                )
            )
        m.assert_called_once_with(
            phase_number=2,
            key_decisions=["Used REST"],
            backfill=False,
            completed_at=None,
            git_ref=None,
        )

    def test_dispatch_get_phase(self):
        sentinel = {"phase": 1, "name": "Setup"}
        with patch("mcp_server.server.get_phase", return_value=sentinel) as m, patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            _run(call_tool("get_phase", {"phase_number": 1}))
        m.assert_called_once_with(1)

    def test_dispatch_refresh_graph(self):
        sentinel = {"refreshed": True}
        with patch(
            "mcp_server.server.refresh_graph", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("refresh_graph", {"file_paths": ["src/x.py"]}))
        m.assert_called_once_with(file_paths=["src/x.py"])

    def test_dispatch_get_signature(self):
        sentinel = {"symbols": []}
        with patch(
            "mcp_server.server.get_signature", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("get_signature", {"file_path": "src/api.py"}))
        m.assert_called_once_with("src/api.py")

    def test_dispatch_get_code(self):
        sentinel = {"source": "def foo(): pass"}
        with patch("mcp_server.server.get_code", return_value=sentinel) as m, patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            _run(call_tool("get_code", {"file_path": "src/api.py", "symbol": "foo"}))
        m.assert_called_once_with("src/api.py", symbol="foo")

    def test_dispatch_export_graph(self):
        sentinel = {"mermaid": "graph LR"}
        with patch("mcp_server.server.export_graph", return_value=sentinel) as m, patch(
            "mcp_server.auto_init.ensure_project_initialized"
        ):
            _run(call_tool("export_graph", {"format": "mermaid"}))
        m.assert_called_once_with(format="mermaid", scope=None)

    def test_dispatch_get_graph_diff(self):
        sentinel = {"diff": []}
        with patch(
            "mcp_server.server.get_graph_diff", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("get_graph_diff", {}))
        m.assert_called_once_with(base_ref="main", head_ref="HEAD")

    def test_dispatch_get_decision_confidence(self):
        sentinel = {"confidence": 0.9}
        with patch(
            "mcp_server.server.learning_get_decision_confidence", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("get_decision_confidence", {"file_path": "src/api.py"}))
        m.assert_called_once_with(file_path="src/api.py", pattern=None)

    # v2.2.0+: get_preferences / get_learned_rules removed (see above).

    def test_dispatch_get_project_maturity(self):
        sentinel = {"maturity": "Senior"}
        with patch(
            "mcp_server.server.learning_get_project_maturity", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("get_project_maturity", {}))
        m.assert_called_once()

    def test_dispatch_get_session_context(self):
        sentinel = {"context": {}}
        with patch(
            "mcp_server.server.learning_get_session_context", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("get_session_context", {}))
        m.assert_called_once()

    def test_dispatch_query_graph(self):
        sentinel = {"callees": []}
        with patch(
            "mcp_server.server.query_graph_tool", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("query_graph", {"file_path": "src/api.py"}))
        m.assert_called_once_with(
            file_path="src/api.py", symbol=None, query_type="callees"
        )

    def test_dispatch_analyze_changes(self):
        sentinel = {"changes": []}
        with patch(
            "mcp_server.server.analyze_changes_tool", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("analyze_changes", {}))
        m.assert_called_once_with(base_ref="main", head_ref="HEAD")

    def test_dispatch_find_hotspots(self):
        sentinel = {"hotspots": []}
        with patch(
            "mcp_server.server.find_hotspots_tool", return_value=sentinel
        ) as m, patch("mcp_server.auto_init.ensure_project_initialized"):
            _run(call_tool("find_hotspots", {}))
        m.assert_called_once_with(threshold=50)


# ---------------------------------------------------------------------------
# main() — lines 968-1045
# ---------------------------------------------------------------------------


class TestServerMain:
    def test_main_installs_crash_handler(self):
        with patch(
            "mcp_server.crash_logger.install_global_handler"
        ) as mock_handler, patch("asyncio.run"), patch(
            "indexer.index_codebase.start_background_watcher", return_value=MagicMock()
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()
        mock_handler.assert_called_once()

    def test_main_crash_handler_exception_does_not_crash(self):
        """If crash handler install fails, main() continues."""
        with patch(
            "mcp_server.crash_logger.install_global_handler",
            side_effect=RuntimeError("boom"),
        ), patch("asyncio.run"), patch(
            "indexer.index_codebase.start_background_watcher", return_value=MagicMock()
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()  # Must not raise

    def test_main_migration_called_when_needed(self):
        mock_watcher = MagicMock()
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=mock_watcher
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch(
            "mcp_server.migrate.detect_migration_needed", return_value=True
        ), patch(
            "mcp_server.migrate.migrate_to_centralized",
            return_value={"migrated": True, "files_copied": 5, "new_path": "/tmp/x"},
        ) as mock_migrate:
            from mcp_server.server import main

            main()
        mock_migrate.assert_called_once()

    def test_main_migration_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=MagicMock()
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch(
            "mcp_server.migrate.detect_migration_needed",
            side_effect=RuntimeError("migrate fail"),
        ):
            from mcp_server.server import main

            main()  # Must not raise

    def test_main_watcher_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher",
            side_effect=ImportError("watchdog not found"),
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()  # Must not raise

    def test_main_learning_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=MagicMock()
        ), patch(
            "indexer.outcome_tracker.analyze_session_outcomes",
            side_effect=RuntimeError("learning fail"),
        ), patch("indexer.rule_learner.run_rule_inference"), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()  # Must not raise

    def test_main_global_sync_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=MagicMock()
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project",
            side_effect=RuntimeError("sync fail"),
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()  # Must not raise

    def test_main_stops_watcher_in_finally(self):
        mock_watcher = MagicMock()
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=mock_watcher
        ), patch("indexer.outcome_tracker.analyze_session_outcomes"), patch(
            "indexer.rule_learner.run_rule_inference"
        ), patch(
            "mcp_server.global_sync.import_global_to_project", return_value={}
        ), patch("mcp_server.migrate.detect_migration_needed", return_value=False):
            from mcp_server.server import main

            main()
        mock_watcher.stop.assert_called_once()

    # v1.8.1 hardening — server.main() refuses $HOME / system dirs.
    # This is the LAST-MILE guard for users who upgrade from v1.8.0 with a
    # leftover rogue project. Without it, even with all upstream guards,
    # `start_background_watcher` would still fire from the rogue config.yaml
    # and walk ~/Library/... — which is the actual production crash mode.
    def test_main_refuses_home_root(self, tmp_path, monkeypatch, capsys):
        import pytest as _pytest

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        monkeypatch.setattr("mcp_server.paths.get_project_root", lambda: fake_home)

        # The watcher MUST NOT be invoked when the guard fires.
        mock_watcher = MagicMock()
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ) as mock_asyncio, patch(
            "indexer.index_codebase.start_background_watcher", return_value=mock_watcher
        ) as mock_start_watcher, patch(
            "mcp_server.migrate.detect_migration_needed", return_value=False
        ), _pytest.raises(SystemExit) as exc:
            from mcp_server.server import main

            main()

        assert exc.value.code == 1
        # Watcher and asyncio loop never reached.
        mock_start_watcher.assert_not_called()
        mock_asyncio.assert_not_called()
        err = capsys.readouterr().err
        assert "$HOME" in err
        assert "clean --orphans" in err

    def test_main_refuses_root_slash(self, monkeypatch, capsys):
        import pytest as _pytest
        from pathlib import Path

        monkeypatch.setattr("mcp_server.paths.get_project_root", lambda: Path("/"))

        mock_watcher = MagicMock()
        with patch("mcp_server.crash_logger.install_global_handler"), patch(
            "asyncio.run"
        ), patch(
            "indexer.index_codebase.start_background_watcher", return_value=mock_watcher
        ) as mock_start_watcher, patch(
            "mcp_server.migrate.detect_migration_needed", return_value=False
        ), _pytest.raises(SystemExit) as exc:
            from mcp_server.server import main

            main()

        assert exc.value.code == 1
        mock_start_watcher.assert_not_called()


# ===========================================================================
# v2.0-rc.2 — Bug 1 (update_node description discoverability) regression
#
# The other Claude Code session in dogfood explicitly missed do_not_revert
# because it lived only in inputSchema.changes.description, not the outer
# tool description. Symptom: "None of the Codevira write tools accept a
# do_not_revert flag" — refused to mark Sachin's Postgres decision protected.
#
# This contract test pins the description so a future "tighten the wording"
# pass doesn't accidentally drop the visibility of `do_not_revert`.
# ===========================================================================


class TestUpdateNodeDescriptionContract:
    def test_update_node_description_mentions_do_not_revert(self):
        """update_node's outer description must explicitly mention
        do_not_revert. Regression of Bug 1: the field was buried in the
        inner schema and AIs missed it during tool discovery."""
        from mcp_server.server import list_tools

        tools = _run(list_tools())
        update_node = next(t for t in tools if t.name == "update_node")
        assert "do_not_revert" in update_node.description, (
            f"Bug 1 regression: update_node description does not mention "
            f"`do_not_revert`. AIs scanning tool surfaces will miss the "
            f"protection mechanism. Description: {update_node.description!r}"
        )

    def test_update_node_inner_changes_description_mentions_do_not_revert(self):
        """The inputSchema.changes.description should ALSO mention
        do_not_revert (defense in depth — different AIs read different
        levels of the schema)."""
        from mcp_server.server import list_tools

        tools = _run(list_tools())
        update_node = next(t for t in tools if t.name == "update_node")
        changes_desc = update_node.inputSchema["properties"]["changes"].get(
            "description", ""
        )
        assert "do_not_revert" in changes_desc
