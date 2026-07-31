"""4.0 Step 4 — search caches must be fast AND never stale.

`search_decisions` was measured at p95 10.18ms against D00012K's 3ms
warm-call ceiling, i.e. in breach before any 4.0 work was added. Two
causes, both "recompute the world on every call":

  * `read_merged` re-parsed the entire decision store — 178 json.loads
    per search.
  * `staleness_check` opened a connection and ran
    CREATE VIRTUAL TABLE IF NOT EXISTS + a sqlite_master scan, to answer
    a question that only changes when decisions.jsonl is written.

Both are now memoized on (mtime_ns, size). The store is append-only, so
any write moves both — a stale read is therefore impossible. These tests
exist to keep that property true, because a fast wrong answer is worse
than a slow right one.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, fts5_index, paths


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    decisions_store.invalidate_merged_cache()
    fts5_index.invalidate_staleness_cache()
    return root


class TestCachesNeverServeStaleData:
    def test_a_new_decision_is_immediately_visible(self, project: Path) -> None:
        """The whole risk of the cache, in one test."""
        decisions_store.record("First decision about retries")
        before = {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}

        did = decisions_store.record("Second decision about caching")
        after = {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}

        assert did not in before
        assert did in after, "cache served a stale view after a write"

    def test_search_sees_a_decision_recorded_after_the_first_search(
        self, project: Path
    ) -> None:
        decisions_store.record("Use bcrypt for password hashing")
        assert decisions_store.search("bcrypt", limit=5)

        decisions_store.record("Use argon2id for password hashing")
        hits = {h["id"] for h in decisions_store.search("argon2id", limit=5)}
        assert hits, "new decision invisible to search — stale FTS or merged cache"

    def test_amendment_is_visible_immediately(self, project: Path) -> None:
        """Amendments append too, so the same invalidation must cover them."""
        did = decisions_store.record("A decision to be locked")
        assert not decisions_store.get(did)["do_not_revert"]

        decisions_store.mark_protected(did)
        assert decisions_store.get(did)["do_not_revert"] is True

    def test_supersede_is_visible_immediately(self, project: Path) -> None:
        old = decisions_store.record("Original decision")
        decisions_store.supersede(old, "Replacement decision", reason="better")
        live = {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}
        assert old not in live, "superseded decision still visible from cache"

    def test_external_write_invalidates(self, project: Path) -> None:
        """A write from ANOTHER process (append by hand) must be picked up."""
        decisions_store.record("First")
        _ = decisions_store.list_all(limit=50)  # warm the cache

        p = paths.decisions_path(project)
        time.sleep(0.01)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(
                '{"id":"DEXTERNAL","ts":"2026-01-01T00:00:00+00:00",'
                '"decision":"written out of band"}\n'
            )

        ids = {d["id"] for d in decisions_store.list_all(limit=50)["decisions"]}
        assert "DEXTERNAL" in ids, "out-of-band append not observed"


class TestStalenessCacheSemantics:
    def test_only_fresh_verdicts_are_memoized(self, project: Path) -> None:
        """A stale verdict must always be re-derived, or a rebuild gets
        skipped and search silently returns old results.

        Note the pre-existing 1-second epsilon in ``staleness_check`` (it
        tolerates filesystems with second-precision mtime), so the source
        mtime is advanced explicitly rather than slept for.
        """
        import os

        decisions_store.record("Something searchable")
        d_path = paths.decisions_path(project)
        i_path = paths.fts5_path(project)

        decisions_store.search("searchable", limit=5)  # builds + memoizes fresh
        assert fts5_index.staleness_check(d_path, i_path) is False

        with d_path.open("a", encoding="utf-8") as fh:
            fh.write(
                '{"id":"DNEW","ts":"2026-01-01T00:00:00+00:00","decision":"new"}\n'
            )
        # Push the source clearly past the epsilon.
        future = time.time() + 5
        os.utime(d_path, (future, future))

        assert (
            fts5_index.staleness_check(d_path, i_path) is True
        ), "the memoized FRESH verdict masked a genuinely stale index"

    def test_invalidate_helpers_are_callable(self, project: Path) -> None:
        decisions_store.record("x")
        decisions_store.list_all(limit=5)
        decisions_store.invalidate_merged_cache()
        fts5_index.invalidate_staleness_cache()
        assert decisions_store.list_all(limit=5)["count"] >= 1


class TestWithinBudget:
    def test_search_p95_is_under_the_locked_ceiling(self, project: Path) -> None:
        """D00012K locks the warm call at <=3ms. Measured 10.18ms before."""
        for i in range(40):
            decisions_store.record(f"Decision number {i} about retries and caching")

        queries = ["retries", "caching", "decision number", "retries caching"]
        decisions_store.search(queries[0], limit=5)  # warm

        lat = []
        for _ in range(15):
            for q in queries:
                t = time.perf_counter()
                decisions_store.search(q, limit=5)
                lat.append((time.perf_counter() - t) * 1000)
        lat.sort()
        p95 = lat[int(len(lat) * 0.95)]
        assert p95 < 3.0, f"p95 {p95:.2f}ms exceeds the 3ms budget (D00012K)"


class TestInPlaceRewritesInvalidate:
    """The (mtime, size) key catches appends. An in-place REWRITE that
    happened to preserve both would not — and `repair_ids` genuinely
    rewrites the file. `rebuild_indexes()` is the chokepoint every
    amendment and repair path already funnels through, so it drops both
    caches."""

    def test_rebuild_indexes_drops_the_caches(self, project: Path) -> None:
        decisions_store.record("A decision")
        decisions_store.list_all(limit=5)  # warm
        assert decisions_store._MERGED_CACHE, "expected a warm cache"

        decisions_store.rebuild_indexes()
        assert not decisions_store._MERGED_CACHE, "merged cache not dropped"
        assert not fts5_index._FRESH_CACHE, "staleness cache not dropped"

    def test_repair_ids_result_is_visible(self, project: Path) -> None:
        """repair_ids rewrites the store; the next read must see it."""
        decisions_store.record("One")
        decisions_store.record("Two")
        decisions_store.list_all(limit=5)  # warm
        out = decisions_store.repair_ids(apply=True)
        assert isinstance(out, dict)
        # Whatever it did, the store must still read back consistently.
        assert decisions_store.list_all(limit=50)["count"] >= 2
