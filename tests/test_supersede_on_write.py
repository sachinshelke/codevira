"""
Phase 30 — supersede-on-write.

record_decision now SUPERSEDES a strong, unprotected near-duplicate instead of
appending a parallel twin (the write-side of the "outdated memory" complaint).
Protected duplicates are never auto-superseded; force / env opt out.
"""

from __future__ import annotations

from mcp_server.tools import learning
from mcp_server.storage import decisions_store

_A = "adopt bcrypt hashing for passwords"
_B = "adopt bcrypt hashing for passwords everywhere"


def _active_ids() -> set[str]:
    return {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}


def test_strong_unprotected_duplicate_is_superseded(monkeypatch):
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    r1 = learning.record_decision(decision=_A)
    id1 = r1["decision_id"]

    r2 = learning.record_decision(decision=_B)
    assert r2.get("superseded") == id1, "strong unprotected dup should be superseded"

    active = _active_ids()
    assert id1 not in active, "the superseded twin must no longer surface"
    assert r2["decision_id"] in active
    # No twin accumulation: exactly one active decision for this cluster.
    assert len(active) == 1


def test_protected_duplicate_is_not_auto_superseded(monkeypatch):
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    learning.record_decision(decision=_A, do_not_revert=True)

    r2 = learning.record_decision(decision=_B)
    assert "superseded" not in r2, "a protected decision must not be auto-superseded"
    assert r2["recorded"] is True
    assert r2.get("decision_id")


def test_force_records_a_twin(monkeypatch):
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    learning.record_decision(decision=_A)
    r2 = learning.record_decision(decision=_B, force=True)
    assert "superseded" not in r2
    assert len(_active_ids()) == 2, "force=True keeps both (a twin)"


def test_env_disable_restores_twin_behavior(monkeypatch):
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "0")
    learning.record_decision(decision=_A)
    r2 = learning.record_decision(decision=_B)
    assert "superseded" not in r2
    assert len(_active_ids()) == 2


def test_terse_subset_does_not_supersede_richer_decision(monkeypatch):
    """SB1: gating on symmetric jaccard (not inflated max(jaccard,overlap))
    means a terse token-subset must NOT supersede a richer superset decision."""
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    # jaccard = 4/6 = 0.667 (< 0.75), overlap = 4/4 = 1.0 -> old code superseded.
    learning.record_decision(
        decision="adopt bcrypt hashing passwords everywhere always"
    )
    r2 = learning.record_decision(decision="adopt bcrypt hashing passwords")
    assert "superseded" not in r2, "a terse subset must not hide the richer decision"
    assert len(_active_ids()) == 2


def test_supersede_preserves_symbol_and_counter_fields(monkeypatch):
    """SB1: the surviving decision must NOT lose symbol / context /
    alternatives_considered / would_re_examine_if when supersede-on-write fires."""
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    learning.record_decision(
        decision="use bcrypt algorithm for password hashing everywhere"
    )
    r2 = learning.record_decision(
        decision="use bcrypt algorithm for password hashing",  # jaccard 4/5=0.8
        file_path="auth.py",
        symbol="login",
        context="chose bcrypt over argon2 for FIPS reasons",
        alternatives_considered=["argon2id", "scrypt"],
        would_re_examine_if="if a FIPS-approved argon2 lands",
    )
    assert r2.get("superseded"), "jaccard 0.8 should trigger supersede"
    survivor = decisions_store.get(r2["decision_id"])
    assert survivor is not None
    assert survivor.get("symbol") == "login", "region lock silently became file-scoped"
    assert "FIPS" in (survivor.get("context") or "")
    assert "argon2id" in (survivor.get("alternatives_considered") or [])
    assert survivor.get("would_re_examine_if")


def test_distinct_decisions_are_not_superseded(monkeypatch):
    monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
    learning.record_decision(decision="use postgres for the invoice ledger")
    r2 = learning.record_decision(decision="render pdf receipts with a monospace font")
    assert "superseded" not in r2
    assert len(_active_ids()) == 2


class TestANegationIsNeverAutoSuperseded:
    """The reason 4.1.0 exists — asserted at the level that actually runs.

    The negation guard lives in ``reconcile.classify``, but ``check_conflict``
    re-implements the duplicate rule inline from the imported primitives and
    never calls it. So every negation test that drove ``reconcile`` directly
    passed while the WRITE PATH — record_decision -> check_conflict ->
    supersede-on-write — happily retired "never do X" as a duplicate of "do X".

    These drive ``record_decision`` end to end. They are RED at 2d6f885.
    """

    @staticmethod
    def _pair(monkeypatch, positive: str, negated: str):
        monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
        first = learning.record_decision(decision=positive)
        second = learning.record_decision(decision=negated)
        return first, second

    def test_never_does_not_supersede_its_positive(self, monkeypatch):
        first, second = self._pair(
            monkeypatch,
            "switch the package manager away from pnpm",
            "never switch the package manager away from pnpm",
        )
        assert second.get("superseded") is None, (
            "recording the NEGATION of a decision must not retire the original "
            f"— it is a contradiction, not a re-record (got {second.get('superseded')})"
        )
        active = _active_ids()
        assert first["decision_id"] in active, "the original must stay live"
        assert second["decision_id"] in active
        assert len(active) == 2

    def test_do_not_is_also_caught(self, monkeypatch):
        _, second = self._pair(
            monkeypatch,
            "auto-migrate the schema on startup",
            "do not auto-migrate the schema on startup",
        )
        assert second.get("superseded") is None
        assert len(_active_ids()) == 2

    def test_check_conflict_reports_a_negation_as_conflict_not_duplicate(
        self, monkeypatch
    ):
        """The tool description promises this in words; assert the behaviour."""
        from mcp_server.tools.check_conflict import check_conflict

        learning.record_decision(decision="expose the admin endpoint publicly")
        out = check_conflict(decision_text="never expose the admin endpoint publicly")
        assert out["status"] == "conflict", out
        assert out["conflicts"], "the negated pair belongs in conflicts"
        assert not out["duplicates"], (
            "a negated restatement must NOT be offered as a duplicate — that "
            "list is what supersede-on-write consumes"
        )

    def test_two_negations_are_still_duplicates(self, monkeypatch):
        """The guard keys on DISAGREEMENT, not on the presence of a negation."""
        monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "1")
        a = learning.record_decision(decision="never cache the invalidation path")
        b = learning.record_decision(
            decision="never cache the invalidation path anywhere"
        )
        assert b.get("superseded") == a["decision_id"], (
            "both sides negate, so they agree — still a duplicate"
        )
