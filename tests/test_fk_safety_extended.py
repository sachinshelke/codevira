"""Extended FK-safety tests — Bugs 13, 14, 15 (rc.5).

Bug 9 (rc.4) fixed ``add_call_edge``. Re-audit found two more inserts
with the same race shape and one outcome path with similar risk:

  - Bug 13: ``add_edge`` — FK on nodes(id) for both source_id + target_id
  - Bug 14: ``add_symbol`` — FK on nodes(id) via file_node_id
  - Bug 15: ``record_outcome`` — FK on sessions(session_id)

All three fixes follow the same pattern: ``WHERE EXISTS`` subqueries
that silently drop rows referencing missing parents instead of raising
``IntegrityError`` and crashing the watcher / engine.
"""
from __future__ import annotations

import pytest

from indexer.sqlite_graph import SQLiteGraph


# ---------------------------------------------------------------------------
# Bug 13 — add_edge FK race
# ---------------------------------------------------------------------------


class TestAddEdgeFKSafety:
    def _setup(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="api")
        db.add_node("file:src/b.py", "file", "b.py", "src/b.py", layer="api")
        return db

    def test_inserts_when_both_nodes_exist(self, tmp_path):
        db = self._setup(tmp_path)
        try:
            db.add_edge("file:src/a.py", "file:src/b.py", kind="imports")
            rows = db.conn.execute("SELECT * FROM edges").fetchall()
            assert len(rows) == 1
        finally:
            db.close()

    def test_source_missing_does_not_raise(self, tmp_path):
        db = self._setup(tmp_path)
        try:
            db.add_edge("file:ghost.py", "file:src/b.py", kind="imports")
            rows = db.conn.execute("SELECT * FROM edges").fetchall()
            assert len(rows) == 0
        finally:
            db.close()

    def test_target_missing_does_not_raise(self, tmp_path):
        db = self._setup(tmp_path)
        try:
            db.add_edge("file:src/a.py", "file:ghost.py", kind="imports")
            rows = db.conn.execute("SELECT * FROM edges").fetchall()
            assert len(rows) == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Bug 14 — add_symbol FK race
# ---------------------------------------------------------------------------


class TestAddSymbolFKSafety:
    def test_inserts_when_file_node_exists(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_node("file:src/a.py", "file", "a.py", "src/a.py", layer="api")
            db.add_symbol(
                symbol_id="file:src/a.py::foo",
                file_node_id="file:src/a.py",
                name="foo", kind="function",
            )
            rows = db.conn.execute("SELECT * FROM symbols").fetchall()
            assert len(rows) == 1
        finally:
            db.close()

    def test_file_node_missing_does_not_raise(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            # Don't add the file node first — should be silently dropped.
            db.add_symbol(
                symbol_id="file:ghost.py::foo",
                file_node_id="file:ghost.py",
                name="foo", kind="function",
            )
            rows = db.conn.execute("SELECT * FROM symbols").fetchall()
            assert len(rows) == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Bug 15 — record_outcome FK race
# ---------------------------------------------------------------------------


class TestRecordOutcomeFKSafety:
    def test_inserts_when_session_exists(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.log_session("s1", "test session", "phase1", [])
            db.record_outcome("s1", "src/a.py", outcome_type="kept")
            rows = db.conn.execute("SELECT * FROM outcomes").fetchall()
            assert len(rows) == 1
        finally:
            db.close()

    def test_session_missing_does_not_raise(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.record_outcome("missing-session", "src/a.py", outcome_type="kept")
            rows = db.conn.execute("SELECT * FROM outcomes").fetchall()
            assert len(rows) == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Bug 16 — _claude_config_path now points at <project>/.mcp.json
# ---------------------------------------------------------------------------


class TestClaudeConfigPathProjectScope:
    """Bug 16: per-project Claude Code MCP must go to .mcp.json,
    not <project>/.claude/settings.json (which is for hooks)."""

    def test_returns_dot_mcp_json_at_project_root(self, tmp_path):
        from mcp_server.ide_inject import _claude_config_path
        path = _claude_config_path(tmp_path)
        assert path == tmp_path / ".mcp.json", (
            "Bug 16: per-project Claude Code MCP config must be "
            f"<project>/.mcp.json, got {path}"
        )

    def test_inject_claude_writes_to_dot_mcp_json(self, tmp_path):
        """The full _inject_claude path writes to <project>/.mcp.json."""
        from mcp_server.ide_inject import _inject_claude

        proj = tmp_path / "myproject"
        proj.mkdir()

        result_path = _inject_claude(
            project_root=proj,
            cmd_path="/fake/codevira",
            python_exe="/fake/python",
        )

        assert result_path == str(proj / ".mcp.json")
        assert (proj / ".mcp.json").exists()

        import json
        data = json.loads((proj / ".mcp.json").read_text())
        assert "mcpServers" in data
        assert "codevira" in data["mcpServers"]

    def test_inject_claude_does_not_touch_settings_json(self, tmp_path):
        """Regression guard: confirm we no longer touch
        <project>/.claude/settings.json (which is for hooks)."""
        from mcp_server.ide_inject import _inject_claude

        proj = tmp_path / "myproject"
        proj.mkdir()

        _inject_claude(
            project_root=proj,
            cmd_path="/fake/codevira",
            python_exe="/fake/python",
        )

        # settings.json must NOT have been created by us.
        assert not (proj / ".claude" / "settings.json").exists(), (
            "Bug 16 regression: _inject_claude wrote to settings.json "
            "again. That's the wrong file for mcpServers."
        )
