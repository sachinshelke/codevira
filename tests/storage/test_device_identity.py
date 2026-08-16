"""4.0 Step 9 · S1 — a machine identity that survives the network stack.

``host_hash`` was documented as "stable per machine across reboots". It is
derived from ``uuid.getnode()``, which returns whichever MAC-bearing
interface enumerates first — and a dev laptop has many (16 on the machine
this was found on). A VPN connecting or Docker starting re-identifies the
machine.

Measured on codevira's own store before this fix: 4 distinct ``host_hash``
values from ONE machine across 7 weeks, two of them live concurrently.

That is not cosmetic. ``id_repair`` keys amendment-following on writer
identity, so a developer whose VPN reconnected looked like a stranger to
their own earlier decision — and their amendment was attributed to whoever
won the id instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server.storage import id_repair, origin


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A private ~/.codevira so tests never mint into the real one."""
    home = tmp_path / "global"
    home.mkdir()
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: home)
    monkeypatch.delenv("CODEVIRA_DEVICE_ID", raising=False)
    origin._persisted_device_id.cache_clear()
    origin._host_hash.cache_clear()
    yield home
    origin._persisted_device_id.cache_clear()
    origin._host_hash.cache_clear()


def _simulate_interface_change(monkeypatch: pytest.MonkeyPatch, mac: int) -> None:
    """A VPN comes up; uuid.getnode() now reports a different interface."""
    monkeypatch.setattr("uuid.getnode", lambda: mac)
    origin._host_hash.cache_clear()


class TestIdentitySurvivesTheNetworkStack:
    def test_a_new_interface_does_not_change_the_device_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE regression. host_hash moves; device_id must not."""
        _simulate_interface_change(monkeypatch, 0x36D9C29C34B9)
        before = origin.current_origin()

        _simulate_interface_change(monkeypatch, 0xAABBCCDDEEFF)
        origin._persisted_device_id.cache_clear()  # as if a new process
        after = origin.current_origin()

        assert before["host_hash"] != after["host_hash"], (
            "test is vacuous unless the legacy identity actually drifts"
        )
        assert before["device_id"] == after["device_id"]

    def test_it_is_persisted_where_a_reinstall_will_find_it(
        self, isolated_home: Path
    ) -> None:
        got = origin.device_id()
        stored = (isolated_home / "device_id").read_text().strip()
        assert stored == got

    def test_a_second_process_reads_the_same_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just cached in-process — actually durable on disk.

        Uses a real HOME rather than a monkeypatch so this exercises the
        same resolution a shipped install does.
        """
        fake_home = tmp_path / "home"
        (fake_home / ".codevira").mkdir(parents=True)
        # This process is pointed at the dir directly; the child resolves
        # the SAME dir through the real Path.home() code path via HOME.
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: fake_home / ".codevira"
        )
        origin._persisted_device_id.cache_clear()

        first = origin.device_id()
        assert (fake_home / ".codevira" / "device_id").is_file()

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mcp_server.storage import origin; print(origin.device_id())",
            ],
            cwd=Path(__file__).resolve().parents[2],
            # CODEVIRA_HOME must be dropped, not just overridden: conftest
            # sets it suite-wide and it takes precedence over HOME, so
            # leaving it in would point the child at the shared test home
            # rather than this test's.
            env={
                **{k: v for k, v in os.environ.items() if k != "CODEVIRA_HOME"},
                "HOME": str(fake_home),
            },
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == first

    def test_it_is_not_derived_from_anything(self, isolated_home: Path) -> None:
        """Random by design: derived identity is what broke. Two fresh
        homes on the same machine must not agree."""
        a = origin.device_id()
        (isolated_home / "device_id").unlink()
        origin._persisted_device_id.cache_clear()
        assert origin.device_id() != a


class TestItNeverMakesAWriteFail:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEVIRA_DEVICE_ID", "ci-runner-7")
        assert origin.device_id() == "ci-runner-7"

    def test_env_override_does_not_write_a_file(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_DEVICE_ID", "ci-runner-7")
        origin.device_id()
        assert not (isolated_home / "device_id").exists()

    def test_unwritable_home_falls_back_to_the_legacy_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Degrade to exactly today's behaviour rather than raising on a
        write path that records every decision."""

        def boom():
            raise OSError("read-only filesystem")

        monkeypatch.setattr("mcp_server.paths.get_global_home", boom)
        origin._persisted_device_id.cache_clear()
        assert origin.device_id() == origin._host_hash()

    def test_a_corrupt_file_does_not_raise(self, isolated_home: Path) -> None:
        (isolated_home / "device_id").write_bytes(b"\xff\xfe\x00garbage")
        origin._persisted_device_id.cache_clear()
        assert isinstance(origin.device_id(), str)


class TestBackwardCompatibility:
    def test_host_hash_is_still_written(self) -> None:
        """A 4.0 record must stay readable by a 3.x reader that only
        knows host_hash."""
        assert origin.current_origin()["host_hash"]

    def test_old_records_still_compare_by_host_hash(self) -> None:
        """Pre-4.0 records have no device_id. Two of them from the same
        machine must still match each other exactly as they did."""
        old = {"host_hash": "e7e6ab0029f7", "ide": "claude_code"}
        assert origin.writer_id(old) == "e7e6ab0029f7"
        assert origin.writer_id(old) == origin.writer_id(dict(old))

    def test_device_id_wins_when_both_are_present(self) -> None:
        rec = {"device_id": "d0", "host_hash": "h0"}
        assert origin.writer_id(rec) == "d0"

    @pytest.mark.parametrize("bad", [None, {}, "not-a-dict", 7, {"ide": "x"}])
    def test_missing_origin_yields_empty_not_an_error(self, bad) -> None:
        assert origin.writer_id(bad) == ""


class TestTheBugThisActuallyFixes:
    """id_repair attributing an amendment to the wrong engineer."""

    #: Alice writes SECOND, so she loses the id race and her decision is
    #: renumbered. That is the case where following matters: the winner
    #: keeps D000120 either way, so only a loser's amendment can be
    #: mis-attributed.
    ALICE = {"ide": "claude_code", "device_id": "aaaa1111", "host_hash": "mac-A1"}
    BOB = {"ide": "cursor", "device_id": "bbbb2222", "host_hash": "mac-B1"}

    @classmethod
    def _store(cls, author_of_amendment: dict) -> list[dict]:
        """Two engineers both minted D000120 on separate branches; the
        merge is clean, so read_merged would silently drop one."""
        return [
            {
                "id": "D000120",
                "ts": "2026-07-13T09:00:00+00:00",
                "decision": "Bob: drop the retry wrapper",
                "origin": cls.BOB,
            },
            {
                "id": "D000120",
                "ts": "2026-07-13T10:00:00+00:00",
                "decision": "Alice: pin the cache TTL at 30s",
                "origin": cls.ALICE,
            },
            {
                "id": "D000120",
                "_amendment_to_id": "D000120",
                "ts": "2026-07-25T09:00:00+00:00",
                "is_protected": True,
                "origin": author_of_amendment,
            },
        ]

    def test_an_amendment_follows_its_base_after_the_mac_changed(self) -> None:
        """Alice's VPN reconnected between recording the decision and
        protecting it, so her host_hash moved mac-A1 -> mac-A2 while her
        device_id did not. The amendment still finds her decision.

        Without S1 this is not merely unresolved — it is SILENT. The
        drifted host matches no loser key, so the ambiguity branch never
        fires, no flag is set, and `is_protected: true` lands on BOB's
        decision instead. See the companion test below.
        """
        amendment_author = {
            "ide": "claude_code",
            "device_id": "aaaa1111",  # unchanged
            "host_hash": "mac-A2",  # drifted
        }
        out = id_repair.normalize(self._store(amendment_author))

        alice_new = next(
            r["new_id"] for r in out["remap"] if r["loser_host"] == "aaaa1111"
        )
        amendment = next(r for r in out["records"] if r.get("_amendment_to_id"))

        assert amendment["_amendment_to_id"] == alice_new
        assert not amendment.get("_amendment_ambiguous")

    def test_the_pre_s1_failure_was_silent_which_is_why_order_matters(self) -> None:
        """Documents the failure S1 removes, using ONLY the legacy field.

        A drifted host matches no loser key AND is non-empty, so the
        record sails past both the follow branch and the flag branch:
        Bob's decision gets marked protected with nothing recording that
        anything was guessed. This is why device_id had to land BEFORE
        origin is stamped on amendments — stamping first would have
        turned today's flagged ambiguity into exactly this.
        """
        legacy_only = {
            "id": "D000120",
            "_amendment_to_id": "D000120",
            "ts": "2026-07-25T09:00:00+00:00",
            "is_protected": True,
            "origin": {"ide": "claude_code", "host_hash": "mac-A2"},  # no device_id
        }
        recs = self._store({})
        recs[2] = legacy_only
        # Strip device_id from the bases too — a wholly pre-4.0 store.
        for r in recs[:2]:
            r["origin"] = {k: v for k, v in r["origin"].items() if k != "device_id"}

        out = id_repair.normalize(recs)
        amendment = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amendment["_amendment_to_id"] == "D000120"  # -> the winner, Bob
        assert not amendment.get("_amendment_ambiguous"), (
            "if this ever starts flagging, the silent-attribution hazard "
            "is gone and this test should become a stricter assertion"
        )

    def test_a_genuinely_foreign_amendment_is_still_flagged_not_moved(self) -> None:
        """The safety property must survive the fix: an amendment from a
        machine that wrote neither base is never attributed by guess."""
        stranger = {"ide": "cursor", "device_id": "cccc3333"}
        out = id_repair.normalize(self._store(stranger))
        amendment = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amendment["_amendment_to_id"] == "D000120"  # stayed on the winner

    def test_a_pre_4_0_amendment_with_no_origin_is_still_flagged(self) -> None:
        """The 0-of-96 case in the live store today. It must keep landing
        on the conservative branch, not become attributable by accident."""
        out = id_repair.normalize(self._store({}))
        amendment = next(r for r in out["records"] if r.get("_amendment_to_id"))
        assert amendment.get("_amendment_ambiguous") is True

    def test_repair_is_still_a_fixed_point(self) -> None:
        """normalize(normalize(x)) == normalize(x) — the property that
        stops a re-merge from oscillating."""
        recs = self._store({"device_id": "aaaa1111", "host_hash": "mac-A2"})
        once = id_repair.normalize(recs)["records"]
        twice = id_repair.normalize(once)["records"]
        assert once == twice
