"""4.0 Step 6 — decision search must say WHY a row surfaced.

`search_decisions` returned a bare `score`: a raw FTS5 BM25 figure, which
is unbounded and negative and therefore meaningless to a reader on its
own. `get_skill` has emitted a `score_breakdown` since v3.1.0, so the one
retrieval surface agents actually use was the one that could not be
debugged — and "why did this surface?" is unanswerable without it.

RED against pre-4.0 code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store, paths


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    paths.ensure_dirs(root)
    decisions_store.invalidate_merged_cache()
    for i in range(6):
        decisions_store.record(f"Decision {i} about atomic writes and locking")
    return root


class TestBreakdownIsPresent:
    def test_store_search_emits_a_breakdown(self, project: Path) -> None:
        hits = decisions_store.search("atomic writes", limit=5)
        assert hits, "expected hits"
        bd = hits[0]["score_breakdown"]
        # `freshness` joined in v4.1 (Phase 26): search re-ranks BM25 by
        # outcome-confidence x recency, and the breakdown must SAY so — an
        # order the reader cannot explain is the thing this breakdown exists
        # to prevent.
        assert set(bd) == {"bm25", "rank", "rank_norm", "freshness", "matched"}
        assert bd["matched"] == "fts5"
        assert 0.0 <= bd["freshness"] <= 1.0

    def test_mcp_tool_surfaces_it_under_full(self, project: Path) -> None:
        """Deliberately NOT in the compact row: measured at +122 tokens on
        a 5-row payload (+27%), which is a real cost on every search for an
        aid needed rarely. E1 (D0000ZQ) chose token discipline for the
        default, so this ships under full=True with snippet and origin."""
        from mcp_server.tools.search import search_decisions

        compact = search_decisions("atomic writes", limit=5)
        assert compact["results"], "expected hits"
        assert "score_breakdown" not in compact["results"][0]

        full = search_decisions("atomic writes", limit=5, full=True)
        assert full["results"][0]["score_breakdown"] is not None


def _now_iso() -> str:
    """Current UTC instant, for fixtures that assert on absolute recency."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class TestSearchRanksByFreshness:
    """v4.1 (Phase 26): search re-ranks BM25 relevance by outcome-confidence x
    recency, and caps AFTER ranking.

    Before this, the cap sat inside the BM25 loop, so freshness could only
    reorder rows relevance had already selected — a two-year-old decision the
    tracker never validated still displaced a current one that BM25 ranked
    just below it.
    """

    def _rewrite(self, root: Path, mutate) -> None:
        """Rewrite decisions.jsonl through `mutate`, then drop the caches."""
        import json

        from mcp_server.storage import fts5_index

        p = paths.decisions_path(root)
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        for r in rows:
            mutate(r)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        decisions_store.invalidate_merged_cache()
        fts5_index.mark_stale(paths.fts5_path(root))

    def test_a_stale_decision_loses_to_a_fresh_one(self, project: Path) -> None:
        ids = sorted(
            json_id
            for json_id in (
                d["id"] for d in decisions_store.list_all(limit=50)["decisions"]
            )
        )
        assert len(ids) >= 2
        oldest, newest = ids[0], ids[-1]

        def mutate(r):
            if r.get("id") == oldest:
                # ancient AND reverted-adjacent: the weakest possible evidence
                r["ts"] = "2020-01-01T00:00:00+00:00"
                r["outcome"] = "modified"
            elif r.get("id") == newest:
                # RELATIVE to now, not a literal date. This was hardcoded to
                # 2026-08-16 and the assertion below is an absolute threshold,
                # so the test passed on the day it was written and started
                # failing ~10 days later as the 90-day half-life decayed the
                # score past 0.9 — a time bomb, not a regression. Any test
                # asserting an absolute freshness value must generate its own
                # timestamp.
                r["ts"] = _now_iso()
                r["outcome"] = "kept"

        self._rewrite(project, mutate)

        hits = decisions_store.search("atomic writes", limit=6)
        order = [h["id"] for h in hits]
        assert newest in order, order
        if oldest in order:
            assert order.index(newest) < order.index(oldest), (
                "a 2020 decision the tracker saw churn must not outrank a "
                f"current kept one: {order}"
            )
        fresh = next(h for h in hits if h["id"] == newest)
        assert fresh["score_breakdown"]["freshness"] > 0.9

    def test_order_is_deterministic_across_repeated_searches(
        self, project: Path
    ) -> None:
        """The clock is sampled once per search. If it were sampled per row,
        rows evaluated later would score younger and the order could drift
        between two identical calls."""
        runs = [
            [h["id"] for h in decisions_store.search("atomic writes", limit=6)]
            for _ in range(5)
        ]
        assert all(r == runs[0] for r in runs), runs


class TestBreakdownIsMeaningful:
    def test_rank_is_zero_based_and_ordered(self, project: Path) -> None:
        hits = decisions_store.search("atomic writes", limit=5)
        ranks = [h["score_breakdown"]["rank"] for h in hits]
        assert ranks == sorted(ranks)
        assert ranks[0] == 0

    def test_rank_norm_is_bounded_unlike_raw_bm25(self, project: Path) -> None:
        """Raw BM25 is unbounded and negative, so it cannot be compared
        across queries. rank_norm can."""
        hits = decisions_store.search("atomic writes", limit=5)
        for h in hits:
            bd = h["score_breakdown"]
            assert 0.0 <= bd["rank_norm"] <= 1.0
        assert hits[0]["score_breakdown"]["rank_norm"] == 1.0

    def test_bm25_matches_the_top_level_score(self, project: Path) -> None:
        """The breakdown must explain the score it ships beside, not a
        different number."""
        hits = decisions_store.search("atomic writes", limit=5)
        for h in hits:
            assert h["score_breakdown"]["bm25"] == h["score"]

    def test_no_hits_yields_no_crash(self, project: Path) -> None:
        assert decisions_store.search("zzzz-nonexistent-token", limit=5) == []
