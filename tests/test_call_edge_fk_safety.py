"""Tests for add_call_edge FK-safety — Bug 9 regression guard.

The watcher's incremental reindex builds a ``name → symbol_id`` lookup
in graph_generator, then iterates over symbols inserting call edges.
Between the lookup and the insert, a concurrent transaction can delete
a symbol (e.g. when a file is removed mid-reindex). Pre-rc.4, this
raised ``sqlite3.IntegrityError: FOREIGN KEY constraint failed`` in
``add_call_edge`` and the watcher crashed. The crash log on Sachin's
machine recorded 67 of these in v1.8.1 alone.

rc.4 fix: the INSERT uses ``WHERE EXISTS`` subqueries so rows referencing
missing symbols are silently dropped. Losing one call edge beats
crashing the watcher — the edge is rebuilt on the next full reindex.

These tests cover:
  - Both endpoints exist → row inserted (happy path)
  - Caller missing → row dropped, no exception
  - Callee missing → row dropped, no exception
  - Both missing → row dropped, no exception
  - INSERT OR REPLACE semantic preserved when both endpoints exist
"""
from __future__ import annotations



from indexer.sqlite_graph import SQLiteGraph


def _add_file_and_symbol(db: SQLiteGraph, file_id: str, symbol_id: str, name: str) -> None:
    """Helper: create a file node and a symbol attached to it."""
    db.add_node(file_id, "file", name + ".py", file_id, layer="api")
    db.add_symbol(
        symbol_id=symbol_id,
        file_node_id=file_id,
        name=name,
        kind="function",
    )


# ---------------------------------------------------------------------------
# Happy path — both endpoints exist
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_inserts_when_both_endpoints_exist(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            _add_file_and_symbol(db, "file:src/a.py", "file:src/a.py::foo", "foo")
            _add_file_and_symbol(db, "file:src/b.py", "file:src/b.py::bar", "bar")

            db.add_call_edge("file:src/a.py::foo", "file:src/b.py::bar", line=10)

            row = db.conn.execute(
                "SELECT * FROM call_edges WHERE caller_id = ? AND callee_id = ?",
                ("file:src/a.py::foo", "file:src/b.py::bar"),
            ).fetchone()
            assert row is not None
            assert row["line"] == 10
        finally:
            db.close()

    def test_insert_or_replace_semantic_preserved(self, tmp_path):
        """If the same edge is added twice with different lines, the
        second wins (INSERT OR REPLACE)."""
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            _add_file_and_symbol(db, "file:a.py", "file:a.py::foo", "foo")
            _add_file_and_symbol(db, "file:b.py", "file:b.py::bar", "bar")

            db.add_call_edge("file:a.py::foo", "file:b.py::bar", line=5)
            db.add_call_edge("file:a.py::foo", "file:b.py::bar", line=42)

            rows = db.conn.execute(
                "SELECT * FROM call_edges WHERE caller_id = ? AND callee_id = ?",
                ("file:a.py::foo", "file:b.py::bar"),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["line"] == 42
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Bug 9 — FK violations now silently drop the row instead of raising
# ---------------------------------------------------------------------------


class TestFKSafety:
    def test_caller_missing_does_not_raise(self, tmp_path):
        """Bug 9 regression test: pre-rc.4 this raised IntegrityError."""
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            _add_file_and_symbol(db, "file:b.py", "file:b.py::bar", "bar")

            # caller_id doesn't exist in symbols. Pre-rc.4: IntegrityError.
            # rc.4: silent drop.
            db.add_call_edge("file:ghost.py::missing", "file:b.py::bar", line=1)

            count = db.conn.execute(
                "SELECT COUNT(*) AS c FROM call_edges"
            ).fetchone()["c"]
            assert count == 0, "row referencing missing caller should be dropped"
        finally:
            db.close()

    def test_callee_missing_does_not_raise(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            _add_file_and_symbol(db, "file:a.py", "file:a.py::foo", "foo")

            db.add_call_edge("file:a.py::foo", "file:ghost.py::missing", line=1)

            count = db.conn.execute(
                "SELECT COUNT(*) AS c FROM call_edges"
            ).fetchone()["c"]
            assert count == 0
        finally:
            db.close()

    def test_both_missing_does_not_raise(self, tmp_path):
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            db.add_call_edge("ghost1", "ghost2", line=1)
            count = db.conn.execute(
                "SELECT COUNT(*) AS c FROM call_edges"
            ).fetchone()["c"]
            assert count == 0
        finally:
            db.close()

    def test_concurrent_symbol_delete_simulation(self, tmp_path):
        """Simulate the actual race: build all_symbols lookup, then a
        concurrent transaction deletes one of them, then we try to add
        the edge. Verify no IntegrityError + no row inserted."""
        db = SQLiteGraph(tmp_path / "graph.db")
        try:
            _add_file_and_symbol(db, "file:a.py", "file:a.py::foo", "foo")
            _add_file_and_symbol(db, "file:b.py", "file:b.py::bar", "bar")

            # Snapshot the lookup
            all_symbols = {
                row["name"]: row["id"]
                for row in db.conn.execute("SELECT id, name FROM symbols").fetchall()
            }
            assert "bar" in all_symbols

            # Simulate concurrent delete (e.g. file:b.py was removed mid-reindex)
            db.remove_symbols_for_file("file:b.py")

            # Now try to add the edge — bar was deleted but our snapshot
            # still references it. Pre-rc.4: crash. rc.4: silent drop.
            db.add_call_edge(all_symbols["foo"], all_symbols["bar"])

            count = db.conn.execute(
                "SELECT COUNT(*) AS c FROM call_edges"
            ).fetchone()["c"]
            assert count == 0
        finally:
            db.close()
