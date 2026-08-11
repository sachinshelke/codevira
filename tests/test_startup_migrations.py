"""
v3.7.0 (M1) — automatic data self-heal at startup.

These fail without `migrate.run_startup_migrations` (AttributeError). They
exercise the real collision-repair path: a pre-3.7 store with a base-id
collision is healed automatically, idempotently, non-destructively.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from mcp_server import migrate
from mcp_server.storage import id_repair, jsonl_store
from mcp_server.storage import paths as store_paths


def _collide(host, decision, ts):
    # Two records minted with the SAME id on two machines — a base-id collision
    # (read_merged silently shadows one until repaired).
    return {
        "id": "D000120",
        "decision": decision,
        "ts": ts,
        "origin": {"host_hash": host},
    }


class TestStartupMigrations:
    def test_auto_repairs_preexisting_collision(self):
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "alice decision", "2026-01-01T10:00:00"))
        jsonl_store.append(p, _collide("bbb", "bob decision", "2026-01-01T10:05:00"))
        # Precondition: a real base-id collision exists.
        assert id_repair.find_collisions(jsonl_store.read_all(p)) != {}

        res = migrate.run_startup_migrations()
        assert "v370_repair_collisions" in res["applied"]

        # Healed automatically; BOTH decisions survive.
        raw = jsonl_store.read_all(p)
        assert id_repair.find_collisions(raw) == {}
        assert {"alice decision", "bob decision"} <= {r["decision"] for r in raw}
        # Non-destructive: a backup was taken before the rewrite.
        assert p.with_name(p.name + ".bak-pre-v370").exists()

    def test_idempotent_second_run_applies_nothing(self):
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "alice", "2026-01-01T10:00:00"))
        jsonl_store.append(p, _collide("bbb", "bob", "2026-01-01T10:05:00"))

        first = migrate.run_startup_migrations()
        assert "v370_repair_collisions" in first["applied"]

        second = migrate.run_startup_migrations()
        assert second["applied"] == [], "ledger must gate a re-run"
        assert "v370_repair_collisions" in second["ledger"]

    def test_all_v370_migrations_are_registered(self):
        # A first run applies every named v3.7.0 migration exactly once.
        res = migrate.run_startup_migrations()
        for name in (
            "v370_repair_collisions",
            "v370_merge_driver",
            "v370_dedupe_registration",
        ):
            assert name in res["ledger"]

    def test_clean_store_marks_migration_without_rewriting(self):
        # No collision -> repair is a no-op but STILL marked applied, so we
        # don't re-scan the store on every boot. No backup is taken.
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "solo", "2026-01-01T10:00:00"))
        before = p.read_text()

        res = migrate.run_startup_migrations()
        assert "v370_repair_collisions" in res["ledger"]
        assert p.read_text() == before, "clean store must not be rewritten"
        assert not p.with_name(p.name + ".bak-pre-v370").exists()


class TestV401RebuildImportEdges:
    """4.0.1: heal graphs left edgeless by the pre-4.0.1 import resolver.

    Import resolution gated every import on a package set that fell back to a
    hardcoded ``["src"]``, so any project not laid out that way built ZERO
    import edges and ``get_impact`` answered "blast radius 0" for every file
    (D00013N). The resolver is fixed, but a graph already on disk stays
    edgeless until something rebuilds it — and a fix that depends on the user
    reading a release note and running ``codevira index --full`` is a fix most
    users never get.

    The guard must be narrow: fire on the broken shape, never on a healthy or
    legitimately-edgeless graph.
    """

    def _graph(self, tmp_path, *, nodes: int, import_edges: int):
        """Build a graph.db with the requested shape and point codevira at it."""
        import sqlite3

        db = tmp_path / "graph" / "graph.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE edges (src TEXT, dst TEXT, kind TEXT)")
        for i in range(nodes):
            conn.execute("INSERT INTO nodes VALUES (?)", (f"file:m{i}.py",))
        for i in range(import_edges):
            conn.execute(
                "INSERT INTO edges VALUES (?, ?, 'imports')",
                (f"file:m{i}.py", "file:m0.py"),
            )
        conn.commit()
        conn.close()
        return db

    def _run(self, tmp_path, monkeypatch):
        """Run the migration with a spy on the background reindex."""
        from mcp_server import migrate

        monkeypatch.setattr(migrate, "get_data_dir", lambda: tmp_path, raising=False)
        import mcp_server.paths as _paths

        monkeypatch.setattr(_paths, "get_data_dir", lambda: tmp_path)
        spy = MagicMock()
        fake = types.ModuleType("indexer.index_codebase")
        fake.start_background_full_index = spy
        with patch.dict(sys.modules, {"indexer.index_codebase": fake}):
            result = migrate._mig_v401_rebuild_import_edges(tmp_path)
        return result, spy

    def test_rebuilds_when_nodes_exist_but_no_import_edges(self, tmp_path, monkeypatch):
        """The bug's exact shape: 1197 nodes, 0 edges -> rebuild."""
        self._graph(tmp_path, nodes=1197, import_edges=0)
        result, spy = self._run(tmp_path, monkeypatch)
        assert result is True
        assert spy.call_count == 1, "should have kicked off a background reindex"

    def test_healthy_graph_is_left_alone(self, tmp_path, monkeypatch):
        """A graph that already has import edges must not pay for a rebuild."""
        self._graph(tmp_path, nodes=300, import_edges=793)
        result, spy = self._run(tmp_path, monkeypatch)
        assert result is False
        assert spy.call_count == 0

    def test_empty_graph_is_left_alone(self, tmp_path, monkeypatch):
        """Nothing indexed yet — auto_init owns the first index, not this."""
        self._graph(tmp_path, nodes=0, import_edges=0)
        result, spy = self._run(tmp_path, monkeypatch)
        assert result is False
        assert spy.call_count == 0

    def test_missing_graph_db_is_a_noop(self, tmp_path, monkeypatch):
        """No graph.db at all must not raise — startup can never be blocked."""
        result, spy = self._run(tmp_path, monkeypatch)
        assert result is False
        assert spy.call_count == 0

    def test_registered_in_startup_migrations(self):
        """It must actually be wired in, or none of the above matters."""
        from mcp_server import migrate

        names = [n for n, _ in migrate._STARTUP_MIGRATIONS]
        assert "v401_rebuild_import_edges" in names
        # Ledger-gated (not in _ALWAYS_RERUN): one rebuild per project, ever.
        assert "v401_rebuild_import_edges" not in migrate._ALWAYS_RERUN

    def test_a_raising_reindex_never_blocks_startup(self, tmp_path, monkeypatch):
        """Failure-isolation: if the reindex cannot start, we return False
        rather than propagating into the startup path."""
        from mcp_server import migrate

        self._graph(tmp_path, nodes=10, import_edges=0)
        import mcp_server.paths as _paths

        monkeypatch.setattr(_paths, "get_data_dir", lambda: tmp_path)
        boom = MagicMock(side_effect=RuntimeError("indexer exploded"))
        fake = types.ModuleType("indexer.index_codebase")
        fake.start_background_full_index = boom
        with patch.dict(sys.modules, {"indexer.index_codebase": fake}):
            assert migrate._mig_v401_rebuild_import_edges(tmp_path) is False
