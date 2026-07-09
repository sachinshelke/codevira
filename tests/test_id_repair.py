"""
Tests for mcp_server/storage/id_repair.py — Phase 25 Tier-0.

The load-bearing property is CONVERGENCE: normalize is a pure, order-independent
fixed point, so two engineers who merge colliding stores get byte-identical
output and the repair can never oscillate.
"""

from __future__ import annotations

import json

from mcp_server.storage import id_repair


def _canon(recs):
    """Order-independent identity of a record set (for convergence checks)."""
    return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in recs)


def _base(id_, decision, *, ts, host, dnr=False):
    return {
        "id": id_,
        "decision": decision,
        "do_not_revert": dnr,
        "ts": ts,
        "origin": {"host_hash": host},
    }


class TestFindCollisions:
    def test_two_bases_same_id_is_a_collision(self):
        recs = [
            _base("D000120", "alice", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000120", "bob", ts="2026-01-01T10:05:00", host="bbb"),
        ]
        cols = id_repair.find_collisions(recs)
        assert set(cols) == {"D000120"}
        assert len(cols["D000120"]) == 2

    def test_amendment_reusing_id_is_not_a_collision(self):
        recs = [
            _base("D000120", "alice", ts="2026-01-01T10:00:00", host="aaa"),
            {"id": "D000120", "_amendment_to_id": "D000120", "do_not_revert": True},
        ]
        assert id_repair.find_collisions(recs) == {}

    def test_distinct_ids_no_collision(self):
        recs = [
            _base("D000120", "a", ts="t1", host="aaa"),
            _base("D000121", "b", ts="t2", host="bbb"),
        ]
        assert id_repair.find_collisions(recs) == {}


class TestNormalize:
    def test_earliest_writer_keeps_id_loser_renumbered(self):
        recs = [
            _base("D000120", "bob later", ts="2026-01-01T10:05:00", host="bbb"),
            _base("D000120", "alice first", ts="2026-01-01T10:00:00", host="aaa"),
        ]
        out = id_repair.normalize(recs)
        # No base-id collisions remain.
        assert id_repair.find_collisions(out["records"]) == {}
        by_text = {r["decision"]: r["id"] for r in out["records"]}
        # Earliest writer (alice) keeps the original id.
        assert by_text["alice first"] == "D000120"
        # Loser got a content-derived id, distinct and non-sequential.
        assert by_text["bob later"] != "D000120"
        assert by_text["bob later"].startswith("D")
        assert len(out["remap"]) == 1
        assert out["remap"][0]["old_id"] == "D000120"

    def test_byte_identical_duplicate_is_deduped(self):
        r = _base("D000120", "same decision", ts="2026-01-01T10:00:00", host="aaa")
        out = id_repair.normalize([dict(r), dict(r)])
        assert out["deduped"] == 1
        assert len(out["records"]) == 1
        assert out["records"][0]["id"] == "D000120"

    def test_amendment_follows_renumbered_loser_by_host(self):
        recs = [
            _base("D000120", "alice first", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000120", "bob later", ts="2026-01-01T10:05:00", host="bbb"),
            # Bob's amendment to his own D000120 (same host bbb).
            {
                "id": "D000120",
                "_amendment_to_id": "D000120",
                "do_not_revert": True,
                "origin": {"host_hash": "bbb"},
            },
        ]
        out = id_repair.normalize(recs)
        bob = next(r for r in out["records"] if r.get("decision") == "bob later")
        amend = next(r for r in out["records"] if r.get("_amendment_to_id"))
        # The amendment moved to follow bob's renumbered base.
        assert amend["_amendment_to_id"] == bob["id"]
        assert amend["id"] == bob["id"]

    def test_idempotent(self):
        recs = [
            _base("D000120", "bob", ts="2026-01-01T10:05:00", host="bbb"),
            _base("D000120", "alice", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000121", "carol", ts="2026-01-02T00:00:00", host="ccc"),
        ]
        once = id_repair.normalize(recs)["records"]
        twice = id_repair.normalize(once)["records"]
        assert _canon(once) == _canon(twice)
        # Second pass finds nothing to do.
        assert id_repair.normalize(once)["collisions"] == 0

    def test_convergence_order_independent(self):
        """The load-bearing property: normalize is a fixed point independent of
        input order — two machines that merge in different orders converge to
        byte-identical records."""
        recs = [
            _base("D000120", "alice", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000120", "bob", ts="2026-01-01T10:05:00", host="bbb"),
            _base("D000120", "carol", ts="2026-01-01T10:03:00", host="ccc"),
            _base("D000121", "dave", ts="2026-01-02T00:00:00", host="ddd"),
        ]
        forward = id_repair.normalize(recs)["records"]
        backward = id_repair.normalize(list(reversed(recs)))["records"]
        assert _canon(forward) == _canon(backward)
        # And no collisions survive in either.
        assert id_repair.find_collisions(forward) == {}

    def test_three_way_collision_all_survive_with_distinct_ids(self):
        recs = [
            _base("D000120", "a", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000120", "b", ts="2026-01-01T10:01:00", host="bbb"),
            _base("D000120", "c", ts="2026-01-01T10:02:00", host="ccc"),
        ]
        out = id_repair.normalize(recs)
        ids = {r["id"] for r in out["records"]}
        assert len(ids) == 3, "all three distinct decisions must survive"
        assert "D000120" in ids  # the winner kept it
