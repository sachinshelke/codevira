"""
Tests for mcp_server/storage/reconcile.py — Phase 29 shared clustering core.

Also asserts backward-compat: check_conflict still resolves the primitives
(it re-imports from here). consensus_store was removed in 4.0.
"""

from __future__ import annotations

from mcp_server.storage import reconcile


class TestClassify:
    def test_near_identical_is_duplicate(self):
        # No stemming: choose text that genuinely shares content tokens.
        c = reconcile.classify(
            "hash passwords with bcrypt algorithm",
            "hash passwords with bcrypt",
        )
        assert c["kind"] == reconcile.KIND_DUPLICATE
        assert c["similarity"] >= 0.6

    def test_terse_contradiction_of_protected_is_conflict(self):
        # The canonical asymmetric-overlap case (only fires vs protected).
        c = reconcile.classify(
            "AgentStore should switch from pnpm to npm",
            "AgentStore uses pnpm workspaces do not switch package manager",
            b_protected=True,
        )
        assert c["kind"] == reconcile.KIND_CONFLICT

    def test_same_overlap_not_conflict_when_unprotected(self):
        c = reconcile.classify(
            "AgentStore should switch from pnpm to npm",
            "AgentStore uses pnpm workspaces do not switch package manager",
            b_protected=False,
        )
        # Not a duplicate (low Jaccard) and not a conflict (b not protected).
        assert c["kind"] == reconcile.KIND_DISTINCT

    def test_unrelated_is_distinct(self):
        c = reconcile.classify(
            "use bcrypt for password hashing",
            "render the invoice pdf with a monospace font",
        )
        assert c["kind"] == reconcile.KIND_DISTINCT


class TestReconcileCandidate:
    def test_partitions_and_ranks_deterministically(self):
        corpus = [
            {
                "id": "D1",
                "decision": "hash passwords with bcrypt algorithm",
                "do_not_revert": False,
            },
            {
                "id": "D2",
                "decision": "store invoices in postgres",
                "do_not_revert": False,
            },
            {
                "id": "D3",
                "decision": "never switch package manager away from pnpm",
                "do_not_revert": True,
            },
        ]
        out = reconcile.reconcile_candidate("hash passwords with bcrypt", corpus)
        dup_ids = [d["id"] for d in out["duplicates"]]
        assert "D1" in dup_ids
        assert "D2" not in dup_ids

    def test_empty_corpus(self):
        out = reconcile.reconcile_candidate("anything", [])
        assert out == {"duplicates": [], "conflicts": []}

    def test_deterministic_order_independent(self):
        corpus = [
            {"id": "D1", "decision": "use bcrypt to hash passwords"},
            {"id": "D2", "decision": "bcrypt password hashing everywhere"},
        ]
        a = reconcile.reconcile_candidate("bcrypt password hashing", corpus)
        b = reconcile.reconcile_candidate(
            "bcrypt password hashing", list(reversed(corpus))
        )
        assert [d["id"] for d in a["duplicates"]] == [d["id"] for d in b["duplicates"]]


class TestBackwardCompat:
    def test_check_conflict_still_exports_primitives(self):
        from mcp_server.tools import check_conflict as cc

        assert cc._DUP_THRESHOLD == reconcile._DUP_THRESHOLD
        assert cc._tokenize("hash the password") == reconcile._tokenize(
            "hash the password"
        )

    def test_consensus_store_is_gone(self):
        """4.0 subtraction: the consensus subsystem was removed (6 MCP
        tools, 1 CLI subcommand, ~1,829 LOC, zero data files in any
        project). reconcile.py remains the shared clustering core for
        check_conflict — that re-export chain is asserted above.
        """
        import pytest

        with pytest.raises(ImportError):
            from mcp_server.storage import consensus_store  # noqa: F401
