"""
Tests for mcp_server/storage/decisions_store.py — v3.7.0 staleness read-side.

Covers the outdated-tombstone (mark_outdated) + surfacing filters that stop
stale decisions from dominating get_session_context / search / list_all.
"""

from __future__ import annotations

from mcp_server.storage import decisions_store


class TestMarkOutdated:
    """mark_outdated tombstones a decision out of default surfacing without
    deleting it — reversible via set_flag(is_outdated=False)."""

    def test_outdated_hidden_from_list_all(self):
        keep = decisions_store.record(decision="keep this current decision")
        stale = decisions_store.record(decision="this one is no longer true")

        res = decisions_store.mark_outdated(stale, reason="superseded by reality")
        assert res["success"] is True

        ids = {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}
        assert keep in ids
        assert stale not in ids, "outdated decision must not surface in list_all"

        # include_outdated=True still returns it (audit preserved, not deleted).
        ids_all = {
            d["id"]
            for d in decisions_store.list_all(limit=50, include_outdated=True)[
                "decisions"
            ]
        }
        assert stale in ids_all

    def test_outdated_hidden_from_search(self):
        did = decisions_store.record(
            decision="use redis for the ratelimiter cache layer"
        )
        decisions_store.mark_outdated(did)
        hits = decisions_store.search("redis ratelimiter cache", limit=10)
        assert all(
            h.get("decision_id") != did and h.get("id") != did for h in hits
        ), "outdated decision must not surface in search()"

    def test_set_flag_clears_outdated(self):
        did = decisions_store.record(decision="a decision that comes back")
        decisions_store.mark_outdated(did)
        assert did not in {
            d["id"] for d in decisions_store.list_all(limit=50)["decisions"]
        }

        # Un-retire it.
        decisions_store.set_flag(did, is_outdated=False)
        assert did in {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}

    def test_mark_outdated_unknown_id_fails_cleanly(self):
        res = decisions_store.mark_outdated("D999999")
        assert res["success"] is False
        assert "not found" in res["error"]


class TestOutdatedProtectionGate:
    """SB2: a do_not_revert decision must NOT be silently retired via the
    outdated tombstone — force is required, mirroring record_decision."""

    def test_mark_outdated_refuses_protected_without_force(self):
        did = decisions_store.record(
            decision="protected decision A", do_not_revert=True
        )
        res = decisions_store.mark_outdated(did)
        assert res["success"] is False
        assert res.get("do_not_revert") is True
        assert decisions_store.get(did).get("is_outdated") is not True

    def test_mark_outdated_force_overrides_protection(self):
        did = decisions_store.record(
            decision="protected decision B", do_not_revert=True
        )
        res = decisions_store.mark_outdated(did, force=True)
        assert res["success"] is True
        assert decisions_store.get(did).get("is_outdated") is True

    def test_set_flag_outdated_refuses_protected_without_force(self):
        did = decisions_store.record(
            decision="protected decision C", do_not_revert=True
        )
        res = decisions_store.set_flag(did, is_outdated=True)
        assert res["success"] is False
        assert decisions_store.get(did).get("is_outdated") is not True

    def test_clearing_outdated_is_always_allowed(self):
        did = decisions_store.record(
            decision="protected decision D", do_not_revert=True
        )
        decisions_store.mark_outdated(did, force=True)
        res = decisions_store.set_flag(did, is_outdated=False)
        assert res["success"] is True
        assert decisions_store.get(did).get("is_outdated") is False

    def test_unprotected_outdated_needs_no_force(self):
        did = decisions_store.record(decision="normal decision E")
        assert decisions_store.mark_outdated(did)["success"] is True

    def test_outdated_record_preserved_on_disk(self):
        """Tombstone is an amendment overlay — the original text survives."""
        did = decisions_store.record(decision="original text stays on disk")
        decisions_store.mark_outdated(did, reason="why")
        rec = decisions_store.get(did)
        assert rec is not None
        assert rec["decision"] == "original text stays on disk"
        assert rec.get("is_outdated") is True
