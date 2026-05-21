"""Tests for retire_rule MCP tool — Bug 3 regression guard.

Real dogfood (Sachin's UDAP project, 2026-05-06): codevira had three
confidence: 1.00 learned rules pinned to ``src/control/cli/`` — a
directory the user was about to delete in a Python→Go migration. Once
the delete commit landed those rules would fire as false positives
on every SessionStart. The MCP surface had no way to retire them.

Tests cover:

  - DB layer: retired_at column, retire_learned_rule + unretire,
    get_learned_rules filters retired by default
  - Tool layer: ``retire_rule(rule_id, reason)`` flips the flag and
    returns the right shape; ``get_learned_rules`` exposes ``id`` +
    ``retired`` so the AI can find and act on stale rules
  - Schema migration: ALTER TABLE ADD COLUMN is idempotent across
    multiple opens (so existing v2.0-rc.2 graphs auto-upgrade)
"""
from __future__ import annotations

from pathlib import Path


from indexer.sqlite_graph import SQLiteGraph


# ---------------------------------------------------------------------------
# DB layer — retired column + retire_learned_rule
# ---------------------------------------------------------------------------


class TestRetiredColumnExists:
    def test_columns_added_on_init(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            cols = db.conn.execute(
                "PRAGMA table_info(learned_rules)"
            ).fetchall()
            names = {row["name"] for row in cols}
            assert "retired_at" in names, (
                "retired_at column must be added on db init "
                "(or Bug 3's schema migration is broken)"
            )
            assert "retired_reason" in names
        finally:
            db.close()

    def test_alter_table_is_idempotent_across_opens(self, tmp_path):
        """Open + close + reopen should not raise from re-running
        the ALTER TABLE migration."""
        path = tmp_path / "graph.db"
        for _ in range(3):
            db = SQLiteGraph(path)
            db.close()
        # If this didn't raise, idempotency holds.


class TestRetireLearnedRule:
    def test_retires_an_active_rule(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_learned_rule(
                "rule one", 0.9, ["s1"], category="testing",
                file_pattern="src/old/",
            )
            rules = db.get_learned_rules()
            assert len(rules) == 1
            rule_id = rules[0]["id"]

            ok = db.retire_learned_rule(rule_id, reason="deleted in week 2")
            assert ok is True

            # Default get_learned_rules excludes retired
            assert db.get_learned_rules() == []

            # include_retired surfaces it
            with_retired = db.get_learned_rules(include_retired=True)
            assert len(with_retired) == 1
            assert with_retired[0]["retired_at"] is not None
            assert with_retired[0]["retired_reason"] == "deleted in week 2"
        finally:
            db.close()

    def test_retire_nonexistent_returns_false(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            assert db.retire_learned_rule(rule_id=99999, reason="x") is False
        finally:
            db.close()

    def test_retire_already_retired_returns_false(self, tmp_path):
        """Retiring a rule that's already retired is a no-op (returns False).

        We use a strict ``WHERE retired_at IS NULL`` clause to avoid
        bumping the timestamp on every retry."""
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_learned_rule("rule one", 0.9, ["s1"])
            rule_id = db.get_learned_rules()[0]["id"]

            assert db.retire_learned_rule(rule_id, reason="first") is True
            assert db.retire_learned_rule(rule_id, reason="again") is False
        finally:
            db.close()

    def test_unretire(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_learned_rule("rule one", 0.9, ["s1"])
            rule_id = db.get_learned_rules()[0]["id"]
            db.retire_learned_rule(rule_id, reason="oops")

            assert db.get_learned_rules() == []
            assert db.unretire_learned_rule(rule_id) is True
            assert len(db.get_learned_rules()) == 1
            assert db.get_learned_rules()[0]["retired_at"] is None
        finally:
            db.close()


class TestGetLearnedRulesFilteringDefault:
    """The whole point of Bug 3 — retired rules must NOT appear in the
    default get_learned_rules output."""

    def test_mixed_retired_and_active_returns_only_active(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_learned_rule("active a", 0.9, ["s1"])
            db.add_learned_rule("active b", 0.8, ["s2"])
            db.add_learned_rule("stale c", 0.95, ["s3"])

            # Retire the third one
            stale_id = next(
                r["id"] for r in db.get_learned_rules() if r["rule_text"] == "stale c"
            )
            db.retire_learned_rule(stale_id, reason="dir deleted")

            active_only = db.get_learned_rules()
            texts = {r["rule_text"] for r in active_only}
            assert texts == {"active a", "active b"}

            include_all = db.get_learned_rules(include_retired=True)
            assert len(include_all) == 3
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Tool layer — retire_rule + get_learned_rules expose id/retired
# ---------------------------------------------------------------------------


class TestToolLayer:
    def test_get_learned_rules_includes_id_and_retired(self, project_env):
        project, data_dir, db = project_env
        db.add_learned_rule("test rule", 0.9, ["s1"], category="testing")

        from mcp_server.tools.learning import get_learned_rules

        result = get_learned_rules()
        assert "rules" in result
        assert len(result["rules"]) == 1

        r = result["rules"][0]
        assert "id" in r, "Bug 3: rules must surface their numeric id"
        assert isinstance(r["id"], int)
        assert "retired" in r
        assert r["retired"] is False

    def test_retire_rule_tool_flips_the_flag(self, project_env):
        project, data_dir, db = project_env
        db.add_learned_rule(
            "src/control/cli/ pin", 1.0, ["s1"],
            category="testing", file_pattern="src/control/cli/",
        )

        from mcp_server.tools.learning import retire_rule, get_learned_rules

        rule_id = get_learned_rules()["rules"][0]["id"]
        result = retire_rule(rule_id=rule_id, reason="deleted in Plan 1 Week 2")

        assert result["retired"] is True
        assert result["rule_id"] == rule_id
        assert result["reason"] == "deleted in Plan 1 Week 2"

        # Subsequent get_learned_rules should not surface it
        assert get_learned_rules()["rules"] == []

    def test_retire_rule_tool_handles_missing_id(self, project_env):
        from mcp_server.tools.learning import retire_rule

        result = retire_rule(rule_id=99999, reason="x")
        assert result["retired"] is False
        assert "error" in result
        assert "99999" in result["error"]

    def test_retire_rule_with_no_reason(self, project_env):
        """reason is optional — should still work."""
        project, data_dir, db = project_env
        db.add_learned_rule("rule x", 0.9, ["s1"])

        from mcp_server.tools.learning import retire_rule, get_learned_rules

        rule_id = get_learned_rules()["rules"][0]["id"]
        result = retire_rule(rule_id=rule_id)
        assert result["retired"] is True
        assert result["reason"] is None


# ---------------------------------------------------------------------------
# Integration: get_session_context surfaces only active rules
# ---------------------------------------------------------------------------


class TestSessionContextRespectsRetired:
    def test_retired_rules_dropped_from_session_context_top_signals(
        self, project_env
    ):
        project, data_dir, db = project_env
        db.add_learned_rule("alive", 0.9, ["s1"])
        db.add_learned_rule("stale", 0.95, ["s2"])

        from mcp_server.tools.learning import get_session_context, get_learned_rules

        # Before retire: both surface
        before = get_session_context()
        rule_texts_before = {
            r["rule"] for r in before["top_signals"]["rules"]
        }
        assert "alive" in rule_texts_before
        assert "stale" in rule_texts_before

        # Retire stale
        stale_id = next(
            r["id"] for r in get_learned_rules()["rules"] if r["rule"] == "stale"
        )
        from mcp_server.tools.learning import retire_rule
        retire_rule(rule_id=stale_id, reason="x")

        # After retire: only alive surfaces
        after = get_session_context()
        rule_texts_after = {
            r["rule"] for r in after["top_signals"]["rules"]
        }
        assert "alive" in rule_texts_after
        assert "stale" not in rule_texts_after, (
            "Bug 3: retired rules must not surface in get_session_context"
        )


# ---------------------------------------------------------------------------
# MCP tool registration contract
# ---------------------------------------------------------------------------


class TestMCPToolRegistration:
    def test_retire_rule_tool_exists_in_server_module(self):
        """Read mcp_server/server.py and confirm the tool is wired up."""
        import mcp_server
        server_path = Path(mcp_server.__file__).parent / "server.py"
        content = server_path.read_text(encoding="utf-8")
        assert 'name="retire_rule"' in content, (
            "Bug 3: retire_rule must be registered as an MCP tool"
        )
        assert 'elif name == "retire_rule"' in content, (
            "Bug 3: retire_rule must have a dispatch handler"
        )
