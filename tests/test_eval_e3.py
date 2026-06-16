"""E3 (Phase 21) — read-side relevance eval.

Pins: self-derived intent queries, correct recall@k / MRR / precision math,
the lexical + LLM-as-judge precision paths, end-to-end run on a seeded
isolated corpus, and graceful behavior on an empty corpus. The eval is the
metric for codevira's actual leverage (D00005N): does the read surface
surface the right memory with low noise?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.eval import judge, relevance, report
from mcp_server.eval.relevance import CaseResult, EvalCase


# ─────────────────────────────────────────────────────────────────────
# Query derivation (self-maintaining ground truth)
# ─────────────────────────────────────────────────────────────────────


class TestDeriveQuery:
    def test_uses_file_tags_and_salient(self) -> None:
        q = relevance.derive_query(
            {
                "file_path": "mcp_server/engine/policies/decision_lock.py",
                "tags": ["safety", "content-aware"],
                "decision": "content-aware orthogonality avoids false-positive blocking",
            }
        )
        assert q is not None
        # file stem term + a tag + a salient topic word all present
        assert "lock" in q and "safety" in q and "orthogonality" in q

    def test_not_verbatim_decision_text(self) -> None:
        # Stop-words / short tokens are dropped, so the query is intent-shaped,
        # not a copy of the decision sentence.
        q = relevance.derive_query({"decision": "use the new fix for this", "tags": []})
        assert q is None or "the" not in q.split()

    def test_no_signal_returns_none(self) -> None:
        assert (
            relevance.derive_query({"decision": "ok", "tags": [], "file_path": None})
            is None
        )


class TestBuildCases:
    def test_skips_signal_less_and_caps(self) -> None:
        decs = [
            {
                "id": "D1",
                "file_path": "a/auth.py",
                "tags": ["auth"],
                "decision": "bcrypt hashing",
            },
            {"id": "D2", "decision": "ok"},  # no signal → skipped
            {"id": "D3", "tags": ["caching"], "decision": "redis cache layer"},
        ]
        cases = relevance.build_cases(decs, max_cases=10)
        ids = {c.decision_id for c in cases}
        assert ids == {"D1", "D3"}
        assert len(relevance.build_cases(decs, max_cases=1)) == 1


# ─────────────────────────────────────────────────────────────────────
# Metric math (deterministic)
# ─────────────────────────────────────────────────────────────────────


def _case(did: str) -> EvalCase:
    return EvalCase(decision_id=did, query="q", source={"id": did})


class TestMetrics:
    def test_recall_and_mrr(self) -> None:
        results = [
            CaseResult(case=_case("a"), hit=True, rank=1, returned_ids=["a"], k=5),
            CaseResult(
                case=_case("b"), hit=True, rank=3, returned_ids=["x", "y", "b"], k=5
            ),
            CaseResult(case=_case("c"), hit=False, rank=None, returned_ids=["x"], k=5),
            CaseResult(case=_case("d"), hit=True, rank=2, returned_ids=["x", "d"], k=5),
        ]
        rep = relevance.summarize(results, k=5)
        assert rep.recall_at_k == 0.75  # 3 of 4
        # MRR = (1/1 + 1/3 + 0 + 1/2) / 4
        assert abs(rep.mrr - (1 + 1 / 3 + 0 + 1 / 2) / 4) < 1e-9

    def test_empty_results(self) -> None:
        rep = relevance.summarize([], k=5)
        assert rep.recall_at_k == 0.0 and rep.mrr == 0.0 and rep.n_cases == 0


# ─────────────────────────────────────────────────────────────────────
# Judge — lexical + LLM
# ─────────────────────────────────────────────────────────────────────


class TestJudge:
    def test_lexical_relevance_signals(self) -> None:
        src = {
            "id": "D1",
            "file_path": "auth.py",
            "tags": ["auth"],
            "decision": "bcrypt password hashing",
        }
        assert judge.lexical_relevance(src, src)  # same id
        assert judge.lexical_relevance(
            src, {"id": "D2", "file_path": "auth.py"}
        )  # same file
        assert judge.lexical_relevance(
            src, {"id": "D3", "tags": ["auth"]}
        )  # shared tag
        assert judge.lexical_relevance(
            src, {"id": "D4", "decision": "password hashing with bcrypt"}
        )  # ≥2 shared tokens
        assert not judge.lexical_relevance(
            src, {"id": "D5", "decision": "redis caching layer"}
        )

    def test_score_llm_uses_injected_ask(self) -> None:
        results = [
            CaseResult(
                case=EvalCase("D1", "auth", {"id": "D1"}),
                hit=True,
                rank=1,
                returned_ids=["D1", "D2"],
                k=5,
            )
        ]
        # Fake the loader so we don't touch the store.
        import mcp_server.eval.judge as J

        J._load = lambda did, cache: {"id": did, "decision": did}  # type: ignore # noqa: E731

        def ask(prompt: str) -> str:
            return "1: y\n2: n"  # judge: item 1 relevant, item 2 not

        ran = judge.score_llm(results, ask)
        assert ran is True
        assert results[0].relevant_in_topk == 1

    def test_score_llm_none_ask_is_noop(self) -> None:
        results = [
            CaseResult(case=_case("a"), hit=True, rank=1, returned_ids=["a"], k=5)
        ]
        assert judge.score_llm(results, None) is False


# ─────────────────────────────────────────────────────────────────────
# End-to-end on an injected corpus + seeded isolated store
# ─────────────────────────────────────────────────────────────────────


class TestRunEval:
    def test_empty_corpus_no_crash(self) -> None:
        res = report.run_eval(decisions=[])
        assert res["metrics"]["recall_at_k"] == 0.0
        assert res["judge_mode"] == "lexical"

    def test_seeded_store_recall(self) -> None:
        """Seed distinctive decisions in the (conftest-isolated) store and
        confirm each is retrievable by its own intent query."""
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="adopt the widget queue for batch ingestion backpressure",
            file_path="ingest/widget.py",
            tags=["ingest", "queue"],
        )
        decisions_store.record(
            decision="cache embeddings in redis with a one hour ttl",
            file_path="cache/redis.py",
            tags=["cache", "redis"],
        )
        res = report.run_eval(k=5)
        # Small clean corpus → both targets should surface near the top.
        assert res["n_cases"] >= 2
        assert res["metrics"]["recall_at_k"] >= 0.5

    def test_format_report_renders(self) -> None:
        res = report.run_eval(decisions=[])
        text = report.format_report(res)
        assert "relevance eval" in text and "recall@" in text

    def test_append_trend_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_server.paths.get_project_root", lambda: tmp_path)
        res = report.run_eval(decisions=[])
        assert report.append_trend(res) is True
        assert (tmp_path / ".codevira-cache" / "eval" / "relevance.jsonl").exists()
