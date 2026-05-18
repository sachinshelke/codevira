"""
test_decision_embeddings.py — Hybrid retrieval regression tests.

2026-05-17 v2.1: codifies the benchmark queries that returned 0 hits in
v2.0's BM25-only ``search_decisions`` and now must return relevant hits
via the new hybrid (BM25 + semantic + RRF) retrieval.

Why this matters: the UDAP benchmark identified that natural-language
queries silently missed decisions whose text used synonyms or paraphrased
the same concept. Every future agent (including AI re-decisions) would
miss prior context → drift. These tests prevent that regression — any
PR that breaks one of these queries fails CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_server.tools._decision_embeddings import (
    rrf_merge,
    embed_decision,
    semantic_search_decisions,
)


class TestRrfMerge:
    """Reciprocal Rank Fusion — pure-Python logic, deterministic."""

    def test_empty_inputs_returns_empty(self):
        assert rrf_merge([], []) == []

    def test_single_retriever_only(self):
        # If only BM25 has hits, results = BM25 order.
        assert rrf_merge([10, 20, 30], [], limit=10) == [10, 20, 30]
        assert rrf_merge([], [10, 20, 30], limit=10) == [10, 20, 30]

    def test_shared_candidate_ranks_higher(self):
        """Candidates appearing in BOTH lists must outrank single-list ones."""
        bm25 = [1, 2, 3]
        semantic = [4, 2, 5]
        # ID=2 is in both; should be top.
        ranked = rrf_merge(bm25, semantic, limit=5)
        assert ranked[0] == 2, f"shared candidate must win; got {ranked}"

    def test_limit_respected(self):
        assert len(rrf_merge([1, 2, 3, 4], [5, 6, 7, 8], limit=3)) == 3

    def test_deterministic_ordering(self):
        # Same inputs → same output every time.
        a = rrf_merge([1, 2, 3], [2, 3, 4], limit=5)
        b = rrf_merge([1, 2, 3], [2, 3, 4], limit=5)
        assert a == b


class TestEmbedDecisionDegradesGracefully:
    """If chromadb is unavailable, embed_decision returns False without raising."""

    def test_returns_false_when_collection_unavailable(self):
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=None,
        ):
            result = embed_decision(decision_id=1, text="anything")
            assert result is False

    def test_returns_false_when_text_empty(self):
        # Even with a healthy collection, blank text → False (no useful vector).
        fake = MagicMock()
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            assert embed_decision(decision_id=1, text="") is False
            assert embed_decision(decision_id=2, text="   ") is False
        # The add method MUST NOT have been called for empty/whitespace text.
        fake.add.assert_not_called()

    def test_returns_false_when_add_raises(self):
        fake = MagicMock()
        fake.add.side_effect = RuntimeError("simulated chromadb error")
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            # Must NOT propagate the exception — caller relies on this for P9.
            assert embed_decision(decision_id=1, text="hello world") is False

    def test_returns_true_on_success(self):
        fake = MagicMock()
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            result = embed_decision(
                decision_id=42,
                text="Decided to use Postgres over DynamoDB",
                session_id="s1",
                file_path="src/db.py",
                context="we need transactions",
            )
            assert result is True
        # Verify chromadb was called with the expected shape.
        fake.add.assert_called_once()
        call_kwargs = fake.add.call_args.kwargs
        assert call_kwargs["ids"] == ["42"]
        assert "Postgres" in call_kwargs["documents"][0]
        # Context should be appended to the embedded text.
        assert "we need transactions" in call_kwargs["documents"][0]
        assert call_kwargs["metadatas"][0]["decision_id"] == 42
        assert call_kwargs["metadatas"][0]["session_id"] == "s1"


class TestSemanticSearchDegradesGracefully:
    """If chromadb is unavailable or fails, return [] not crash."""

    def test_returns_empty_when_collection_unavailable(self):
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=None,
        ):
            assert semantic_search_decisions("anything") == []

    def test_returns_empty_on_query_error(self):
        fake = MagicMock()
        fake.query.side_effect = RuntimeError("chroma failed")
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            assert semantic_search_decisions("anything") == []

    def test_parses_ids_from_chroma_response(self):
        fake = MagicMock()
        # chromadb returns ids as nested list (per-query).
        fake.query.return_value = {"ids": [["7", "12", "3"]]}
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            result = semantic_search_decisions("test query", limit=10)
            assert result == [7, 12, 3]

    def test_invalid_ids_skipped(self):
        """If chromadb returns malformed IDs, skip them rather than crash."""
        fake = MagicMock()
        fake.query.return_value = {"ids": [["5", "not-an-int", None, "9"]]}
        with patch(
            "mcp_server.tools._decision_embeddings._decisions_collection_or_none",
            return_value=fake,
        ):
            result = semantic_search_decisions("query")
            assert result == [5, 9]


class TestBenchmarkQueriesNoLongerMiss:
    """The benchmark gap: queries that v2.0 BM25-only search returned 0 for
    must now return hits via the hybrid path. Tests use mocked semantic
    retrieval so they don't require the ML model — the integration
    smoke happens at the e2e/G2 layer.
    """

    def test_natural_language_query_finds_via_semantic_when_bm25_empty(self):
        """search_decisions integration: BM25 returns 0, semantic returns
        the right ID, hybrid path elevates it."""
        # Mock db.search_decisions → no BM25 hits
        mock_db = MagicMock()
        mock_db.search_decisions.return_value = []
        # Mock semantic retrieval → returns decision id 42
        # Mock db.conn.execute for the fetch-by-id query
        mock_row = {
            "id": 42,
            "decision": "Chose 4-layer DDD architecture",
            "context": "domain / application / infra / presentation",
            "file_path": None,
            "do_not_revert": 0,
            "summary": None,
            "phase": None,
            "created_at": "2026-05-17T12:00:00",
        }
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [mock_row]
        mock_db.conn.execute.return_value = mock_cur

        # 2026-05-18 v2.1.2 Item 1: search.py now calls the scored variant
        # so it can apply a similarity threshold. We mock that instead and
        # return a distance well BELOW the 0.65 search threshold so the
        # match survives the filter.
        with patch("mcp_server.tools.search._get_db", return_value=mock_db), patch(
            "mcp_server.tools._decision_embeddings.semantic_search_decisions_scored",
            return_value=[(42, 0.30)],
        ):
            from mcp_server.tools.search import search_decisions

            result = search_decisions("DDD architecture layer", limit=5)

        # Critical assertions: count>0 AND retrieval flagged correctly.
        assert result["count"] == 1, (
            f"Bug regression: 'DDD architecture layer' returned 0 hits. "
            f"v2.0 BM25-only behavior re-introduced? Got: {result}"
        )
        assert result["retrieval"] in (
            "hybrid",
            "semantic",
        ), f"Hybrid path didn't engage; retrieval={result['retrieval']!r}"
        assert result["results"][0]["id"] == 42
        # v2.1.2 Item 1: response now exposes threshold_used.
        assert "threshold_used" in result

    def test_pure_bm25_path_still_works_when_semantic_unavailable(self):
        """If semantic returns nothing (chromadb unavailable), fall back to
        BM25-only. Verifies P9 graceful degradation."""
        mock_db = MagicMock()
        mock_db.search_decisions.return_value = [
            {
                "id": 1,
                "decision": "x",
                "context": "y",
                "file_path": None,
                "do_not_revert": 0,
                "summary": None,
                "phase": None,
                "created_at": "2026-05-17T12:00:00",
            },
        ]
        with patch("mcp_server.tools.search._get_db", return_value=mock_db), patch(
            "mcp_server.tools._decision_embeddings.semantic_search_decisions_scored",
            return_value=[],
        ):
            from mcp_server.tools.search import search_decisions

            result = search_decisions("anything", limit=5)
        assert result["count"] == 1
        assert result["retrieval"] == "keyword"

    def test_gibberish_query_returns_zero_above_threshold(self):
        """v2.1.2 Item 1: when semantic returns hits but all have distance
        ABOVE the search threshold, the response has count=0 and a
        distinguishing retrieval marker. The trust-recovery fix for the
        ``"how to make a cake"`` regression in v2.1.1.
        """
        mock_db = MagicMock()
        mock_db.search_decisions.return_value = []
        # Semantic finds the closest decisions but every distance is > 0.65
        # (the default search threshold). The threshold filter rejects all.
        with patch("mcp_server.tools.search._get_db", return_value=mock_db), patch(
            "mcp_server.tools._decision_embeddings.semantic_search_decisions_scored",
            return_value=[(7, 0.90), (8, 0.88)],
        ):
            from mcp_server.tools.search import search_decisions

            result = search_decisions("how to make a cake", limit=5)
        assert result["count"] == 0
        assert result["retrieval"] == "semantic-no-results-above-threshold"
