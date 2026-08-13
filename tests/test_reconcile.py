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
        """Two entries with EQUAL similarity, so the ``(-similarity, id)`` sort
        is genuinely contested.

        The earlier version of this test used texts of differing similarity:
        the ranks were already distinct, nothing ever tied, and it would have
        passed against a sort that fell back to input order. Identical texts
        force the tiebreak to do the work.
        """
        corpus = [
            {"id": "D9", "decision": "bcrypt password hashing"},
            {"id": "D1", "decision": "bcrypt password hashing"},
            {"id": "D5", "decision": "bcrypt password hashing"},
        ]
        import itertools

        seen = {
            tuple(
                d["id"]
                for d in reconcile.reconcile_candidate(
                    "bcrypt password hashing", list(p)
                )["duplicates"]
            )
            for p in itertools.permutations(corpus)
        }
        assert seen == {
            ("D1", "D5", "D9")
        }, f"every permutation must rank identically; got {seen}"


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


class TestPickCanonical:
    """Phase 29's second clause: given a duplicate cluster, return ONE
    canonical record chosen deterministically, the union of what the merged
    records carried, and an alias map so ``[[Dxxxx]]`` references still
    resolve after the merge.

    The winner order must be TOTAL. A cluster is merged independently on every
    machine that holds it, so any tie broken by list position makes two
    engineers converge on different files — the same defect fixed in
    ``id_repair`` this release. The order here deliberately mirrors that one.

    The committed text is always one of the input records. An LLM may suggest
    a merged wording, but committing synthesised text would make the result a
    function of sampling rather than of content, and convergence would be gone.
    """

    def _rec(self, id_, text, ts, host="h0", dnr=False, tags=None):
        return {
            "id": id_,
            "decision": text,
            "ts": ts,
            "do_not_revert": dnr,
            "tags": tags or [],
            "origin": {"host_hash": host},
        }

    def test_earliest_record_wins_and_the_rest_alias_to_it(self):
        cluster = [
            self._rec("D9", "hash passwords with bcrypt", "2026-03-01T00:00:00Z"),
            self._rec("D2", "hash passwords with bcrypt", "2026-01-01T00:00:00Z"),
            self._rec("D5", "hash passwords with bcrypt", "2026-02-01T00:00:00Z"),
        ]
        out = reconcile.pick_canonical(cluster)
        assert out["canonical_id"] == "D2", (
            "the oldest is the id other decisions are most likely to reference, "
            "so keeping it minimises alias churn"
        )
        assert out["aliases"] == {"D9": "D2", "D5": "D2"}

    def test_a_protected_decision_is_never_merged_away(self):
        """A do_not_revert record outranks an older unprotected one. Merging a
        lock into an unlocked record would silently drop the protection."""
        cluster = [
            self._rec("D1", "never change the wire format", "2026-01-01T00:00:00Z"),
            self._rec(
                "D7", "never change the wire format", "2026-06-01T00:00:00Z", dnr=True
            ),
        ]
        out = reconcile.pick_canonical(cluster)
        assert out["canonical_id"] == "D7"
        assert out["protected"] is True
        assert out["aliases"] == {"D1": "D7"}

    def test_two_protected_members_disagreeing_is_flagged_not_resolved(self):
        """Two locks with different text is a real conflict. Pick
        deterministically so the run is reproducible, but say so — silently
        choosing one is exactly what the governing rule forbids."""
        cluster = [
            self._rec(
                "D1", "never change the wire format", "2026-01-01T00:00:00Z", dnr=True
            ),
            self._rec(
                "D2", "always change the wire format", "2026-02-01T00:00:00Z", dnr=True
            ),
        ]
        out = reconcile.pick_canonical(cluster)
        assert out["ambiguous"] is True
        assert out["canonical_id"] == "D1"

    def test_tags_and_provenance_are_unioned(self):
        cluster = [
            self._rec(
                "D1", "x", "2026-01-01T00:00:00Z", host="h1", tags=["auth", "db"]
            ),
            self._rec(
                "D2", "x", "2026-02-01T00:00:00Z", host="h2", tags=["db", "perf"]
            ),
        ]
        out = reconcile.pick_canonical(cluster)
        assert out["tags"] == [
            "auth",
            "db",
            "perf",
        ], "sorted union, so it is order-free"
        assert {p.get("host_hash") for p in out["provenance"]} == {"h1", "h2"}

    def test_output_is_identical_across_every_permutation_INCLUDING_ties(self):
        """The assertion the older order test could not make.

        Every record here is identical but for its id, so ts, writer and
        content hash ALL tie — the case that finds a positional tiebreak. The
        previous test used texts of differing similarity, so the sort never
        contested anything and would have passed against a broken order.
        """
        import itertools

        cluster = [
            self._rec("D3", "same text", "2026-01-01T00:00:00Z"),
            self._rec("D1", "same text", "2026-01-01T00:00:00Z"),
            self._rec("D2", "same text", "2026-01-01T00:00:00Z"),
        ]
        results = {
            (o["canonical_id"], tuple(sorted(o["aliases"].items())))
            for o in (
                reconcile.pick_canonical(list(p))
                for p in itertools.permutations(cluster)
            )
        }
        assert (
            len(results) == 1
        ), f"all 6 permutations must agree; got {len(results)} distinct outcomes"

    def test_a_single_record_needs_no_merge(self):
        out = reconcile.pick_canonical([self._rec("D1", "x", "2026-01-01T00:00:00Z")])
        assert out["canonical_id"] == "D1"
        assert out["aliases"] == {}
        assert out["ambiguous"] is False

    def test_empty_cluster_is_not_an_error(self):
        out = reconcile.pick_canonical([])
        assert out["canonical"] is None and out["aliases"] == {}
