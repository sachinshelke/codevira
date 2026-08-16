"""4.0 Step 9 · S3a — a supersession chain that survives a two-host merge.

``id_repair`` renumbers a colliding base record and follows its amendments.
It did not touch decision-to-decision EDGES: before this change the module
contained zero references to ``superseded_by``.

So on a two-host merge the chain snapped. Alice supersedes D000120 with
D000121; Bob independently minted D000120 too; the repair renumbers Alice's
base to a content-derived id and her ``superseded_by`` edge is left pointing
at an id that now belongs to BOB. The visible symptom is a decision the team
retired months ago still reading as current, because nothing resolves the
edge to it any more.

The safety half matters as much: an edge that cannot be attributed must stay
put and be flagged, never guessed. A supersession aimed at the wrong
engineer's decision marks their work obsolete.
"""

from __future__ import annotations

import pytest

from mcp_server.storage import id_repair


ALICE = {"ide": "claude_code", "device_id": "aaaa1111"}
BOB = {"ide": "cursor", "device_id": "bbbb2222"}


def _base(rid: str, ts: str, origin: dict, **extra) -> dict:
    return {
        "id": rid,
        "ts": ts,
        "decision": f"decision {rid} {ts}",
        **extra,
        "origin": origin,
    }


def _two_host_merge() -> list[dict]:
    """Bob wins D000120 on ts; Alice's copy is renumbered.

    Alice had already superseded HER D000120 with D000130, so her
    ``superseded_by`` edge must follow the renumber.
    """
    return [
        _base("D000120", "2026-07-13T09:00:00+00:00", BOB),
        _base("D000120", "2026-07-13T10:00:00+00:00", ALICE, superseded_by="D000130"),
        _base("D000130", "2026-07-20T09:00:00+00:00", ALICE),
    ]


class TestTheChainSurvives:
    def test_a_superseded_by_edge_follows_its_renumbered_base(self) -> None:
        """The done-when for this landing."""
        out = id_repair.normalize(_two_host_merge())

        alice_new = next(
            r["new_id"] for r in out["remap"] if r["loser_host"] == "aaaa1111"
        )
        alice = next(r for r in out["records"] if r["id"] == alice_new)

        # D000130 did not collide, so the edge still points at it — what
        # moved is the record holding the edge, and it kept the edge intact.
        assert alice["superseded_by"] == "D000130"
        assert not alice.get("_reference_ambiguous")

    def test_an_edge_pointing_at_a_split_id_is_repointed(self) -> None:
        """The real break: the TARGET of the edge is what got renumbered."""
        recs = [
            # Both hosts minted D000200.
            _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
            _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
            # Alice later retires HER D000200 in favour of D000201.
            _base("D000201", "2026-07-20T09:00:00+00:00", ALICE, supersedes="D000200"),
        ]
        out = id_repair.normalize(recs)

        alice_old = next(
            r["new_id"] for r in out["remap"] if r["loser_host"] == "aaaa1111"
        )
        successor = next(r for r in out["records"] if r["id"] == "D000201")

        assert successor["supersedes"] == alice_old, (
            "the edge must follow Alice's renumbered record, not stay on Bob's"
        )
        assert not successor.get("_reference_ambiguous")

    def test_the_edge_never_silently_lands_on_the_other_engineer(self) -> None:
        """Without the rewrite the pointer keeps its literal value, which is
        now Bob's record. Assert the value CHANGED, so a regression that
        drops the rewrite fails here rather than passing vacuously."""
        out = id_repair.normalize(
            [
                _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
                _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
                _base(
                    "D000201", "2026-07-20T09:00:00+00:00", ALICE, supersedes="D000200"
                ),
            ]
        )
        successor = next(r for r in out["records"] if r["id"] == "D000201")
        assert successor["supersedes"] != "D000200"


class TestItNeverGuesses:
    def test_an_unattributable_edge_stays_put_and_is_flagged(self) -> None:
        """A third machine's edge into a contested id cannot be attributed."""
        carol = {"ide": "codex", "device_id": "cccc3333"}
        out = id_repair.normalize(
            [
                _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
                _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
                _base(
                    "D000201", "2026-07-20T09:00:00+00:00", carol, supersedes="D000200"
                ),
            ]
        )
        successor = next(r for r in out["records"] if r["id"] == "D000201")
        assert successor["supersedes"] == "D000200"  # left on the winner
        assert successor["_reference_ambiguous"] is True
        assert out["ambiguous_references"] == 1

    def test_a_pre_4_0_edge_with_no_origin_is_flagged_not_moved(self) -> None:
        out = id_repair.normalize(
            [
                _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
                _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
                {
                    "id": "D000201",
                    "ts": "2026-07-20T09:00:00+00:00",
                    "decision": "no origin, as every pre-4.0 record has",
                    "supersedes": "D000200",
                },
            ]
        )
        successor = next(r for r in out["records"] if r["id"] == "D000201")
        assert successor["supersedes"] == "D000200"
        assert successor["_reference_ambiguous"] is True


class TestItLeavesEverythingElseAlone:
    """This runs at EVERY server start on every registered project. The
    dominant risk is not failing to fix — it is touching what was fine."""

    def test_a_store_with_no_collisions_is_returned_untouched(self) -> None:
        clean = [
            _base(
                "D000100", "2026-07-13T09:00:00+00:00", ALICE, superseded_by="D000101"
            ),
            _base("D000101", "2026-07-14T09:00:00+00:00", ALICE),
        ]
        out = id_repair.normalize([dict(r) for r in clean])
        assert out["records"] == clean
        assert out["collisions"] == 0
        assert out["ambiguous_references"] == 0

    def test_an_edge_to_an_uncontested_id_is_not_rewritten(self) -> None:
        """Only ids that were actually SPLIT are candidates."""
        out = id_repair.normalize(
            [
                _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
                _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
                _base(
                    "D000300", "2026-07-20T09:00:00+00:00", ALICE, supersedes="D000999"
                ),
            ]
        )
        rec = next(r for r in out["records"] if r["id"] == "D000300")
        assert rec["supersedes"] == "D000999"
        assert not rec.get("_reference_ambiguous")

    @pytest.mark.parametrize("bad", [None, 7, [], {}, ""])
    def test_a_non_string_edge_does_not_raise(self, bad) -> None:
        out = id_repair.normalize(
            [
                _base("D000200", "2026-07-13T09:00:00+00:00", BOB),
                _base("D000200", "2026-07-13T10:00:00+00:00", ALICE),
                _base("D000300", "2026-07-20T09:00:00+00:00", ALICE, supersedes=bad),
            ]
        )
        assert len(out["records"]) == 3

    def test_no_record_is_ever_lost(self) -> None:
        recs = _two_host_merge()
        out = id_repair.normalize(recs)
        assert len(out["records"]) == len(recs)
        assert len({r["id"] for r in out["records"]}) == len(recs)


class TestConvergence:
    def test_still_a_fixed_point_with_references(self) -> None:
        """normalize(normalize(x)) == normalize(x). Reference rewriting must
        not re-open the loop — this runs on every server start, so an
        oscillation would rewrite the file forever."""
        once = id_repair.normalize(_two_host_merge())["records"]
        twice = id_repair.normalize(once)["records"]
        assert once == twice

    def test_two_machines_compute_the_same_result(self) -> None:
        """Convergence with zero coordination: input order differs (git
        union-merge orders by branch), output must not."""
        recs = _two_host_merge()
        a = id_repair.normalize(recs)["records"]
        b = id_repair.normalize(list(reversed(recs)))["records"]
        assert sorted(r["id"] for r in a) == sorted(r["id"] for r in b)
        by_a = {r["id"]: r.get("superseded_by") for r in a}
        by_b = {r["id"]: r.get("superseded_by") for r in b}
        assert by_a == by_b

    def test_the_order_version_is_reported(self) -> None:
        """Two machines on different versions converge to different files.
        Callers need to be able to see that rather than silently disagree."""
        assert id_repair.normalize([])["order_version"] == id_repair.ORDER_VERSION
