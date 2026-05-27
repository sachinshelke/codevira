"""Unit tests for mcp_server.storage.fts5_index.

Covers:
- rebuild_from_jsonl: full rebuild from decisions.jsonl
- add_decision: incremental insert, idempotent on dup IDs
- search: BM25 ranking, limit, empty index, malformed query
- staleness_check: detects mtime drift
- Superseded decisions excluded
- Performance: 1000-decision corpus, sub-50ms queries
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_server.storage import fts5_index, jsonl_store


@pytest.fixture
def decisions_path(tmp_path: Path) -> Path:
    path = tmp_path / "decisions.jsonl"
    jsonl_store.append_many(
        path,
        [
            {
                "id": "D000001",
                "decision": "Use bcrypt for password hashing",
                "context": "Industry standard; team familiarity",
                "tags": ["security", "auth"],
            },
            {
                "id": "D000002",
                "decision": "Prefer named exports over default exports in TypeScript",
                "context": None,
                "tags": ["typescript"],
            },
            {
                "id": "D000003",
                "decision": "Always use context.Context as first argument in Go functions",
                "context": "Idiomatic Go pattern",
                "tags": ["go"],
            },
            {
                "id": "D000004",
                "decision": "Superseded old approach",
                "is_superseded": True,
                "tags": ["security"],
            },
        ],
    )
    return path


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "fts5.sqlite"


class TestRebuild:
    def test_rebuild_count_excludes_superseded(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        count = fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # 4 decisions, 1 superseded → 3 indexed
        assert count == 3

    def test_rebuild_creates_index_file(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        assert index_path.is_file()

    def test_rebuild_idempotent(self, decisions_path: Path, index_path: Path) -> None:
        c1 = fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        c2 = fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        assert c1 == c2 == 3

    def test_rebuild_empty_decisions(self, tmp_path: Path, index_path: Path) -> None:
        empty_path = tmp_path / "empty.jsonl"
        empty_path.touch()
        count = fts5_index.rebuild_from_jsonl(empty_path, index_path)
        assert count == 0


class TestSearch:
    def test_search_keyword_match(self, decisions_path: Path, index_path: Path) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        results = fts5_index.search(index_path, "bcrypt")
        assert len(results) >= 1
        assert results[0]["decision_id"] == "D000001"

    def test_search_multiword(self, decisions_path: Path, index_path: Path) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        results = fts5_index.search(index_path, "password hashing")
        assert any(r["decision_id"] == "D000001" for r in results)

    def test_search_stemming(self, decisions_path: Path, index_path: Path) -> None:
        """Porter stemmer should match 'hash' to 'hashing'."""
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        results = fts5_index.search(index_path, "hash")
        # Should find the bcrypt decision via stemming.
        assert any(r["decision_id"] == "D000001" for r in results)

    def test_search_no_results(self, decisions_path: Path, index_path: Path) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        results = fts5_index.search(index_path, "xyzabc123nonexistent")
        assert results == []

    def test_search_empty_query(self, decisions_path: Path, index_path: Path) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        assert fts5_index.search(index_path, "") == []
        assert fts5_index.search(index_path, "   ") == []

    def test_search_missing_index_returns_empty(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        # Don't rebuild first.
        assert fts5_index.search(index_path, "bcrypt") == []

    def test_search_excludes_superseded(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # D000004 had "security" tag + "Superseded old approach" — but it's
        # superseded and should not appear.
        results = fts5_index.search(index_path, "superseded")
        ids = [r["decision_id"] for r in results]
        assert "D000004" not in ids

    def test_search_respects_limit(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # All 3 active decisions match a wildcard-ish query; limit=2.
        results = fts5_index.search(index_path, "use", limit=2)
        assert len(results) <= 2

    def test_search_malformed_query_safe(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # Quotes / parens / colons that could confuse FTS5 — we should
        # sanitize and not raise.
        for q in ['"unbalanced', "(", "x:y:z", "**"]:
            results = fts5_index.search(index_path, q)
            assert isinstance(results, list)  # no exception

    def test_search_returns_bm25_score(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        results = fts5_index.search(index_path, "bcrypt")
        assert all("score" in r for r in results)
        assert all(isinstance(r["score"], float) for r in results)


class TestAddDecision:
    def test_add_incremental_after_rebuild(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # Add a brand-new decision.
        fts5_index.add_decision(
            index_path,
            {
                "id": "D000999",
                "decision": "Newly added bcrypt-related thing",
                "tags": ["security"],
            },
        )
        results = fts5_index.search(index_path, "newly added")
        assert any(r["decision_id"] == "D000999" for r in results)

    def test_add_idempotent_replaces_existing(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # Add the same ID twice.
        fts5_index.add_decision(
            index_path,
            {
                "id": "D000005",
                "decision": "First version",
                "tags": [],
            },
        )
        fts5_index.add_decision(
            index_path,
            {
                "id": "D000005",
                "decision": "Second version",
                "tags": [],
            },
        )
        results = fts5_index.search(index_path, "version")
        # Should appear exactly once (the second insert replaces the first).
        d5_results = [r for r in results if r["decision_id"] == "D000005"]
        assert len(d5_results) == 1

    def test_add_superseded_decision_skipped(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        fts5_index.add_decision(
            index_path,
            {
                "id": "D000777",
                "decision": "superseded skip me",
                "is_superseded": True,
                "tags": [],
            },
        )
        results = fts5_index.search(index_path, "skip")
        assert not any(r["decision_id"] == "D000777" for r in results)


class TestStaleness:
    def test_staleness_fresh_index_not_stale(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        assert fts5_index.staleness_check(decisions_path, index_path) is False

    def test_staleness_after_jsonl_changes(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        # Make the source newer than the index by appending a line.
        # Wait 1.1s so the mtime tick is observable past the epsilon.
        time.sleep(1.1)
        jsonl_store.append(decisions_path, {"id": "D000999", "decision": "new"})
        assert fts5_index.staleness_check(decisions_path, index_path) is True

    def test_staleness_missing_index(
        self, decisions_path: Path, index_path: Path
    ) -> None:
        assert fts5_index.staleness_check(decisions_path, index_path) is True


class TestPerformance:
    def test_1000_decisions_search_under_50ms(
        self, tmp_path: Path, index_path: Path
    ) -> None:
        """Performance gate: rebuild + query 1000 decisions in <1s + 50ms."""
        decisions_path = tmp_path / "decisions.jsonl"

        # Synthesize 1000 decisions with varied vocabulary.
        VOCAB = [
            "authentication",
            "database",
            "schema",
            "migration",
            "validator",
            "performance",
            "scaling",
            "caching",
            "retry",
            "circuit",
            "queue",
            "encoding",
            "serialization",
            "tokenization",
            "compression",
        ]
        records = []
        for i in range(1000):
            words = [VOCAB[(i + k) % len(VOCAB)] for k in range(5)]
            records.append(
                {
                    "id": f"D{i:06d}",
                    "decision": f"Use {' '.join(words)} for module {i}",
                    "tags": [words[0]],
                }
            )
        jsonl_store.append_many(decisions_path, records)

        t0 = time.perf_counter()
        count = fts5_index.rebuild_from_jsonl(decisions_path, index_path)
        rebuild_ms = (time.perf_counter() - t0) * 1000
        assert count == 1000
        assert rebuild_ms < 5000, f"rebuild took {rebuild_ms:.1f}ms"

        # Now query 100 times; average should be <50ms.
        t0 = time.perf_counter()
        for q in VOCAB * 7:  # ~100 queries
            results = fts5_index.search(index_path, q, limit=5)
            assert isinstance(results, list)
        total_ms = (time.perf_counter() - t0) * 1000
        per_query_ms = total_ms / (len(VOCAB) * 7)
        assert per_query_ms < 50, f"average query took {per_query_ms:.1f}ms"
