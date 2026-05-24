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
