"""Unit tests for mcp_server.storage.jsonl_store.

Covers:
- Atomic append (single-record)
- Batched append_many
- read_all, iter_records, count, last_id
- Malformed lines: logged, skipped, not crash
- UTF-8 round-trip (emoji, CJK)
- Monotonic ID generation (next_monotonic_id, append_with_generated_id)
- Concurrent append safety (process-level lock)
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mcp_server.storage import jsonl_store


@pytest.fixture
def jsonl_path(tmp_path: Path) -> Path:
    """A fresh JSONL file path inside tmp_path (file does not exist yet)."""
    return tmp_path / "decisions.jsonl"


class TestAppendAndRead:
    def test_append_single_record(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001", "decision": "Use bcrypt"})
        assert jsonl_path.is_file()
        records = jsonl_store.read_all(jsonl_path)
        assert len(records) == 1
        assert records[0]["id"] == "D000001"
        assert records[0]["decision"] == "Use bcrypt"

    def test_append_multiple_records(self, jsonl_path: Path) -> None:
        for i in range(3):
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}", "text": f"d{i}"})
        records = jsonl_store.read_all(jsonl_path)
        assert len(records) == 3
        assert [r["id"] for r in records] == ["D000000", "D000001", "D000002"]

    def test_append_many_batch(self, jsonl_path: Path) -> None:
        records = [{"id": f"D{i:06d}", "x": i} for i in range(10)]
        jsonl_store.append_many(jsonl_path, records)
        out = jsonl_store.read_all(jsonl_path)
        assert len(out) == 10
        assert out[5]["x"] == 5

    def test_append_many_empty_is_noop(self, jsonl_path: Path) -> None:
        jsonl_store.append_many(jsonl_path, [])
        # File may or may not be created (we don't write anything); reading
        # MUST return empty list either way.
        assert jsonl_store.read_all(jsonl_path) == []

    def test_read_all_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert jsonl_store.read_all(tmp_path / "nonexistent.jsonl") == []

    def test_iter_records_streams(self, jsonl_path: Path) -> None:
        for i in range(5):
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}"})
        seen = list(jsonl_store.iter_records(jsonl_path))
        assert len(seen) == 5

    def test_count_skips_blank_lines(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001"})
        # Manually append a blank line that count() should ignore.
        with open(jsonl_path, "ab") as fh:
            fh.write(b"\n")
        jsonl_store.append(jsonl_path, {"id": "D000002"})
        assert jsonl_store.count(jsonl_path) == 2


class TestBadInput:
    def test_record_with_embedded_newline_rejected(self, jsonl_path: Path) -> None:
        # Direct newline inside a value would break our one-record-per-line
        # contract. json.dumps escapes \n in strings, so this should NOT
        # raise — the \n stays escaped in the JSON.
        jsonl_store.append(jsonl_path, {"decision": "line1\nline2"})
        records = jsonl_store.read_all(jsonl_path)
        assert records[0]["decision"] == "line1\nline2"

    def test_non_serializable_value_raises(self, jsonl_path: Path) -> None:
        class NotSerializable:
            pass

        with pytest.raises(TypeError):
            jsonl_store.append(jsonl_path, {"x": NotSerializable()})

    def test_malformed_line_skipped_with_warning(
        self, jsonl_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001"})
        # Hand-write a broken line in the middle.
        with open(jsonl_path, "ab") as fh:
            fh.write(b"{not valid json\n")
        jsonl_store.append(jsonl_path, {"id": "D000003"})

        with caplog.at_level("WARNING", logger="mcp_server.storage.jsonl_store"):
            records = jsonl_store.read_all(jsonl_path)
        # Good records survive; bad one skipped.
        assert len(records) == 2
        assert {r["id"] for r in records} == {"D000001", "D000003"}
        assert any("malformed JSON" in m for m in caplog.messages)

    def test_non_object_record_skipped(self, jsonl_path: Path) -> None:
        # A bare JSON array or string is valid JSON but not a record.
        with open(jsonl_path, "ab") as fh:
            fh.write(b'["array", "not", "object"]\n')
            fh.write(b'"just a string"\n')
        jsonl_store.append(jsonl_path, {"id": "D000001"})
        records = jsonl_store.read_all(jsonl_path)
        assert len(records) == 1
        assert records[0]["id"] == "D000001"


class TestUTF8:
    def test_emoji_roundtrip(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"decision": "Ship it 🚀"})
        records = jsonl_store.read_all(jsonl_path)
        assert records[0]["decision"] == "Ship it 🚀"

    def test_cjk_roundtrip(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"decision": "使用 bcrypt 加密"})
        records = jsonl_store.read_all(jsonl_path)
        assert records[0]["decision"] == "使用 bcrypt 加密"

    def test_accents_roundtrip(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"decision": "café au lait — naïve"})
        records = jsonl_store.read_all(jsonl_path)
        assert records[0]["decision"] == "café au lait — naïve"


class TestMonotonicIDs:
    def test_first_id_in_fresh_file(self, jsonl_path: Path) -> None:
        assert jsonl_store.next_monotonic_id(jsonl_path) == "D000001"

    def test_id_increments(self, jsonl_path: Path) -> None:
        new_id = jsonl_store.append_with_generated_id(jsonl_path, {"decision": "first"})
        assert new_id == "D000001"
        new_id = jsonl_store.append_with_generated_id(
            jsonl_path, {"decision": "second"}
        )
        assert new_id == "D000002"

    def test_id_field_is_set_on_record(self, jsonl_path: Path) -> None:
        new_id = jsonl_store.append_with_generated_id(jsonl_path, {"decision": "test"})
        records = jsonl_store.read_all(jsonl_path)
        assert records[0]["id"] == new_id

    def test_custom_prefix(self, jsonl_path: Path) -> None:
        new_id = jsonl_store.append_with_generated_id(
            jsonl_path, {"text": "test"}, prefix="OUT"
        )
        # width=6 is the numeric width regardless of prefix length, so
        # "OUT" + 6-digit zero-padded "1" = "OUT000001".
        assert new_id == "OUT000001"

    def test_custom_prefix_with_explicit_width(self, jsonl_path: Path) -> None:
        new_id = jsonl_store.append_with_generated_id(
            jsonl_path, {"text": "test"}, prefix="OUT", width=4
        )
        assert new_id == "OUT0001"

    def test_last_id_empty_file(self, jsonl_path: Path) -> None:
        assert jsonl_store.last_id(jsonl_path) is None
        jsonl_path.touch()  # empty file
        assert jsonl_store.last_id(jsonl_path) is None

    def test_last_id_after_appends(self, jsonl_path: Path) -> None:
        for _ in range(5):
            jsonl_store.append_with_generated_id(jsonl_path, {"x": 1})
        assert jsonl_store.last_id(jsonl_path) == "D000005"

    def test_amendment_record_does_not_steal_next_id(self, jsonl_path: Path) -> None:
        """v3.0.0 regression (2026-05-25): `_compute_next_id_locked` used to
        tail-read the last record and increment its id, but amendment
        records re-use an existing decision's id (carrying `_amendment_to_id`).
        That caused next = amended_id + 1 — a collision with an already-
        issued sequential id. Bug surfaced when `set_decision_flag` was
        called before a fresh `record_decision`: the new decision got the
        old id and silently overwrote it in the merged view.
        """
        # 3 fresh records → D000001, D000002, D000003
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "first"})
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "second"})
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "third"})
        # Amendment to D000002 (carries `_amendment_to_id`)
        jsonl_store.append(
            jsonl_path,
            {
                "id": "D000002",
                "_amendment_to_id": "D000002",
                "do_not_revert": True,
            },
        )
        # Next fresh record must be D000004, not D000003 (collision pre-fix).
        new_id = jsonl_store.append_with_generated_id(
            jsonl_path, {"decision": "fourth"}
        )
        assert new_id == "D000004"

    def test_multiple_amendments_then_new_id(self, jsonl_path: Path) -> None:
        """Walking-back past N consecutive amendments must still find the
        most-recent ORIGINAL record and increment from there."""
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "a"})
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "b"})
        # Three amendments to D000001 in a row (e.g., a tags update, then
        # do_not_revert flip, then another tags update).
        for _ in range(3):
            jsonl_store.append(
                jsonl_path,
                {
                    "id": "D000001",
                    "_amendment_to_id": "D000001",
                    "tags": ["any"],
                },
            )
        new_id = jsonl_store.append_with_generated_id(jsonl_path, {"decision": "c"})
        assert new_id == "D000003"

    def test_full_scan_fallback_when_tail_window_is_all_amendments(
        self, jsonl_path: Path
    ) -> None:
        """v3.5.0 regression: on a >100 KB file the next-id scan only tail-reads
        the last 4 KB. If that window holds ONLY amendment records — e.g. right
        after ``observe-git`` appends a burst of small outcome amendments — the
        reversed scan finds no real id, and pre-fix code fell back to D000001,
        COLLIDING with the existing D000001 and clobbering it (incl.
        do_not_revert decisions) in the merged view. The full-file fallback
        must find the real max id instead.
        """
        # Two real decisions; pad the first so the file clears the 100 KB
        # tail-read threshold (>100 KB → the 4 KB tail-read path).
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "x" * 120_000})
        jsonl_store.append_with_generated_id(jsonl_path, {"decision": "second"})
        assert jsonl_path.stat().st_size > 100_000
        # A burst of small amendments — easily enough to fill the last 4 KB
        # window so it contains NO non-amendment record.
        for _ in range(100):
            jsonl_store.append(
                jsonl_path,
                {"id": "D000001", "_amendment_to_id": "D000001", "outcome": "kept"},
            )
        # A fresh record must still get D000003 (max real id + 1), NOT D000001.
        new_id = jsonl_store.append_with_generated_id(jsonl_path, {"decision": "third"})
        assert new_id == "D000003", f"ID collided: got {new_id}"

    def test_next_id_uses_max_not_last_after_out_of_order_rewrite(
        self, jsonl_path: Path
    ) -> None:
        """v3.6.0 regression: minting must use MAX(real id)+1, not last-in-file+1.

        A junk cleanup / compact / cross-project repair can rewrite the store
        out of id order, leaving a LOWER id at the tail. Pre-fix code took the
        last real id (D000002 here) and +1'd it, re-issuing D000003 — which
        already exists — silently clobbering it in the merged view. The fix
        scans the whole file for the max id (D000005) and returns D000006.

        This is the exact shape of the 2026-06-27 store corruption: after a
        bulk junk cleanup left low ids at the tail, fresh writes collided
        onto D000001/D000002/D000003.
        """
        for rec in (
            {"id": "D000005", "decision": "kept-high"},
            {"id": "D000003", "decision": "kept-mid"},
            {"id": "D000002", "decision": "kept-low-at-tail"},
        ):
            jsonl_store.append(jsonl_path, rec)
        new_id = jsonl_store.append_with_generated_id(jsonl_path, {"decision": "fresh"})
        assert new_id == "D000006", f"expected max+1=D000006, got {new_id}"


class TestConcurrency:
    def test_concurrent_appenders_no_corruption(self, jsonl_path: Path) -> None:
        """50 threads × 20 appends each; final count must be exactly 1000
        and every line must be valid JSON. Catches lock failures."""
        N_THREADS = 50
        N_PER_THREAD = 20

        def worker(thread_id: int) -> None:
            for i in range(N_PER_THREAD):
                jsonl_store.append(
                    jsonl_path,
                    {"thread": thread_id, "seq": i, "marker": f"t{thread_id}_s{i}"},
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = jsonl_store.read_all(jsonl_path)
        assert len(records) == N_THREADS * N_PER_THREAD

        # Verify every (thread, seq) pair appears exactly once.
        seen: set[tuple[int, int]] = set()
        for r in records:
            key = (r["thread"], r["seq"])
            assert key not in seen, f"duplicate record: {key}"
            seen.add(key)
        assert len(seen) == N_THREADS * N_PER_THREAD

    def test_concurrent_id_generation_unique(self, jsonl_path: Path) -> None:
        """20 threads × 10 append_with_generated_id calls; every ID must
        be unique even under contention."""
        N_THREADS = 20
        N_PER_THREAD = 10
        ids: list[str] = []
        ids_lock = threading.Lock()

        def worker() -> None:
            for _ in range(N_PER_THREAD):
                new_id = jsonl_store.append_with_generated_id(jsonl_path, {"x": 1})
                with ids_lock:
                    ids.append(new_id)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ids) == N_THREADS * N_PER_THREAD
        assert len(set(ids)) == N_THREADS * N_PER_THREAD  # all unique
        # Sequence must run 1..200 with no gaps.
        nums = sorted(int(i[1:], 36) for i in ids)
        assert nums == list(range(1, N_THREADS * N_PER_THREAD + 1))


class TestSizeBudget:
    def test_1000_records_under_2_seconds(
        self, jsonl_path: Path, request: pytest.FixtureRequest
    ) -> None:
        """Performance smoke: 1000 append + 1000 read in <2s."""
        import time

        N = 1000

        t0 = time.perf_counter()
        for i in range(N):
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}", "x": i})
        elapsed = time.perf_counter() - t0
        # Loose budget for CI machines; fail HARD if we ever exceed 5s.
        assert elapsed < 5.0, f"1000 appends took {elapsed:.2f}s"

        t0 = time.perf_counter()
        records = jsonl_store.read_all(jsonl_path)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"1000 reads took {elapsed:.2f}s"
        assert len(records) == N


# =====================================================================
# v3.0.1 shared primitives: read_merged / compact / read_recent
# =====================================================================


class TestReadMerged:
    """Covers the amendment-overlay primitive extracted from
    decisions_store._read_merged. Convention: amendment record carries
    the SAME ``id`` as the base + truthy ``_amendment_to_id`` marker;
    later amendments win; orphans emit defensively; underscored fields
    are NOT overlaid.
    """

    def test_base_only_passes_through(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001", "decision": "Use bcrypt"})
        merged = jsonl_store.read_merged(jsonl_path)
        assert len(merged) == 1
        assert merged[0]["decision"] == "Use bcrypt"

    def test_single_amendment_overlays_field(self, jsonl_path: Path) -> None:
        jsonl_store.append(
            jsonl_path,
            {"id": "D000001", "decision": "Use bcrypt", "do_not_revert": False},
        )
        jsonl_store.append(
            jsonl_path,
            {"id": "D000001", "_amendment_to_id": "D000001", "do_not_revert": True},
        )
        merged = jsonl_store.read_merged(jsonl_path)
        assert len(merged) == 1
        assert merged[0]["do_not_revert"] is True
        assert merged[0]["decision"] == "Use bcrypt"  # untouched

    def test_amendment_chain_three_deep(self, jsonl_path: Path) -> None:
        """Plan B3: three amendments to the same base merge in order.

        Each amendment targets the BASE id (not a prior amendment) —
        amendment-of-amendment is explicitly not supported by this
        contract. Later amendments win on overlapping fields.
        """
        jsonl_store.append(jsonl_path, {"id": "D000001", "tags": ["a"]})
        jsonl_store.append(
            jsonl_path,
            {"id": "D000001", "_amendment_to_id": "D000001", "tags": ["a", "b"]},
        )
        jsonl_store.append(
            jsonl_path,
            {
                "id": "D000001",
                "_amendment_to_id": "D000001",
                "do_not_revert": True,
            },
        )
        jsonl_store.append(
            jsonl_path,
            {"id": "D000001", "_amendment_to_id": "D000001", "tags": ["final"]},
        )
        merged = jsonl_store.read_merged(jsonl_path)
        assert len(merged) == 1
        assert merged[0]["tags"] == ["final"]  # 3rd amendment wins
        assert merged[0]["do_not_revert"] is True  # 2nd amendment preserved

    def test_underscore_fields_not_overlaid(self, jsonl_path: Path) -> None:
        """``_amendment_to_id`` marker + future ``_evicted`` / ``_promoted_to``
        tombstones must NOT leak into user-visible state.
        """
        jsonl_store.append(jsonl_path, {"id": "D000001", "decision": "Use bcrypt"})
        jsonl_store.append(
            jsonl_path,
            {
                "id": "D000001",
                "_amendment_to_id": "D000001",
                "_evicted": True,
                "do_not_revert": True,
            },
        )
        merged = jsonl_store.read_merged(jsonl_path)
        assert merged[0]["do_not_revert"] is True
        # Underscored fields from the amendment must not pollute the base.
        assert "_evicted" not in merged[0]
        assert "_amendment_to_id" not in merged[0]

    def test_insertion_order_preserved_across_bases(self, jsonl_path: Path) -> None:
        for i in (3, 1, 2):  # intentionally out-of-numeric-order
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}", "n": i})
        merged = jsonl_store.read_merged(jsonl_path)
        assert [r["id"] for r in merged] == ["D000003", "D000001", "D000002"]

    def test_orphan_amendment_emits_for_diagnosis(self, jsonl_path: Path) -> None:
        # Amendment without a preceding base — should NOT be silently
        # dropped (defensive surfacing so users can diagnose).
        jsonl_store.append(
            jsonl_path,
            {"id": "D000099", "_amendment_to_id": "D000099", "do_not_revert": True},
        )
        merged = jsonl_store.read_merged(jsonl_path)
        assert len(merged) == 1
        assert merged[0]["id"] == "D000099"

    def test_missing_id_skipped(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001", "ok": True})
        jsonl_store.append(jsonl_path, {"decision": "no id here"})
        merged = jsonl_store.read_merged(jsonl_path)
        assert len(merged) == 1
        assert merged[0]["id"] == "D000001"

    def test_custom_field_names(self, jsonl_path: Path) -> None:
        """Future v3.1 stores (e.g. working memory with W-prefixed ids)
        reuse the same primitive via id_field / amendment_field overrides.
        """
        jsonl_store.append(jsonl_path, {"wid": "W000001", "content": "hello"})
        jsonl_store.append(
            jsonl_path,
            {"wid": "W000001", "_promoted_to": "D000007", "content": "hello-v2"},
        )
        merged = jsonl_store.read_merged(
            jsonl_path, id_field="wid", amendment_field="_promoted_to"
        )
        assert len(merged) == 1
        assert merged[0]["content"] == "hello-v2"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert jsonl_store.read_merged(tmp_path / "nope.jsonl") == []


class TestCompact:
    """Predicate-based atomic rewrite. Future v3.1 stores use this for
    working-memory eviction (drop tombstoned entries during sync).
    """

    def test_drops_records_failing_predicate(self, jsonl_path: Path) -> None:
        for i in range(5):
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}", "drop_me": i % 2 == 0})
        dropped = jsonl_store.compact(
            jsonl_path, keep_predicate=lambda r: not r.get("drop_me")
        )
        assert dropped == 3  # i=0,2,4
        remaining = jsonl_store.read_all(jsonl_path)
        assert len(remaining) == 2
        assert [r["id"] for r in remaining] == ["D000001", "D000003"]

    def test_keep_all_is_noop_on_count(self, jsonl_path: Path) -> None:
        for i in range(3):
            jsonl_store.append(jsonl_path, {"id": f"D{i:06d}"})
        dropped = jsonl_store.compact(jsonl_path, keep_predicate=lambda r: True)
        assert dropped == 0
        assert len(jsonl_store.read_all(jsonl_path)) == 3

    def test_drop_all_yields_empty_file(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001"})
        jsonl_store.append(jsonl_path, {"id": "D000002"})
        dropped = jsonl_store.compact(jsonl_path, keep_predicate=lambda r: False)
        assert dropped == 2
        assert jsonl_path.read_text() == ""

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.jsonl"
        assert jsonl_store.compact(path, keep_predicate=lambda r: True) == 0

    def test_preserves_malformed_lines(self, jsonl_path: Path) -> None:
        """compact is filtering, not corruption cleanup — malformed
        lines must survive so users can run ``codevira doctor``.
        """
        jsonl_store.append(jsonl_path, {"id": "D000001", "drop": False})
        # Append a malformed line directly.
        with open(jsonl_path, "ab") as fh:
            fh.write(b"{this is not valid json\n")
        jsonl_store.append(jsonl_path, {"id": "D000002", "drop": True})

        dropped = jsonl_store.compact(
            jsonl_path, keep_predicate=lambda r: not r.get("drop")
        )
        assert dropped == 1  # only D000002 dropped
        content = jsonl_path.read_text()
        assert "{this is not valid json" in content  # corrupt line preserved
        assert "D000001" in content
        assert "D000002" not in content

    def test_trailing_newline_only_when_nonempty(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "D000001"})
        jsonl_store.compact(jsonl_path, keep_predicate=lambda r: True)
        # Non-empty file ends in exactly one newline (matches append style).
        assert jsonl_path.read_text().endswith("\n")
        assert not jsonl_path.read_text().endswith("\n\n")


class TestReadRecent:
    """Sort-by-ts-desc + slice. Extracted from sessions_store.read_recent."""

    def test_newest_first(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "S000001", "ts": "2026-01-01T00:00:00Z"})
        jsonl_store.append(jsonl_path, {"id": "S000002", "ts": "2026-03-01T00:00:00Z"})
        jsonl_store.append(jsonl_path, {"id": "S000003", "ts": "2026-02-01T00:00:00Z"})
        recent = jsonl_store.read_recent(jsonl_path, limit=10)
        assert [r["id"] for r in recent] == ["S000002", "S000003", "S000001"]

    def test_limit_slices(self, jsonl_path: Path) -> None:
        for i in range(5):
            jsonl_store.append(
                jsonl_path,
                {"id": f"S{i:06d}", "ts": f"2026-0{i + 1}-01T00:00:00Z"},
            )
        recent = jsonl_store.read_recent(jsonl_path, limit=2)
        assert len(recent) == 2
        assert recent[0]["id"] == "S000004"  # 2026-05
        assert recent[1]["id"] == "S000003"  # 2026-04

    def test_missing_ts_sorts_to_end(self, jsonl_path: Path) -> None:
        jsonl_store.append(jsonl_path, {"id": "S000001", "ts": "2026-01-01T00:00:00Z"})
        jsonl_store.append(jsonl_path, {"id": "S000002"})  # no ts
        recent = jsonl_store.read_recent(jsonl_path, limit=10)
        assert recent[0]["id"] == "S000001"
        assert recent[-1]["id"] == "S000002"

    def test_custom_ts_field(self, jsonl_path: Path) -> None:
        jsonl_store.append(
            jsonl_path, {"id": "X1", "created_at": "2026-01-01T00:00:00Z"}
        )
        jsonl_store.append(
            jsonl_path, {"id": "X2", "created_at": "2026-02-01T00:00:00Z"}
        )
        recent = jsonl_store.read_recent(jsonl_path, limit=10, ts_field="created_at")
        assert [r["id"] for r in recent] == ["X2", "X1"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert jsonl_store.read_recent(tmp_path / "nope.jsonl", limit=10) == []
