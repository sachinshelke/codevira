"""4.0 Step 9 · S3b — edges that name their target by content, not by id.

``_amendment_to_id`` is ambiguous the moment two machines mint the same id,
which is the entire reason ``id_repair`` has to infer an amendment's base
from ``(old_id, writer)``. That inference is careful — it flags rather than
guesses — but it still fails in cases where the answer is knowable:

* the amender and the base's author are genuinely different people
  (a teammate protecting your decision), and
* anything written before ``device_id`` existed.

A uid names exactly one record. An amendment carrying ``_amendment_to_uid``
needs no inference at all, so those cases resolve exactly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, id_repair, jsonl_store, paths, uid


ALICE = {"ide": "claude_code", "device_id": "aaaa1111"}
BOB = {"ide": "cursor", "device_id": "bbbb2222"}
CAROL = {"ide": "codex", "device_id": "cccc3333"}


def _base(rid: str, ts: str, origin: dict, **extra) -> dict:
    rec = {"id": rid, "ts": ts, "decision": f"d {rid} {ts}", **extra, "origin": origin}
    rec["uid"] = uid.compute(rec)
    return rec


class TestAUidEdgeResolvesWhatTheHeuristicCannot:
    def test_a_teammates_amendment_finds_the_right_base(self) -> None:
        """Carol protects ALICE's decision. The (old_id, writer) rule cannot
        attribute this — Carol wrote neither base — so pre-S3b it stayed on
        Bob's record and marked HIS decision do_not_revert."""
        alice = _base("D000120", "2026-07-13T10:00:00+00:00", ALICE)
        bob = _base("D000120", "2026-07-13T09:00:00+00:00", BOB)  # earlier, wins
        amendment = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "_amendment_to_uid": alice["uid"],
            "ts": "2026-07-25T09:00:00+00:00",
            "do_not_revert": True,
            "origin": CAROL,
        }

        out = id_repair.normalize([bob, alice, amendment])
        alice_new = next(
            r["new_id"] for r in out["remap"] if r["loser_host"] == "aaaa1111"
        )
        amended = next(r for r in out["records"] if r.get("_amendment_to_id"))

        assert amended["_amendment_to_id"] == alice_new
        assert not amended.get("_amendment_ambiguous")

    def test_without_the_uid_edge_the_same_case_is_flagged(self) -> None:
        """The contrast that shows the uid edge is doing the work — and
        that the fallback still refuses to guess."""
        alice = _base("D000120", "2026-07-13T10:00:00+00:00", ALICE)
        bob = _base("D000120", "2026-07-13T09:00:00+00:00", BOB)
        amendment = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "ts": "2026-07-25T09:00:00+00:00",
            "do_not_revert": True,
            "origin": CAROL,
        }
        out = id_repair.normalize([bob, alice, amendment])
        amended = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amended["_amendment_to_id"] == "D000120"  # left on the winner

    def test_a_uid_naming_the_winner_leaves_the_edge_alone(self) -> None:
        """The winner keeps its id, so there is nothing to redirect."""
        alice = _base("D000120", "2026-07-13T10:00:00+00:00", ALICE)
        bob = _base("D000120", "2026-07-13T09:00:00+00:00", BOB)
        amendment = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "_amendment_to_uid": bob["uid"],
            "ts": "2026-07-25T09:00:00+00:00",
            "is_outdated": True,
            "origin": CAROL,
        }
        out = id_repair.normalize([bob, alice, amendment])
        amended = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amended["_amendment_to_id"] == "D000120"

    def test_an_unknown_uid_falls_back_rather_than_dropping(self) -> None:
        alice = _base("D000120", "2026-07-13T10:00:00+00:00", ALICE)
        bob = _base("D000120", "2026-07-13T09:00:00+00:00", BOB)
        amendment = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "_amendment_to_uid": "0000000000000000",
            "ts": "2026-07-25T09:00:00+00:00",
            "origin": ALICE,
        }
        out = id_repair.normalize([bob, alice, amendment])
        assert len(out["records"]) == 3
        amended = next(r for r in out["records"] if r.get("_amendment_to_id"))
        # Falls through to the writer heuristic, which CAN place this one.
        assert amended["_amendment_to_id"] != "D000120"

    def test_it_works_for_a_pre_4_0_base_with_no_stored_uid(self) -> None:
        """Derivation, not backfill: an old base has no uid on disk but
        yields the same one an amendment can point at."""
        alice_raw = {
            "id": "D000120",
            "ts": "2026-07-13T10:00:00+00:00",
            "decision": "legacy alice",
            "origin": ALICE,
        }
        bob = _base("D000120", "2026-07-13T09:00:00+00:00", BOB)
        amendment = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "_amendment_to_uid": uid.compute(alice_raw),
            "ts": "2026-07-25T09:00:00+00:00",
            "origin": CAROL,
        }
        out = id_repair.normalize([bob, alice_raw, amendment])
        alice_new = next(
            r["new_id"] for r in out["remap"] if r["loser_host"] == "aaaa1111"
        )
        amended = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amended["_amendment_to_id"] == alice_new


class TestIdentityIsNotOverwritten:
    def test_an_amendment_does_not_clobber_the_bases_uid(self, tmp_path: Path) -> None:
        """Found before shipping: the merged record reported the identity of
        the last thing that happened to it, so every uid-keyed edge pointing
        at that record resolved to nothing."""
        p = tmp_path / "s.jsonl"
        p.write_text(
            json.dumps({"id": "D1", "decision": "base", "uid": "BASE_UID"})
            + "\n"
            + json.dumps(
                {
                    "id": "D1",
                    "_amendment_to_id": "D1",
                    "is_outdated": True,
                    "uid": "AMEND_UID",
                }
            )
            + "\n"
        )
        merged = jsonl_store.read_merged(p)[0]
        assert merged["is_outdated"] is True
        assert merged["uid"] == "BASE_UID"


class TestOnTheWritePath:
    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "proj"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
        monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
        monkeypatch.chdir(root)
        paths.ensure_dirs(root)
        (root / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
        decisions_store.invalidate_merged_cache()
        return root

    def _rows(self, root: Path) -> list[dict]:
        p = root / ".codevira" / "decisions.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def test_every_amendment_names_its_base_by_uid(self, project: Path) -> None:
        did = decisions_store.record("subject")
        decisions_store.mark_protected(did)
        decisions_store.reaffirm(did)
        decisions_store.supersede(did, "successor", reason="r")

        amendments = [r for r in self._rows(project) if r.get("_amendment_to_id")]
        assert amendments
        assert all(a.get("_amendment_to_uid") for a in amendments)

    def test_the_edge_points_at_the_real_base(self, project: Path) -> None:
        did = decisions_store.record("subject")
        decisions_store.mark_protected(did)
        rows = self._rows(project)
        base = next(r for r in rows if not r.get("_amendment_to_id"))
        amendment = next(r for r in rows if r.get("_amendment_to_id"))
        assert amendment["_amendment_to_uid"] == base["uid"]

    def test_a_second_amendment_still_names_the_base_not_the_first(
        self, project: Path
    ) -> None:
        """After one amendment lands, get() returns a merged record. The
        edge must still be the BASE's uid — this is what the overlay guard
        protects."""
        did = decisions_store.record("subject")
        decisions_store.mark_protected(did)
        decisions_store.reaffirm(did)
        rows = self._rows(project)
        base = next(r for r in rows if not r.get("_amendment_to_id"))
        amendments = [r for r in rows if r.get("_amendment_to_id")]
        assert {a["_amendment_to_uid"] for a in amendments} == {base["uid"]}
