"""
test_ai_promotion.py — Hero 10 acceptance + behavioral + mutation tests.

Tier-0 pre-flight from start (post-Bug-4 reinforcement):
  - Real outcomes recorded via SQLiteGraph.record_outcome (not mocked)
  - Behavioral spies on signals.outcomes calls
  - End-to-end dispatch test (registers all 7 heroes)
  - End-to-end through claude_code_hooks.handle("SessionStart")
    — Bug 4 lesson: every wiring path that hasn't been exercised
    end-to-end is a candidate for silent fail-open
  - 10+ mutations from start
  - Bug-shape audit (no Bug-3-class dead fields)
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.ai_promotion import (
    AIPromotionScore,
    _format_inject,
    _truncate,
)
from mcp_server.engine.promotion_score import (
    score_decision,
    aggregate_decision_outcomes,
)


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_session_start_event(
    *,
    project_root: Path | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=EventType.SESSION_START,
        project_root=project_root or Path("/p"),
        ai_tool="claude-code",
        session_id="test-session",
    )


class _FakeSignals:
    """Honors-args fake — outcomes() + learned_rules() return canned data
    AND record every call argument so tests can prove gates short-circuit
    BEFORE the signal fetch (the key Bug-4-shape defense)."""

    def __init__(
        self,
        *,
        outcomes_data: list[dict[str, Any]] | None = None,
        rules_data: list[dict[str, Any]] | None = None,
    ):
        self._outcomes = outcomes_data or []
        self._rules = rules_data or []
        self.outcomes_calls: list[dict[str, Any]] = []
        self.rules_calls: list[dict[str, Any]] = []

    def outcomes(
        self,
        *,
        since_days: int = 30,
        min_outcomes: int = 2,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.outcomes_calls.append(
            {
                "since_days": since_days,
                "min_outcomes": min_outcomes,
                "limit": limit,
            }
        )
        # Honor min_outcomes filter on the fake too — test code can plant
        # below-threshold rows and verify they get filtered.
        return [d for d in self._outcomes if d.get("total", 0) >= min_outcomes][:limit]

    def learned_rules(
        self,
        *,
        min_confidence: float = 0.7,
        max_items: int = 3,
    ) -> list[dict[str, Any]]:
        self.rules_calls.append(
            {
                "min_confidence": min_confidence,
                "max_items": max_items,
            }
        )
        return [r for r in self._rules if r.get("confidence", 0.0) >= min_confidence][
            :max_items
        ]


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    monkeypatch.setattr(
        "mcp_server.paths.get_global_home",
        lambda: cv_data,
    )
    return project


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        "CODEVIRA_AI_PROMOTION_MODE",
        "CODEVIRA_AI_PROMOTION_MIN_SCORE",
        "CODEVIRA_AI_PROMOTION_MIN_CONFIDENCE",
        "CODEVIRA_AI_PROMOTION_MAX_INJECT",
        "CODEVIRA_AI_PROMOTION_MIN_OUTCOMES",
    ):
        monkeypatch.delenv(env, raising=False)


# =====================================================================
# Acceptance tests (8 scenarios from spec)
# =====================================================================


class TestAcceptance:
    def test_1_non_session_start_event_allowed(self):
        """Non-SessionStart events pass through; outcomes() NOT called."""
        policy = AIPromotionScore()
        spy = _FakeSignals()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",
        )
        verdict = policy.evaluate(event, signals=spy)
        assert verdict.is_allowing()
        assert (
            spy.outcomes_calls == []
        ), "outcomes() must NOT be called for non-SessionStart events"

    def test_2_session_start_no_decisions_allow(self):
        """Cold project — no outcomes → allow."""
        policy = AIPromotionScore()
        spy = _FakeSignals(outcomes_data=[], rules_data=[])
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.is_allowing()

    def test_3_decisions_below_min_outcomes_allow(self):
        """Decision with 1 outcome (below default min_outcomes=2) → allow."""
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": 1,
                    "decision": "x",
                    "file_path": "a.py",
                    "kept": 1,
                    "modified": 0,
                    "reverted": 0,
                    "total": 1,
                    "score": 1.0,
                },
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.is_allowing()
        # outcomes() WAS called (it's the policy's job to filter).
        assert len(spy.outcomes_calls) == 1

    def test_4_high_score_decision_inject(self):
        """Happy path: one stable decision with score=1.0 → inject."""
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": 1,
                    "decision": "use bcrypt over argon2",
                    "file_path": "auth.py",
                    "kept": 5,
                    "modified": 0,
                    "reverted": 0,
                    "total": 5,
                    "score": 1.0,
                    "locked": 0,
                },
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.action == "inject"
        ctx = verdict.inject_context or ""
        assert "bcrypt over argon2" in ctx
        assert "auth.py" in ctx
        assert "1.00" in ctx  # the score

    def test_5_only_top_n_injected(self, monkeypatch: pytest.MonkeyPatch):
        """Many high-score decisions → cap at max_inject."""
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MAX_INJECT", "2")
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": i,
                    "decision": f"decision_{i}",
                    "file_path": f"f{i}.py",
                    "kept": 5,
                    "modified": 0,
                    "reverted": 0,
                    "total": 5,
                    "score": 1.0,
                    "locked": 0,
                }
                for i in range(10)
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.action == "inject"
        # Exactly 2 decisions appear in the inject (cap).
        # Each inject row starts with "1. " or "2. " etc. — count those.
        ctx = verdict.inject_context or ""
        count = ctx.count("decision_")
        assert count == 2, f"max_inject=2 not honored: {count} decisions in ctx"

    def test_6_off_mode_skips_outcomes_call(self, monkeypatch: pytest.MonkeyPatch):
        """mode=off must short-circuit BEFORE signals.outcomes is called."""
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MODE", "off")
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": 1,
                    "decision": "x",
                    "file_path": "a.py",
                    "kept": 5,
                    "modified": 0,
                    "reverted": 0,
                    "total": 5,
                    "score": 1.0,
                    "locked": 0,
                },
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.is_allowing()
        assert (
            spy.outcomes_calls == []
        ), "mode=off must short-circuit before outcomes() (perf + privacy)"

    def test_7_min_score_filter_excludes_below_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A decision with score=0.5 is excluded when min_score=0.7."""
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": 1,
                    "decision": "stable_x",
                    "file_path": "a.py",
                    "kept": 5,
                    "modified": 0,
                    "reverted": 0,
                    "total": 5,
                    "score": 1.0,
                    "locked": 0,
                },
                {
                    "id": 2,
                    "decision": "midstable_y",
                    "file_path": "b.py",
                    "kept": 1,
                    "modified": 0,
                    "reverted": 1,
                    "total": 2,
                    "score": 0.5,
                    "locked": 0,
                },
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.action == "inject"
        ctx = verdict.inject_context or ""
        assert "stable_x" in ctx
        assert (
            "midstable_y" not in ctx
        ), "min_score=0.7 should exclude score=0.5 decision"

    def test_8_high_confidence_rules_in_inject(self):
        """Rules above min_confidence appear in the inject."""
        policy = AIPromotionScore()
        spy = _FakeSignals(
            outcomes_data=[],  # no decisions
            rules_data=[
                {
                    "id": 1,
                    "rule_text": "Tests live in tests/ mirror layout",
                    "confidence": 0.85,
                    "category": "testing",
                },
            ],
        )
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.action == "inject"
        ctx = verdict.inject_context or ""
        assert "tests/ mirror layout" in ctx
        assert "[testing]" in ctx
        assert "0.85" in ctx


# =====================================================================
# Behavioral gates — the spies catch what verdict-only tests miss
# =====================================================================


class TestBehavioralGates:
    def test_event_type_gate_short_circuits_signals(self):
        """PreToolUse arriving at the policy must NOT touch signals."""
        policy = AIPromotionScore()
        spy = _FakeSignals()
        for evt_type in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.USER_PROMPT_SUBMIT,
            EventType.STOP,
        ):
            spy.outcomes_calls.clear()
            event = HookEvent(event_type=evt_type, project_root=Path("/p"))
            policy.evaluate(event, signals=spy)
            assert (
                spy.outcomes_calls == []
            ), f"{evt_type} must NOT trigger outcomes() call"

    def test_signals_none_gate(self):
        """signals=None → allow (defensive; runner should always pass them
        but legacy/test paths may not)."""
        policy = AIPromotionScore()
        verdict = policy.evaluate(_make_session_start_event(), signals=None)
        assert verdict.is_allowing()

    def test_priority_value_stable(self):
        """Priority is part of the public contract; lock at 10."""
        assert AIPromotionScore().priority == 10

    def test_handles_only_session_start(self):
        """handles must be exactly (SESSION_START,) — no Bug-3-shape drift."""
        assert AIPromotionScore.handles == (EventType.SESSION_START,)

    def test_enabled_by_default_true(self):
        """Required for register_default_policies inclusion."""
        assert AIPromotionScore.enabled_by_default is True

    def test_invalid_mode_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MODE", "block")  # invalid
        cfg = AIPromotionScore()._config()
        assert cfg["mode"] == "inject"  # default fallback

    def test_max_inject_clamped(self, monkeypatch: pytest.MonkeyPatch):
        # Below floor → clamped to 1
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MAX_INJECT", "0")
        assert AIPromotionScore()._config()["max_inject"] == 1
        # Above ceiling → clamped to 10
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MAX_INJECT", "999")
        assert AIPromotionScore()._config()["max_inject"] == 10
        # Garbage → default 3
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MAX_INJECT", "not-an-int")
        assert AIPromotionScore()._config()["max_inject"] == 3

    def test_min_score_clamped(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MIN_SCORE", "-1.0")
        assert AIPromotionScore()._config()["min_score"] == 0.0
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MIN_SCORE", "2.0")
        assert AIPromotionScore()._config()["min_score"] == 1.0
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MIN_SCORE", "garbage")
        assert AIPromotionScore()._config()["min_score"] == 0.7  # default


# =====================================================================
# Pure scoring unit tests
# =====================================================================


class TestScoreFunctions:
    def test_score_all_kept(self):
        assert score_decision(kept=5, modified=0, reverted=0) == 1.0

    def test_score_all_reverted(self):
        assert score_decision(kept=0, modified=0, reverted=5) == 0.0

    def test_score_modified_is_half_credit(self):
        # 0 kept + 5 modified + 0 reverted → 2.5/5 = 0.5
        assert score_decision(kept=0, modified=5, reverted=0) == 0.5

    def test_score_zero_total_is_zero(self):
        assert score_decision(kept=0, modified=0, reverted=0) == 0.0

    def test_score_negative_inputs_clamped(self):
        # Defensive — negative coerced to 0
        assert score_decision(kept=-1, modified=0, reverted=0) == 0.0

    def test_score_mixed(self):
        # 3 kept + 1 modified + 1 reverted → (3 + 0.5)/5 = 0.7
        assert abs(score_decision(kept=3, modified=1, reverted=1) - 0.7) < 1e-9


# =====================================================================
# Real-DB integration — Tier-0 pre-flight (Bug-1-class defense)
# =====================================================================


class TestRealDBIntegration:
    def test_aggregate_returns_correct_scores_from_real_outcomes(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Plant decisions + outcomes via the actual DB methods, query
        through aggregate_decision_outcomes, verify the score formula
        runs against real schema (catches Bug-1-class column drift)."""
        from indexer.sqlite_graph import SQLiteGraph
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "test"),
        )
        # Insert decision id=1 manually so we control the FK target
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "use bcrypt over argon2", "auth.py", "perf"),
        )
        decision_id = cur.lastrowid
        # Plant outcomes via the DB method (same path outcome_tracker uses)
        for _ in range(3):
            g.record_outcome(
                session_id="s1",
                file_path="auth.py",
                outcome_type="kept",
                decision_id=decision_id,
            )
        g.record_outcome(
            session_id="s1",
            file_path="auth.py",
            outcome_type="reverted",
            decision_id=decision_id,
        )
        g.conn.commit()

        rows = aggregate_decision_outcomes(
            g.conn,
            since_days=30,
            min_outcomes=2,
            limit=10,
        )
        g.close()
        assert len(rows) == 1
        d = rows[0]
        assert d["kept"] == 3
        assert d["reverted"] == 1
        assert d["total"] == 4
        # 3 kept + 0 modified + 1 reverted → 3/4 = 0.75
        assert abs(d["score"] - 0.75) < 1e-9

    def test_signals_outcomes_returns_rows_from_real_db(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Bug-1-class regression check: signals.outcomes() through real
        SignalContext + real graph returns scored rows (not silently [])."""
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine.signals import SignalContext
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "test"),
        )
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "x", "f.py", "ctx"),
        )
        did = cur.lastrowid
        for _ in range(3):
            g.record_outcome(
                session_id="s1",
                file_path="f.py",
                outcome_type="kept",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        ctx = SignalContext(project_root=isolated_project)
        rows = ctx.outcomes(since_days=30, min_outcomes=2)
        assert len(rows) == 1, (
            f"signals.outcomes returned {len(rows)} rows from real DB "
            f"with 3 outcomes planted. Bug 1-shape regression?"
        )
        assert rows[0]["score"] == 1.0


# =====================================================================
# End-to-end through dispatch() — all 7 heroes registered
# =====================================================================


class TestEngineDispatch:
    def test_hero_10_fires_through_dispatch(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real outcomes DB + dispatch() → inject. Catches Bug-2-class
        wiring bugs and Bug-4-class wiring path mismatches."""
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "x"),
        )
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "use bcrypt over argon2", "auth.py", "perf"),
        )
        did = cur.lastrowid
        for _ in range(5):
            g.record_outcome(
                session_id="s1",
                file_path="auth.py",
                outcome_type="kept",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        event = HookEvent(
            event_type=EventType.SESSION_START,
            project_root=isolated_project,
            ai_tool="claude-code",
            session_id="new-session",
        )
        verdict = dispatch(event)
        assert verdict.action == "inject", (
            f"Hero 10 must fire inject through dispatch with real outcomes; "
            f"got {verdict.action}"
        )
        assert "bcrypt over argon2" in (verdict.inject_context or "")
        reset_policies()

    def test_hero_10_fires_through_claude_code_wiring(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end through claude_code_hooks.handle("SessionStart").

        This is the BUG 4 LESSON test: every wiring path must be tested
        with realistic JSON, not just the dispatch unit. SessionStart was
        a brand-new event-type for the engine (Hero 10 is the first
        SESSION_START policy), so this is highest-risk wiring code.
        """
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
        )
        from mcp_server.engine.wiring import claude_code_hooks
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "x"),
        )
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "use bcrypt over argon2", "auth.py", "perf"),
        )
        did = cur.lastrowid
        for _ in range(5):
            g.record_outcome(
                session_id="s1",
                file_path="auth.py",
                outcome_type="kept",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        # Realistic Claude Code SessionStart payload
        raw_payload = {
            "session_id": "new-session-id",
            "cwd": str(isolated_project),
            "source": "startup",
            "model": "claude-sonnet-4-5",
        }
        stdin_buf = io.StringIO(json.dumps(raw_payload))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("SessionStart")
        assert rc == 0, "inject should yield exit 0 (continue)"
        emitted = json.loads(stdout_buf.getvalue())
        # Inject path: hookSpecificOutput.additionalContext (Round-5 schema fix)
        hso = emitted.get("hookSpecificOutput", {})
        ctx = hso.get("additionalContext", "")
        assert "bcrypt over argon2" in ctx, (
            "Bug-4-shape regression: Hero 10 didn't inject through "
            f"claude_code_hooks SessionStart wiring. Emitted: {emitted}"
        )
        assert hso.get("hookEventName") == "SessionStart"
        reset_policies()


# =====================================================================
# Registration
# =====================================================================


class TestRegistration:
    def test_register_default_policies_includes_hero_10(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "ai_promotion_score" in names

    def test_idempotent_with_seven_heroes(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        register_default_policies()  # idempotent
        names = [p.name for p in registered_policies()]
        for n in (
            "ai_promotion_score",
            "anti_regression",
            "blast_radius_veto",
            "relevance_inject",
            "decision_lock",
            "live_style_enforcement",
            "token_budget_persist",
        ):
            assert (
                names.count(n) == 1
            ), f"Idempotency broken — {n!r} appears {names.count(n)} times"


# =====================================================================
# Edge cases (Bug-shape-audit defenses)
# =====================================================================


class TestEdgeCases:
    def test_empty_outcomes_and_empty_rules_returns_allow(self):
        """If both signals are empty, no inject — silent (no noise)."""
        policy = AIPromotionScore()
        spy = _FakeSignals(outcomes_data=[], rules_data=[])
        verdict = policy.evaluate(_make_session_start_event(), signals=spy)
        assert verdict.is_allowing()

    def test_signals_outcomes_raises_does_not_break_policy(self):
        """signals.outcomes raising must not propagate — log + treat as []."""

        class CrashingSignals:
            def outcomes(self, **k):
                raise RuntimeError("DB locked")

            def learned_rules(self, **k):
                return []

        policy = AIPromotionScore()
        verdict = policy.evaluate(
            _make_session_start_event(),
            signals=CrashingSignals(),
        )
        assert verdict.is_allowing()

    def test_signals_rules_raises_does_not_break_policy(self):
        class CrashingRules:
            def outcomes(self, **k):
                return []

            def learned_rules(self, **k):
                raise RuntimeError("rules DB corrupted")

        policy = AIPromotionScore()
        verdict = policy.evaluate(
            _make_session_start_event(),
            signals=CrashingRules(),
        )
        assert verdict.is_allowing()

    def test_format_inject_truncates_long_decision_text(self):
        """Long decision text gets truncated so inject stays small."""
        long_text = "x" * 500
        out = _truncate(long_text, 80)
        assert len(out) <= 80
        assert out.endswith("…")

    def test_format_inject_strips_newlines_in_decisions(self):
        """Newlines in decision text would break the inject's bullet
        formatting. _truncate replaces them."""
        text = "line1\nline2\nline3"
        out = _truncate(text)
        assert "\n" not in out

    def test_locked_decision_marked_in_inject(self):
        """Decisions with locked=1 should display a 🔒 marker."""
        ctx = _format_inject(
            stable=[
                {
                    "id": 1,
                    "decision": "x",
                    "file_path": "a.py",
                    "kept": 5,
                    "modified": 0,
                    "reverted": 0,
                    "total": 5,
                    "score": 1.0,
                    "locked": 1,
                },
            ],
            rules=[],
        )
        assert "🔒" in ctx, "Locked decisions should show 🔒 marker"


# =====================================================================
# Performance bounds
# =====================================================================


class TestPerformance:
    def test_evaluate_session_start_with_100_decisions_under_10ms(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end perf check with realistic data volume."""
        import time
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine.signals import SignalContext
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)

        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "x"),
        )
        for i in range(100):
            cur = g.conn.execute(
                "INSERT INTO decisions (session_id, decision, file_path, "
                "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                ("s1", f"d_{i}", f"f{i}.py", "ctx"),
            )
            did = cur.lastrowid
            # 5 outcomes per decision = 500 outcomes total
            for _ in range(5):
                g.record_outcome(
                    session_id="s1",
                    file_path=f"f{i}.py",
                    outcome_type="kept",
                    decision_id=did,
                )
        g.conn.commit()
        g.close()

        policy = AIPromotionScore()
        ctx = SignalContext(project_root=isolated_project)
        event = _make_session_start_event(project_root=isolated_project)

        # Warm-up
        policy.evaluate(event, signals=ctx)

        durations = []
        for _ in range(50):
            ctx2 = SignalContext(project_root=isolated_project)  # cold cache
            t0 = time.perf_counter()
            policy.evaluate(event, signals=ctx2)
            durations.append((time.perf_counter() - t0) * 1000)

        durations.sort()
        p50 = durations[25]
        p95 = durations[47]
        assert p50 < 10.0, f"p50={p50:.2f}ms exceeds 10ms target"
        # p95 has more variance under GC pressure AND parallel test load
        # (full suite is ~500 concurrent tests); use a looser bound.
        # Median (p50) is the real perf signal; p95 just catches gross
        # regressions.
        assert p95 < 60.0, f"p95={p95:.2f}ms exceeds 60ms loose bound"
