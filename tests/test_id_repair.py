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

    def test_minted_loser_ids_globally_unique_under_hash_collision(self, monkeypatch):
        """H5: with a tiny loser-hash width, 19 losers can't fit the 16-value
        1-hex space (pigeonhole) — normalize must widen so every id stays
        unique, and the result must be a true fixed point (no fresh collision
        that re-opens read_merged shadowing)."""
        monkeypatch.setattr(id_repair, "_LOSER_HASH_WIDTH", 1)
        recs = [
            _base(
                "D000120",
                f"distinct decision number {i}",
                ts=f"2026-01-01T10:{i:02d}:00",
                host=f"h{i}",
            )
            for i in range(20)
        ]
        out = id_repair.normalize(recs)
        ids = [r["id"] for r in out["records"]]
        assert len(set(ids)) == len(ids), f"minted loser ids collided: {ids}"
        assert id_repair.find_collisions(out["records"]) == {}
        # True fixed point: re-normalizing changes nothing.
        twice = id_repair.normalize(out["records"])["records"]
        assert _canon(out["records"]) == _canon(twice)

    def test_loser_id_avoids_an_existing_distinct_base_id(self, monkeypatch):
        """H5: a minted loser id must also avoid a DISTINCT pre-existing base
        id, not just other losers."""
        monkeypatch.setattr(id_repair, "_LOSER_HASH_WIDTH", 1)
        # 16 singleton bases occupy every 1-hex value D0..Df, plus a collision
        # pair whose loser must therefore widen past width 1.
        recs = [
            _base(f"D{d:x}", f"singleton {d}", ts="2026-01-01T09:00:00", host="s")
            for d in range(16)
        ] + [
            _base("D000120", "winner", ts="2026-01-01T10:00:00", host="a"),
            _base("D000120", "loser", ts="2026-01-01T10:01:00", host="b"),
        ]
        out = id_repair.normalize(recs)
        ids = [r["id"] for r in out["records"]]
        assert len(set(ids)) == len(ids)
        assert id_repair.find_collisions(out["records"]) == {}

    def test_amendment_ambiguous_same_host_stays_on_winner_and_flagged(self):
        """M6: winner+loser share a host, so an amendment on that host can't be
        attributed — it must stay on the WINNER (keeps old id) and be flagged,
        not silently follow the loser (which would move protection/staleness
        onto the wrong engineer's decision)."""
        recs = [
            _base("D000120", "winner", ts="2026-01-01T10:00:00", host="same"),
            _base("D000120", "loser", ts="2026-01-01T10:05:00", host="same"),
            {
                "id": "D000120",
                "_amendment_to_id": "D000120",
                "do_not_revert": True,
                "origin": {"host_hash": "same"},
            },
        ]
        out = id_repair.normalize(recs)
        amend = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amend["_amendment_to_id"] == "D000120"
        assert amend.get("_amendment_ambiguous") is True
        assert out["ambiguous_amendments"] == 1

    def test_hostless_amendment_is_flagged_not_silently_followed(self):
        """M6: a host-less amendment to a split base can't be attributed."""
        recs = [
            _base("D000120", "winner", ts="2026-01-01T10:00:00", host="aaa"),
            _base("D000120", "loser", ts="2026-01-01T10:05:00", host="bbb"),
            {"id": "D000120", "_amendment_to_id": "D000120", "is_outdated": True},
        ]
        out = id_repair.normalize(recs)
        amend = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amend["_amendment_to_id"] == "D000120"
        assert amend.get("_amendment_ambiguous") is True

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
