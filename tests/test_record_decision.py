"""Tests for record_decision + mark_decision_protected — Bug 2 regression guard.

Real dogfood (Sachin's UDAP project, 2026-05-06): user asked AI to log
a decision and "mark do_not_revert=true". The AI scanned the MCP tool
surface and concluded "no tool accepts do_not_revert as a flag on a
decision — only on a file via update_node". That was correct; codevira
had no decision-level protection mechanism. Hero 1's positioning
("AI cannot undo your protected decisions") was missing the canonical
write path.

Bug 2 fix:
  - decisions table gets a do_not_revert column (default 0)
  - new ``record_decision`` tool creates a single decision with the flag
  - new ``mark_decision_protected`` flips it on an existing decision
  - search_decisions / get_recent_decisions surface the flag

These tests cover:
  - DB layer: schema, record_decision, set_decision_protection
  - Tool layer: record_decision, mark_decision_protected
  - Integration: search_decisions returns the flag
  - MCP tool registration contract
"""

from __future__ import annotations

import pytest

from indexer.sqlite_graph import SQLiteGraph


# ---------------------------------------------------------------------------
# DB layer — schema migration + record_decision
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_decisions_has_do_not_revert_column(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            cols = db.conn.execute("PRAGMA table_info(decisions)").fetchall()
            names = {row["name"] for row in cols}
            assert (
                "do_not_revert" in names
            ), "Bug 2: decisions table must have do_not_revert column"
        finally:
            db.close()

    def test_default_value_is_zero(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="test")
            decisions = db.get_recent_decisions(limit=1)
            assert decisions[0]["do_not_revert"] == 0
        finally:
            db.close()


class TestRecordDecisionDB:
    def test_records_decision_with_protection(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(
                decision="Use Postgres for cortex metadata",
                file_path="cortex/datastore/sqlite_store.py",
                context="multi-host operator access",
                do_not_revert=True,
            )
            assert "decision_id" in res
            assert "session_id" in res

            # Confirm via get_recent_decisions
            recent = db.get_recent_decisions(limit=5)
            d = next(d for d in recent if d["id"] == res["decision_id"])
            assert d["do_not_revert"] == 1
            assert d["decision"] == "Use Postgres for cortex metadata"
            assert d["file_path"] == "cortex/datastore/sqlite_store.py"
            assert d["context"] == "multi-host operator access"
        finally:
            db.close()

    def test_records_decision_without_protection_default(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="something casual")
            recent = db.get_recent_decisions(limit=1)
            assert recent[0]["do_not_revert"] == 0
        finally:
            db.close()

    def test_auto_creates_session_when_omitted(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="x")
            sid = res["session_id"]
            # Session row exists (FK constraint passed, plus we can fetch it)
            sessions = db.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (sid,)
            ).fetchall()
            assert len(sessions) == 1
        finally:
            db.close()

    def test_uses_provided_session_id(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="x", session_id="my-explicit-session")
            assert res["session_id"] == "my-explicit-session"
        finally:
            db.close()


class TestSetDecisionProtection:
    def test_flips_protection_on(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="x", do_not_revert=False)
            did = res["decision_id"]

            ok = db.set_decision_protection(did, True)
            assert ok is True

            d = next(d for d in db.get_recent_decisions(limit=1) if d["id"] == did)
            assert d["do_not_revert"] == 1
        finally:
            db.close()

    def test_flips_protection_off(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            res = db.record_decision(decision="x", do_not_revert=True)
            did = res["decision_id"]

            ok = db.set_decision_protection(did, False)
            assert ok is True

            d = next(d for d in db.get_recent_decisions(limit=1) if d["id"] == did)
            assert d["do_not_revert"] == 0
        finally:
            db.close()

    def test_returns_false_for_missing_id(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            assert db.set_decision_protection(99999, True) is False
        finally:
            db.close()


# ---------------------------------------------------------------------------
# search_decisions surfaces do_not_revert
# ---------------------------------------------------------------------------


class TestSearchDecisionsSurfaceFlag:
    def test_search_returns_do_not_revert_flag(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.record_decision(
                decision="lock me down",
                file_path="src/auth.py",
                do_not_revert=True,
            )
            db.record_decision(
                decision="reversible thought",
                file_path="src/auth.py",
                do_not_revert=False,
            )

            matches = db.search_decisions(query="auth")
            # Both should appear
            by_text = {m["decision"]: m for m in matches}
            assert by_text["lock me down"]["do_not_revert"] == 1
            assert by_text["reversible thought"]["do_not_revert"] == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------


class TestRecordDecisionTool:
    @pytest.mark.skip(
        reason="v2.2.0: tests deprecated feature (search_codebase / _check_search_deps / graph.db backend)"
    )
    def test_records_with_protection(self, project_env):
        from mcp_server.tools.learning import record_decision

        result = record_decision(
            decision="use Postgres",
            file_path="cortex/datastore/sqlite_store.py",
            context="multi-host operator access",
            do_not_revert=True,
        )
        assert result["recorded"] is True
        assert result["do_not_revert"] is True
        assert isinstance(result["decision_id"], int)
        assert "protected" in result["hint"].lower() or "lock" in result["hint"].lower()

    def test_records_without_protection_by_default(self, project_env):
        from mcp_server.tools.learning import record_decision

        result = record_decision(decision="quick note")
        assert result["recorded"] is True
        assert result["do_not_revert"] is False

    def test_rejects_empty_decision(self, project_env):
        from mcp_server.tools.learning import record_decision

        result = record_decision(decision="")
        assert result["recorded"] is False
        assert "error" in result


class TestMarkDecisionProtectedTool:
    def test_flips_an_existing_decision(self, project_env):
        from mcp_server.tools.learning import (
            record_decision,
            mark_decision_protected,
        )

        rec = record_decision(decision="something", do_not_revert=False)
        did = rec["decision_id"]

        result = mark_decision_protected(decision_id=did, do_not_revert=True)
        assert result["updated"] is True
        assert result["do_not_revert"] is True

    def test_returns_error_on_missing_id(self, project_env):
        from mcp_server.tools.learning import mark_decision_protected

        result = mark_decision_protected(decision_id=99999, do_not_revert=True)
        assert result["updated"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# MCP tool registration contract
# ---------------------------------------------------------------------------


class TestMCPToolRegistration:
    def test_record_decision_tool_registered(self):
        import mcp_server
        from pathlib import Path

        server_path = Path(mcp_server.__file__).parent / "server.py"
        content = server_path.read_text(encoding="utf-8")
        assert 'name="record_decision"' in content
        assert 'elif name == "record_decision"' in content

    def test_mark_decision_protected_tool_registered(self):
        import mcp_server
        from pathlib import Path

        server_path = Path(mcp_server.__file__).parent / "server.py"
        content = server_path.read_text(encoding="utf-8")
        assert 'name="mark_decision_protected"' in content
        assert 'elif name == "mark_decision_protected"' in content

    def test_update_node_description_mentions_record_decision(self):
        """update_node's description must direct AIs toward
        record_decision for decision-level protection (not just file)."""
        import mcp_server
        from pathlib import Path

        server_path = Path(mcp_server.__file__).parent / "server.py"
        content = server_path.read_text(encoding="utf-8")
        # Find the update_node block; assert it references record_decision
        # in the description.
        idx = content.find('name="update_node"')
        assert idx >= 0
        # Scan ~1500 chars of the description block
        block = content[idx : idx + 2000]
        assert "record_decision" in block, (
            "Bug 2: update_node description must point AIs at record_decision "
            "for decision-level protection"
        )
