"""
v3.7.0 (M1) — automatic data self-heal at startup.

These fail without `migrate.run_startup_migrations` (AttributeError). They
exercise the real collision-repair path: a pre-3.7 store with a base-id
collision is healed automatically, idempotently, non-destructively.
"""

from __future__ import annotations

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
