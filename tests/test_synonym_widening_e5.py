"""E5 (Phase 23, part b') — no-dependency synonym query widening.

The opt-in ``[semantic]`` embedding extra is deferred (D0000XY); this is the
model-free recall aid. Pins: the synonym map, that widening is OFF by default
(read surface unchanged), and the actual win — a query in one vocabulary
recalls a decision recorded in a synonymous one ("database" → "postgres").
"""

from __future__ import annotations


import pytest

from mcp_server.storage import fts5_index, synonyms


class TestSynonymMap:
    def test_expands_to_group(self) -> None:
        out = synonyms.expand("database")
        assert out[0] == "database"  # query term first
        assert {"db", "postgres", "sql"} <= set(out)

    def test_unknown_token_passes_through(self) -> None:
        assert synonyms.expand("frobnicate") == ["frobnicate"]

    def test_case_insensitive(self) -> None:
        assert set(synonyms.expand("AUTH")) == set(synonyms.expand("auth"))

    def test_empty(self) -> None:
        assert synonyms.expand("") == []


class TestQueryBuilder:
    def test_default_off_no_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEVIRA_SYNONYM_WIDENING", raising=False)
        q = fts5_index._sanitize_fts_query("database auth")
        assert q == '"database" OR "auth"'  # unchanged

    def test_widening_on_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEVIRA_SYNONYM_WIDENING", "1")
        q = fts5_index._sanitize_fts_query("database")
        assert '"postgres"' in q and '"db"' in q and '"database"' in q

    def test_widening_dedups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEVIRA_SYNONYM_WIDENING", "1")
        # "db" and "database" are the same group → no duplicate quoted terms.
        q = fts5_index._sanitize_fts_query("db database")
        assert q.count('"db"') == 1 and q.count('"database"') == 1


class TestRecallWin:
    """The headline: widening recalls a synonymous decision that the plain
    keyword search misses."""

    def test_synonym_query_recall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_server.storage import decisions_store

        did = decisions_store.record(
            decision="store the event log in postgres as an append-only ledger",
            tags=["postgres", "events"],
        )

        # Plain keyword search for "database" — the decision says "postgres",
        # not "database", so it should NOT surface.
        monkeypatch.delenv("CODEVIRA_SYNONYM_WIDENING", raising=False)
        plain = {h["id"] for h in decisions_store.search("database", limit=10)}
        assert did not in plain

        # With widening, "database" expands to include "postgres" → recalled.
        monkeypatch.setenv("CODEVIRA_SYNONYM_WIDENING", "1")
        widened = {h["id"] for h in decisions_store.search("database", limit=10)}
        assert did in widened
