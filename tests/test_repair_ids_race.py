"""
test_repair_ids_race.py — v3.7.1: repair_ids must not destroy a concurrent append.

``repair_ids(apply=True)`` used to do::

    raw, malformed = read_records_and_malformed(path)   # shared lock, RELEASED
    result = id_repair.normalize(raw)
    rewrite_all(path, result["records"], ...)           # exclusive lock

Any decision appended in the gap between those two locks was silently
destroyed by the full-file rewrite — it was read before, and overwritten after.

This is not theoretical. The repair runs automatically at EVERY server start
(``_mig_v370_repair_collisions``), and a user running several MCP servers at
once can easily have one window record a decision while another boots.

The fix holds ONE exclusive lock across read + normalize + rewrite, the same
discipline ``jsonl_store.compact`` already used.
"""

from __future__ import annotations

import json

import pytest

from mcp_server.storage import decisions_store, jsonl_store, paths


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A decisions.jsonl containing a real id collision (so a repair happens)."""
    d = tmp_path / ".codevira"
    d.mkdir()
    path = d / "decisions.jsonl"
    # Two records minted with the SAME id — what two engineers produce.
    path.write_text(
        '{"id":"D1","decision":"first","ts":"2026-01-01T00:00:00+00:00"}\n'
        '{"id":"D1","decision":"second","ts":"2026-01-02T00:00:00+00:00"}\n'
    )
    monkeypatch.setattr(paths, "decisions_path", lambda *a, **k: path)
    monkeypatch.setattr(decisions_store, "rebuild_indexes", lambda *a, **k: None)
    return path


def _ids(path):
    return [json.loads(ln)["id"] for ln in path.read_text().splitlines() if ln.strip()]


def _texts(path):
    return [
        json.loads(ln).get("decision")
        for ln in path.read_text().splitlines()
        if ln.strip()
    ]


def _exclusive_lock_is_free(path) -> bool:
    """True if the file's exclusive lock can be taken right now.

    A real concurrent appender goes through ``jsonl_store.append``, which takes
    this lock — so "is the lock free?" is exactly "could an appender slip into
    this window?". Non-blocking so the test can never hang.
    """
    import fcntl

    with open(path, "a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # someone holds it — an appender would block
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


class TestConcurrentAppendSurvives:
    def test_lock_is_held_across_the_read_write_window(self, store, monkeypatch):
        """THE regression, stated as the guarantee that actually prevents it.

        The old code released its shared read lock before taking the exclusive
        write lock, so an appender could land in between and be overwritten by
        the full-file rewrite. The fix holds ONE exclusive lock across
        read + normalize + rewrite, so the window does not exist.

        Asserting "the lock is held while the transform runs" is the
        deterministic form of that. (Appending directly with open(..., "a")
        would NOT reproduce the bug — it bypasses the lock entirely, which no
        real caller does.)
        """
        real_normalize = decisions_store_normalize()
        observed: dict[str, bool] = {}

        def _normalize_and_probe(records):
            observed["lock_free_mid_repair"] = _exclusive_lock_is_free(store)
            return real_normalize(records)

        monkeypatch.setattr(
            "mcp_server.storage.id_repair.normalize", _normalize_and_probe
        )

        decisions_store.repair_ids(apply=True)

        assert observed["lock_free_mid_repair"] is False, (
            "the exclusive lock was NOT held during the repair — a concurrent "
            "append could land in the read-vs-write window and be destroyed"
        )

    def test_a_real_concurrent_appender_is_not_lost(self, store, monkeypatch):
        """End-to-end: an appender using the real API must survive a repair."""
        import threading

        real_normalize = decisions_store_normalize()
        started = threading.Event()

        def _normalize_and_let_appender_race(records):
            started.set()  # appender thread is now racing us
            return real_normalize(records)

        monkeypatch.setattr(
            "mcp_server.storage.id_repair.normalize", _normalize_and_let_appender_race
        )

        def _append():
            started.wait(timeout=5)
            jsonl_store.append(
                store,
                {
                    "id": "D9",
                    "decision": "concurrent",
                    "ts": "2026-01-03T00:00:00+00:00",
                },
            )

        t = threading.Thread(target=_append)
        t.start()
        decisions_store.repair_ids(apply=True)
        t.join(timeout=5)

        assert "concurrent" in _texts(
            store
        ), "a concurrently appended decision was destroyed by the repair"

    def test_repair_still_fixes_the_collision(self, store):
        """The race fix must not break what repair_ids is for."""
        before = _ids(store)
        assert before.count("D1") == 2

        result = decisions_store.repair_ids(apply=True)

        assert result["applied"] is True
        after = _ids(store)
        assert len(set(after)) == len(after), "collision was not repaired"
        assert len(after) == 2, "a record was lost during repair"


class TestNoNeedlessRewrites:
    def test_clean_store_is_not_rewritten(self, tmp_path, monkeypatch):
        """A store with no collisions must be left byte-identical — otherwise
        every server start rewrites the whole decision log."""
        d = tmp_path / ".codevira"
        d.mkdir()
        path = d / "decisions.jsonl"
        path.write_text(
            '{"id":"D1","decision":"only","ts":"2026-01-01T00:00:00+00:00"}\n'
        )
        monkeypatch.setattr(paths, "decisions_path", lambda *a, **k: path)
        monkeypatch.setattr(decisions_store, "rebuild_indexes", lambda *a, **k: None)

        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns

        result = decisions_store.repair_ids(apply=True)

        assert result["changed"] is False
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime, "clean store was rewritten"


class TestTransformAllPrimitive:
    def test_preserves_malformed_lines(self, tmp_path):
        path = tmp_path / "x.jsonl"
        path.write_text('{"id":"A"}\nNOT JSON\n{"id":"B"}\n')

        out = jsonl_store.transform_all(path, lambda recs: {"records": recs})

        assert out["malformed_preserved"] == 1
        assert "NOT JSON" in path.read_text(), "malformed line was dropped"

    def test_report_only_never_writes(self, store):
        before = store.read_bytes()
        decisions_store.repair_ids(apply=False)
        assert store.read_bytes() == before


def decisions_store_normalize():
    """Return the real id_repair.normalize (imported lazily inside repair_ids)."""
    from mcp_server.storage import id_repair

    return id_repair.normalize
