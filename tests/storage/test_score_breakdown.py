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
        assert set(bd) == {"bm25", "rank", "rank_norm", "matched"}
        assert bd["matched"] == "fts5"

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
