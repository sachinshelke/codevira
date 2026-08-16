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
        """The asymmetric-overlap detector is gated to PROTECTED decisions so
        it does not flood users with warnings about ordinary related work.

        The fixture changed in 4.0.1. It used to be "...do not switch package
        manager", which contains a negation — so it was also, accidentally,
        asserting that a decision and its contradiction may be ignored when
        neither is locked. That is not what this test is for, and the negation
        guard now (correctly) classifies such a pair as a conflict. This
        fixture keeps the original intent — high overlap, no contradiction —
        with the negation removed. See TestNegationIsNeverADuplicate.
        """
        c = reconcile.classify(
            "AgentStore should switch from pnpm to npm",
            "AgentStore uses pnpm workspaces for the package manager",
            b_protected=False,
        )
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
        assert seen == {("D1", "D5", "D9")}, (
            f"every permutation must rank identically; got {seen}"
        )


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
        assert len(results) == 1, (
            f"all 6 permutations must agree; got {len(results)} distinct outcomes"
        )

    def test_a_single_record_needs_no_merge(self):
        out = reconcile.pick_canonical([self._rec("D1", "x", "2026-01-01T00:00:00Z")])
        assert out["canonical_id"] == "D1"
        assert out["aliases"] == {}
        assert out["ambiguous"] is False

    def test_empty_cluster_is_not_an_error(self):
        out = reconcile.pick_canonical([])
        assert out["canonical"] is None and out["aliases"] == {}


class TestClusterStore:
    """Phase 25 Tier-1: cluster a whole store into merge groups + conflicts.

    Tier-0 (``id_repair``) resolves records that collide on an ID. Tier-1
    resolves records that say the same THING in different words — two
    engineers recording the same decision independently, each with its own id.

    Governing rule from the phase: the structural tier is authoritative and
    this one is assistive. It may merge provably-safe duplicates; it must
    NEVER auto-resolve a contradiction. Conflicts come back as data for the
    caller to surface, never as a silent pick.

    Escalation deliberately returns data rather than writing anywhere. The
    phase specified routing conflicts through ``consensus_store``, which was
    removed in 4.0 — and a pure function that hands back what it found is a
    better fit anyway: it keeps this layer free of I/O and lets the caller
    decide the surface.
    """

    def _rec(self, id_, text, ts="2026-01-01T00:00:00Z", dnr=False):
        return {
            "id": id_,
            "decision": text,
            "ts": ts,
            "do_not_revert": dnr,
            "tags": [],
            "origin": {"host_hash": "h0"},
        }

    def test_two_restatements_merge_and_a_third_is_left_alone(self):
        records = [
            self._rec("D1", "hash passwords with bcrypt algorithm"),
            self._rec("D2", "hash passwords with bcrypt"),
            self._rec("D3", "deploy the frontend to cloudflare pages"),
        ]
        out = reconcile.cluster_store(records)
        assert len(out["merges"]) == 1
        m = out["merges"][0]
        assert sorted(m["members"]) == ["D1", "D2"]
        assert "D3" not in m["members"], "an unrelated decision must not be swept in"

    def test_clusters_are_transitive(self):
        """A~C and B~C must land in ONE group even though A and B alone do NOT
        classify as duplicates — otherwise the same decision survives twice
        under two canonical ids.

        The linking pair here is deliberately NON-ADJACENT in id order: after
        the internal sort the members are D1, D2, D3, and the duplicate edges
        are D1~D3 (indices 0,2) and D2~D3 (1,2), while D1~D2 is distinct
        (Jaccard 0.50). An earlier fixture had all three mutually adjacent, so
        a union restricted to neighbouring pairs still passed it — mutation
        testing caught that. This one fails against any non-transitive
        implementation.
        """
        records = [
            self._rec(
                "D1", "hash passwords with the bcrypt algorithm today and always"
            ),
            self._rec("D2", "hash passwords with bcrypt"),
            self._rec("D3", "hash passwords with bcrypt algorithm"),
        ]
        assert (
            reconcile.classify(records[0]["decision"], records[1]["decision"])["kind"]
            == reconcile.KIND_DISTINCT
        ), "precondition: the two ends are NOT duplicates of each other"

        out = reconcile.cluster_store(records)
        assert len(out["merges"]) == 1
        assert sorted(out["merges"][0]["members"]) == ["D1", "D2", "D3"]

    def test_a_contradiction_is_reported_never_merged(self):
        records = [
            self._rec(
                "D1", "never switch the package manager away from pnpm", dnr=True
            ),
            self._rec("D2", "switch the package manager away from pnpm"),
        ]
        out = reconcile.cluster_store(records)
        assert out["merges"] == [], "a contradiction is not a duplicate"
        assert [c["ids"] for c in out["conflicts"]] == [["D1", "D2"]]

    def test_output_is_identical_across_permutations(self):
        import itertools

        records = [
            self._rec("D3", "hash passwords with bcrypt algorithm"),
            self._rec("D1", "hash passwords with bcrypt"),
            self._rec("D2", "hash passwords with bcrypt algorithm today"),
        ]
        seen = {
            (
                tuple(
                    (m["canonical_id"], tuple(sorted(m["members"])))
                    for m in reconcile.cluster_store(list(p))["merges"]
                )
            )
            for p in itertools.permutations(records)
        }
        assert len(seen) == 1, f"clustering must be order-free; got {len(seen)}"

    def test_a_protected_member_wins_its_cluster(self):
        records = [
            self._rec("D1", "hash passwords with bcrypt algorithm"),
            self._rec("D2", "hash passwords with bcrypt", dnr=True),
        ]
        out = reconcile.cluster_store(records)
        assert out["merges"][0]["canonical_id"] == "D2"
        assert out["merges"][0]["protected"] is True

    def test_the_pairwise_cap_is_reported_not_silent(self, caplog):
        """The scan is O(n^2). Bounding it is fine; bounding it silently is
        not — a truncated result that looks complete is worse than a slow one."""
        records = [self._rec(f"D{i}", f"decision number {i}") for i in range(40)]
        out = reconcile.cluster_store(records, max_records=10)
        assert out["truncated"] is True
        assert out["scanned"] == 10

    def test_no_duplicates_is_an_empty_plan_not_an_error(self):
        out = reconcile.cluster_store(
            [self._rec("D1", "alpha beta gamma"), self._rec("D2", "delta epsilon zeta")]
        )
        assert out == {"merges": [], "conflicts": [], "scanned": 2, "truncated": False}


class TestNegationIsNeverADuplicate:
    """A decision and its negation are lexically near-identical — they differ
    by one token — so pure set math scores them a duplicate. On this corpus:
    "never switch the package manager away from pnpm" vs the same sentence
    without "never" scores Jaccard 0.83, well over the 0.60 duplicate
    threshold.

    That is the worst possible false positive for this codebase. Tier-1 would
    alias one meaning away, and supersede-on-write — which consumes the same
    classifier — would RETIRE "never do X" as a duplicate of "do X". A
    protected decision is guarded there, but an unprotected one is not.

    A negation appearing on one side and not the other is therefore a
    conflict by construction, whatever the similarity says. It stays
    deterministic: a set difference against a fixed word list, no model.
    """

    PAIRS = [
        (
            "never switch the package manager away from pnpm",
            "switch the package manager away from pnpm",
        ),
        ("do not vendor the wasm toolchain", "vendor the wasm toolchain"),
        ("avoid using global mutable state", "using global mutable state"),
    ]

    def test_a_negated_pair_is_a_conflict_not_a_duplicate(self):
        for a, b in self.PAIRS:
            c = reconcile.classify(a, b)
            assert c["kind"] == reconcile.KIND_CONFLICT, (
                f"{a!r} vs {b!r} classified {c['kind']} (jaccard {c['jaccard']:.2f})"
            )

    def test_it_is_symmetric(self):
        a, b = self.PAIRS[0]
        assert reconcile.classify(a, b)["kind"] == reconcile.classify(b, a)["kind"]

    def test_both_sides_negated_is_still_a_duplicate(self):
        """The guard keys on DISAGREEMENT, not on the presence of a negation.
        Two decisions that both say 'never' are still restatements."""
        c = reconcile.classify(
            "never switch the package manager away from pnpm",
            "never switch package manager from pnpm",
        )
        assert c["kind"] == reconcile.KIND_DUPLICATE

    def test_tier1_reports_a_negated_pair_instead_of_merging_it(self):
        records = [
            {
                "id": "D1",
                "decision": "never switch the package manager away from pnpm",
                "ts": "2026-01-01T00:00:00Z",
            },
            {
                "id": "D2",
                "decision": "switch the package manager away from pnpm",
                "ts": "2026-02-01T00:00:00Z",
            },
        ]
        out = reconcile.cluster_store(records)
        assert out["merges"] == [], "a decision and its negation must never merge"
        assert [c["ids"] for c in out["conflicts"]] == [["D1", "D2"]]
