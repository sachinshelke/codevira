"""4.0 Step 9 — amendments carry provenance, and never steal it.

Every mutation in codevira is an appended amendment row: mark_protected,
reaffirm, mark_outdated, set_flag, supersede. None of them recorded WHO
made the change — 0 of 96 amendments in the reference store carried an
``origin``.

That is what actually snaps a supersession chain on a two-host merge.
``id_repair`` renumbers a colliding base and uses ``(old_id, writer)`` to
decide which copy an amendment belongs to; with no writer it cannot
decide, so the amendment stays on whoever won the id race. Alice's
"superseded by D000002" lands on BOB's decision — his work reads as
retired, and hers reads as current.

The second half is the trap that makes this dangerous to fix carelessly:
``read_merged`` overlays amendment fields onto the base, so an amendment
carrying ``origin`` would rewrite the base's authorship. Mark someone
else's decision outdated and the record would then say you wrote it.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, id_repair, jsonl_store, paths


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _rows(root: Path) -> list[dict]:
    p = root / ".codevira" / "decisions.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _amendments(root: Path) -> list[dict]:
    return [r for r in _rows(root) if r.get("_amendment_to_id")]


class TestEveryMutationIsAttributable:
    """One test per amendment writer — a new one added without provenance
    reintroduces the merge bug silently."""

    def test_supersede(self, project: Path) -> None:
        did = decisions_store.record("original")
        decisions_store.supersede(did, "replacement", reason="better")
        assert all(a.get("origin", {}).get("device_id") for a in _amendments(project))

    def test_mark_protected(self, project: Path) -> None:
        did = decisions_store.record("lock me")
        decisions_store.mark_protected(did)
        assert _amendments(project)[-1]["origin"]["device_id"]

    def test_reaffirm(self, project: Path) -> None:
        did = decisions_store.record("still true")
        decisions_store.reaffirm(did)
        assert _amendments(project)[-1]["origin"]["device_id"]

    def test_mark_outdated(self, project: Path) -> None:
        did = decisions_store.record("no longer true")
        decisions_store.mark_outdated(did, reason="superseded by reality")
        assert _amendments(project)[-1]["origin"]["device_id"]

    def test_set_flag(self, project: Path) -> None:
        did = decisions_store.record("flag me")
        decisions_store.set_flag(did, tags=["x"])
        assert _amendments(project)[-1]["origin"]["device_id"]

    def test_no_amendment_writer_is_missed(self, project: Path) -> None:
        """Exercise every mutation and assert the population invariant —
        the thing that was 0/96 before this change."""
        did = decisions_store.record("subject")
        decisions_store.mark_protected(did)
        decisions_store.reaffirm(did)
        decisions_store.set_flag(did, tags=["a"])
        decisions_store.supersede(did, "successor", reason="r")

        amendments = _amendments(project)
        assert len(amendments) >= 4
        assert all("origin" in a for a in amendments), (
            f"{sum('origin' not in a for a in amendments)} amendment(s) still "
            "unattributable — they will mis-merge across two hosts"
        )


class TestAnAmendmentNeverStealsAuthorship:
    """The trap: read_merged overlays amendment fields onto the base."""

    def test_amending_does_not_rewrite_who_decided(self, project: Path) -> None:
        did = decisions_store.record("Alice's call")
        rows = _rows(project)
        base = next(r for r in rows if r["id"] == did and not r.get("_amendment_to_id"))
        base["origin"] = {"ide": "cursor", "device_id": "alice-machine"}
        (project / ".codevira" / "decisions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        decisions_store.invalidate_merged_cache()

        # Someone else marks it outdated from a different machine.
        decisions_store.mark_outdated(did, reason="moved on")
        decisions_store.invalidate_merged_cache()

        merged = decisions_store.get(did)
        assert merged["is_outdated"] is True, "the amendment must still apply"
        assert (
            merged["origin"]["device_id"] == "alice-machine"
        ), "authorship was rewritten to whoever amended it"

    def test_the_overlay_guard_holds_for_orphan_ordering(self, tmp_path: Path) -> None:
        """A git union-merge can place the amendment BEFORE its base.
        That branch re-applies the overlay and needs the same guard."""
        p = tmp_path / "s.jsonl"
        amendment = {
            "id": "D1",
            "_amendment_to_id": "D1",
            "is_outdated": True,
            "origin": {"device_id": "bob"},
        }
        base = {"id": "D1", "decision": "x", "origin": {"device_id": "alice"}}
        p.write_text(json.dumps(amendment) + "\n" + json.dumps(base) + "\n")

        merged = jsonl_store.read_merged(p)
        assert merged[0]["is_outdated"] is True
        assert merged[0]["origin"]["device_id"] == "alice"

    def test_a_base_without_origin_is_not_given_one(self, tmp_path: Path) -> None:
        """Unlike `ts`, there is no heal-if-missing. Filling in provenance
        from the amender asserts something nobody knows."""
        p = tmp_path / "s.jsonl"
        p.write_text(
            json.dumps({"id": "D1", "decision": "legacy, no origin"})
            + "\n"
            + json.dumps(
                {
                    "id": "D1",
                    "_amendment_to_id": "D1",
                    "is_outdated": True,
                    "origin": {"device_id": "bob"},
                }
            )
            + "\n"
        )
        merged = jsonl_store.read_merged(p)
        assert merged[0]["is_outdated"] is True
        assert merged[0].get("origin") is None

    def test_ts_still_heals_when_missing(self, tmp_path: Path) -> None:
        """Guard against over-applying the fix to the ts case, which
        deliberately DOES heal."""
        p = tmp_path / "s.jsonl"
        p.write_text(
            json.dumps({"id": "D1", "decision": "no ts"})
            + "\n"
            + json.dumps(
                {
                    "id": "D1",
                    "_amendment_to_id": "D1",
                    "ts": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )
        assert jsonl_store.read_merged(p)[0]["ts"] == "2026-01-01T00:00:00+00:00"


class TestTheTwoHostMerge:
    """The landing's done-when, in-process."""

    def test_a_supersession_chain_survives(self, project: Path) -> None:
        old = decisions_store.record("Cache TTL is 30s", context="stale reads")
        new = decisions_store.supersede(old, "Cache TTL is 5s", reason="stale")[
            "new_id"
        ]

        # Bob independently minted the same id on his branch; the git
        # union-merge combines both lines with no conflict.
        rows = _rows(project)
        bob = copy.deepcopy(
            next(r for r in rows if r["id"] == old and not r.get("_amendment_to_id"))
        )
        bob.update(
            decision="Bob: drop the retry wrapper",
            ts="2026-01-01T00:00:00+00:00",  # Bob is earlier, so Bob wins
            origin={"ide": "cursor", "device_id": "bob-machine"},
        )
        merged = [bob] + rows

        out = id_repair.normalize(merged)
        (project / ".codevira" / "decisions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in out["records"]) + "\n"
        )
        decisions_store.invalidate_merged_cache()

        assert out["collisions"] == 1
        assert (
            out["ambiguous_amendments"] == 0
        ), "the supersession could not be attributed — this is the bug"

        alice_new = out["remap"][0]["new_id"]
        alice, bobs = decisions_store.get(alice_new), decisions_store.get(old)

        assert alice["superseded_by"] == new, "Alice's chain must still resolve"
        assert not bobs.get("is_superseded"), "Bob's decision must NOT be retired"
        assert bobs["origin"]["device_id"] == "bob-machine"
