"""Phase 13 — learned hot-path weight tuning.

Pins: faithful composite scoring, the tuner's conservatism guards (min cases,
meaningful-improvement-only, persist-only-on-win), atomic persistence, and the
OPT-IN hot-path read with transparent fallback. The learner can never make the
read surface worse than it ships.
"""

from __future__ import annotations


import pytest

from mcp_server.eval import composite, weight_tuner
from mcp_server.storage import learned_weights


# ─────────────────────────────────────────────────────────────────────
# Composite scoring fidelity (matches relevance_inject._score_candidates)
# ─────────────────────────────────────────────────────────────────────


class TestCompositeScore:
    def test_components_and_outcome(self) -> None:
        w = {"tag": 0.4, "file": 0.4, "fts": 0.2}
        dec = {
            "id": "D1",
            "tags": ["auth", "bcrypt"],
            "file_path": "src/auth.py",
            "weight": 1.0,
        }
        q = {"auth", "password"}  # one tag hit + file stem hit
        # tag 0.4·1 + file 0.4 + fts 0.2·0.5^0 = 1.0, × outcome 1.0
        assert abs(composite.composite_score(q, dec, 0, w) - 1.0) < 1e-9

    def test_outcome_weight_scales(self) -> None:
        w = {"tag": 0.4, "file": 0.4, "fts": 0.2}
        dec = {"id": "D1", "tags": ["auth"], "file_path": "x.py", "outcome": "reverted"}
        q = {"auth"}
        # tag 0.4 × outcome 0.2 = 0.08
        assert abs(composite.composite_score(q, dec, None, w) - 0.08) < 1e-9

    def test_no_match_is_zero(self) -> None:
        w = {"tag": 0.4, "file": 0.4, "fts": 0.2}
        dec = {"id": "D1", "tags": ["caching"], "file_path": "redis.py"}
        assert composite.composite_score({"auth"}, dec, None, w) == 0.0


# ─────────────────────────────────────────────────────────────────────
# Tuner guards + persist-only-on-win
# ─────────────────────────────────────────────────────────────────────


def _decs(n: int) -> list[dict]:
    return [
        {
            "id": f"D{i}",
            "file_path": f"a/m{i}.py",
            "tags": [f"t{i}"],
            "decision": f"topic{i} alpha",
        }
        for i in range(n)
    ]


class TestTuner:
    def test_too_few_cases_keeps_defaults(self) -> None:
        res = weight_tuner.tune(decisions=_decs(5), persist=False)
        assert res["status"] == "too_few_cases"
        assert res["persisted"] is False

    def test_meaningful_win_is_chosen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            composite, "build_pools", lambda cases, **k: [(c, set(), []) for c in cases]
        )
        winner = {"tag": 0.1, "file": 0.1, "fts": 0.6}

        def fake_eval(cases, weights, *, k=5, pools=None):
            r = 0.9 if weights == winner else 0.5
            return {"recall_at_k": r, "mrr": r, "n": len(cases)}

        monkeypatch.setattr(composite, "evaluate_weights", fake_eval)
        res = weight_tuner.tune(decisions=_decs(25), persist=False)
        assert res["improved"] is True
        assert res["best_weights"] == winner
        assert res["improvement"] >= 0.02

    def test_sub_threshold_gain_keeps_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            composite, "build_pools", lambda cases, **k: [(c, set(), []) for c in cases]
        )

        def fake_eval(cases, weights, *, k=5, pools=None):
            # default 0.5; a slightly-better vector 0.51 (gain 0.01 < 0.02)
            r = 0.51 if weights == {"tag": 0.1, "file": 0.1, "fts": 0.1} else 0.5
            return {"recall_at_k": r, "mrr": r, "n": len(cases)}

        monkeypatch.setattr(composite, "evaluate_weights", fake_eval)
        res = weight_tuner.tune(decisions=_decs(25), persist=True)
        # A sub-threshold win is reported transparently but NOT applied.
        assert res["improved"] is False
        assert res["persisted"] is False
        assert learned_weights.load() is None  # nothing written → defaults stay live

    def test_persists_on_real_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            composite, "build_pools", lambda cases, **k: [(c, set(), []) for c in cases]
        )
        winner = {"tag": 0.2, "file": 0.1, "fts": 0.5}
        monkeypatch.setattr(
            composite,
            "evaluate_weights",
            lambda cases, weights, *, k=5, pools=None: {
                "recall_at_k": 0.95 if weights == winner else 0.5,
                "mrr": 0.95 if weights == winner else 0.5,
                "n": len(cases),
            },
        )
        res = weight_tuner.tune(decisions=_decs(25), persist=True)
        assert res["persisted"] is True
        assert learned_weights.load() == winner  # round-trips from disk

    def test_default_in_grid_so_best_never_below_default(self) -> None:
        # Real run on a seeded isolated corpus: best is ALWAYS ≥ default.
        from mcp_server.storage import decisions_store

        for i in range(25):
            decisions_store.record(
                decision=f"topic{i} distinctive alpha beta gamma",
                file_path=f"src/mod{i}.py",
                tags=[f"area{i}"],
            )
        res = weight_tuner.tune(persist=False)
        if res["status"] == "ok":
            assert (
                res["best_metric"]["recall_at_k"]
                >= res["default_metric"]["recall_at_k"] - 1e-9
            )


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────


class TestLearnedWeights:
    def test_round_trip(self) -> None:
        assert learned_weights.save({"tag": 0.1, "file": 0.2, "fts": 0.7})
        assert learned_weights.load() == {"tag": 0.1, "file": 0.2, "fts": 0.7}

    def test_missing_returns_none(self) -> None:
        assert learned_weights.load() is None

    def test_malformed_returns_none(self) -> None:
        p = learned_weights.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid json")
        assert learned_weights.load() is None


# ─────────────────────────────────────────────────────────────────────
# Hot-path opt-in + fallback (the safety contract)
# ─────────────────────────────────────────────────────────────────────


class TestHotPathOptIn:
    def _ri(self):
        import mcp_server.engine.policies.relevance_inject as ri

        return ri

    def test_default_off_uses_shipped_weights(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ri = self._ri()
        monkeypatch.delenv("CODEVIRA_LEARNED_WEIGHTS", raising=False)
        monkeypatch.setattr(ri, "_LEARNED_WEIGHTS_CACHE", ri._UNSET)
        assert ri._effective_weights() == (0.4, 0.4, 0.2)

    def test_opt_in_uses_learned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ri = self._ri()
        learned_weights.save({"tag": 0.1, "file": 0.2, "fts": 0.7})
        monkeypatch.setenv("CODEVIRA_LEARNED_WEIGHTS", "1")
        monkeypatch.setattr(ri, "_LEARNED_WEIGHTS_CACHE", ri._UNSET)
        assert ri._effective_weights() == (0.1, 0.2, 0.7)

    def test_opt_in_but_no_file_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ri = self._ri()
        monkeypatch.setenv("CODEVIRA_LEARNED_WEIGHTS", "1")
        monkeypatch.setattr(ri, "_LEARNED_WEIGHTS_CACHE", ri._UNSET)
        assert ri._effective_weights() == (0.4, 0.4, 0.2)  # no learned file → defaults
