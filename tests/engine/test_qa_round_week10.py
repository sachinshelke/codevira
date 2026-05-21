"""
test_qa_round_week10.py — Integrated QA across Weeks 1-10 (7 heroes shipped).

Why this file exists
====================

Per the user's "did you ACTUALLY do unbiased QA on Weeks 9-10?" challenge.
The Week 10 commit said "10/10 mutations caught" — true at the unit level.
But I had NOT done the integration QA round across Weeks 1-10 that I did
across Weeks 1-9 in test_qa_round_week9.py. This file fills that gap.

Two new event-type policies shipped recently:
  - Hero 7 (Week 9): first PostToolUse policy → caught Bug 4 in Week-9 round
  - Hero 10 (Week 10): first SessionStart policy → this round looks for
    a Bug-4-shape risk in the SessionStart wiring path

Specific seams Week 10 introduces
=================================

1. signals.outcomes / signals.learned_rules share _decisions_cache slot
   — verify no key-collision in pathological cases
2. CODEVIRA_ENGINE=0 kill switch must also short-circuit SessionStart
3. Hero 10 crash must not break SessionStart dispatch (multi-policy)
4. Hero 10's enabled_by_default=False must opt out (Bug 3 regression for Hero 10)
5. Multi-inject combination on SessionStart (priority ordering, concatenation)
6. CLI _parse_since must warn on malformed input (silent fallback = bug shape)
7. CLI must NOT suggest locking on already-locked decisions (would be noise)
8. Bug-4 lesson: SessionStart through claude_code_hooks JSON wiring with
   mode=off / no outcomes / both → silent allow (NOT inject)

Round structure
===============

J1-J3:  Default registration (now 7 heroes); event-type partition with H10
J4:     CODEVIRA_ENGINE=0 also kills SessionStart
J5:     Hero 10 crash isolation in multi-policy SessionStart
J6:     Hero 10's enabled_by_default=False (Bug 3 regression for H10)
J7:     signals.outcomes / signals.decisions cache non-collision
J8-J11: Multi-inject ordering + wiring path edge cases
J12-J14: CLI parse_since + locked-decision-no-suggestion
J15:    promotion_score graceful failure on schema mismatch
M11-M15: Manual mutations on the new seams
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


# =====================================================================
# Fixtures
# =====================================================================


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
def _isolate_engine(monkeypatch: pytest.MonkeyPatch):
    from mcp_server.engine.runner import reset_policies

    reset_policies()
    for env in (
        "CODEVIRA_ENGINE",
        "CODEVIRA_DECISION_LOCK_MODE",
        "CODEVIRA_ANTI_REGRESSION_MODE",
        "CODEVIRA_BLAST_RADIUS_MODE",
        "CODEVIRA_CROSS_SESSION_MODE",
        "CODEVIRA_TOKEN_BUDGET_MODE",
        "CODEVIRA_LIVE_STYLE_MODE",
        "CODEVIRA_AI_PROMOTION_MODE",
        "CODEVIRA_AI_PROMOTION_MIN_SCORE",
        "CODEVIRA_AI_PROMOTION_MIN_OUTCOMES",
        "CODEVIRA_AI_PROMOTION_MAX_INJECT",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_policies()


def _set_project(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()


def _open_graph(project: Path):
    from mcp_server.paths import get_data_dir
    from indexer.sqlite_graph import SQLiteGraph

    graph_db = get_data_dir() / "graph" / "graph.db"
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteGraph(graph_db)


def _ensure_session(g, session_id: str = "s1") -> None:
    g.conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, summary) VALUES (?, ?)",
        (session_id, "qa round 10"),
    )


def _plant_stable_decision(
    g, file_path: str, decision_text: str, kept: int = 5, locked: int = 0
) -> int:
    """Plant a decision + N kept outcomes. Returns decision id."""
    _ensure_session(g)
    cur = g.conn.execute(
        "INSERT INTO decisions (session_id, decision, file_path, "
        "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        ("s1", decision_text, file_path, "ctx"),
    )
    did = cur.lastrowid
    if locked:
        g.conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path, do_not_revert) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{file_path}:locked", "function", "locked", file_path, locked),
        )
    for _ in range(kept):
        g.record_outcome(
            session_id="s1",
            file_path=file_path,
            outcome_type="kept",
            decision_id=did,
        )
    g.conn.commit()
    return did


# =====================================================================
# J1-J3 — Updated default-registration + event-type partition (with H10)
# =====================================================================


class TestJ1_DefaultRegistration:
    def test_default_heroes_registered_after_week_11(self):
        """As of Week 11, the default set is 8 heroes. Future heroes
        must update this assertion explicitly — drift in either direction
        (missing or unexpected) is a bug.
        """
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
        )

        register_default_policies()
        names = {p.name for p in registered_policies()}
        expected = {
            "blast_radius_veto",  # Hero 4 (Week 4)
            "decision_lock",  # Hero 1 (Week 5)
            "relevance_inject",  # Hero 5 (Week 6)
            "token_budget_persist",  # Hero 6 (Week 7)
            "anti_regression",  # Hero 2 (Week 8)
            "live_style_enforcement",  # Hero 7 (Week 9)
            "ai_promotion_score",  # Hero 10 (Week 10)
            "intent_inference",  # Hero 9 (Week 11)
            "scope_contract_lock",  # Hero 3 (Week 12)
            "post_edit_graph_refresh",  # v2.1.2 Item 4
        }
        assert names == expected, (
            f"Default hero set drift — got {sorted(names)}, "
            f"expected {sorted(expected)}"
        )

    def test_session_start_eligible_includes_only_hero_10(self):
        """Hero 10 is the ONLY policy on SESSION_START. If a future
        policy adds itself there, this test must be updated explicitly
        — not silently."""
        from mcp_server.engine import register_default_policies, registered_policies
        from mcp_server.engine.events import EventType

        register_default_policies()
        ss_eligible = {
            p.name
            for p in registered_policies()
            if EventType.SESSION_START in set(p.handles)
        }
        assert ss_eligible == {
            "ai_promotion_score"
        }, f"SessionStart eligibility drift: {ss_eligible}"

    def test_hero_10_silent_on_pre_post_user_prompt_stop(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Hero 10 must NOT fire on PRE_TOOL_USE / POST_TOOL_USE /
        USER_PROMPT_SUBMIT / STOP — even with rich outcomes data planted.
        Catches the symmetric failure mode of Bug 4 (Hero 7 was supposed
        to fire on Write but didn't; here we ensure Hero 10 is supposed
        NOT to fire on these and indeed doesn't)."""
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _plant_stable_decision(g, "auth.py", "use bcrypt", kept=5)
        g.close()

        reset_policies()
        register_default_policies()

        for evt_type in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.USER_PROMPT_SUBMIT,
            EventType.STOP,
        ):
            event = HookEvent(
                event_type=evt_type,
                project_root=isolated_project,
                tool_name="Edit" if "TOOL" in evt_type.name else "",
                target_file=isolated_project / "auth.py"
                if "TOOL" in evt_type.name
                else None,
            )
            v = dispatch(event)
            # Hero 10 must NOT inject on these. Other heroes may allow,
            # warn, or block — but Hero 10's contribution should be NONE.
            inject_policies = v.metadata.get("inject_policies", [])
            assert (
                "ai_promotion_score" not in inject_policies
            ), f"Hero 10 fired on {evt_type} — handles drift!"


# =====================================================================
# J4 — CODEVIRA_ENGINE=0 also kills SessionStart dispatch
# =====================================================================


class TestJ4_KillSwitchHonorsSessionStart:
    def test_engine_disabled_short_circuits_session_start(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The kill switch was tested with PRE_TOOL_USE in Week-9 round
        but not with SESSION_START. Lock it in for the new event type."""
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _plant_stable_decision(g, "auth.py", "use bcrypt", kept=5)
        g.close()

        reset_policies()
        register_default_policies()
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")

        event = HookEvent(
            event_type=EventType.SESSION_START,
            project_root=isolated_project,
            ai_tool="claude-code",
            session_id="x",
        )
        v = dispatch(event)
        # Without kill switch: would inject. With kill switch: allow + metadata.
        assert v.action == "allow"
        assert v.metadata.get("engine_disabled") is True


# =====================================================================
# J5 — Hero 10 crash isolation in multi-policy SessionStart
# =====================================================================


class TestJ5_CrashIsolation:
    def test_crashing_h10_does_not_break_session_start_dispatch(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If Hero 10 raises, the engine must log + treat as allow,
        and OTHER SessionStart policies (when they ship) must still run.
        Today there's only Hero 10 on SessionStart, but adding a second
        sentinel policy lets us prove the isolation now — before the
        next SessionStart hero ships."""
        from mcp_server.engine import register_policy, dispatch, reset_policies
        from mcp_server.engine.policy import Policy, PolicyVerdict
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.ai_promotion import AIPromotionScore

        # Sabotage Hero 10's evaluate to raise.
        _orig = AIPromotionScore.evaluate

        def crashing_evaluate(self, event, signals=None):
            raise RuntimeError("intentional Hero 10 crash for crash-isolation test")

        monkeypatch.setattr(AIPromotionScore, "evaluate", crashing_evaluate)

        sentinel_called = {"n": 0}

        class SecondSessionStartPolicy(Policy):
            name = "sentinel_after_h10"
            handles = (EventType.SESSION_START,)
            priority = 5  # lower; runs after H10

            def evaluate(self, event, signals=None):
                sentinel_called["n"] += 1
                return PolicyVerdict.allow()

        reset_policies()
        register_policy(AIPromotionScore())
        register_policy(SecondSessionStartPolicy())

        event = HookEvent(
            event_type=EventType.SESSION_START,
            project_root=isolated_project,
        )
        v = dispatch(event)
        # Sentinel ran despite Hero 10 raising
        assert sentinel_called["n"] == 1, (
            "Crash isolation broken: SessionStart sentinel didn't run "
            "after Hero 10 raised"
        )
        # Total verdict is allow (Hero 10 treated as allow on raise)
        assert v.action == "allow"


# =====================================================================
# J6 — Hero 10's enabled_by_default=False (Bug 3 regression for H10)
# =====================================================================


class TestJ6_Bug3RegressionForHero10:
    def test_setting_h10_disabled_excludes_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Bug 3 was: enabled_by_default=False had zero effect; the
        register_default_policies helper ignored it. The fix landed in
        Week 7. Re-verify it still works for the newest hero."""
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
        )
        from mcp_server.engine.policies.ai_promotion import AIPromotionScore

        monkeypatch.setattr(
            AIPromotionScore,
            "enabled_by_default",
            False,
        )
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "ai_promotion_score" not in names, (
            "Bug 3 regression for Hero 10: register_default_policies "
            "did NOT honor enabled_by_default=False"
        )
        # The other 6 heroes still register (Bug 3 fix is per-hero, not global)
        assert "decision_lock" in names
        assert "anti_regression" in names


# =====================================================================
# J7 — signals.outcomes / signals.decisions cache non-collision
# =====================================================================


class TestJ7_CacheNonCollision:
    def test_outcomes_and_decisions_caches_do_not_collide(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The Week-10 commit reused _decisions_cache for outcomes() and
        learned_rules() — flagged in execution log as a "Surprise" but
        never tested. Verify pathological keys do NOT collide.

        Specifically: a decision with file_path='outcomes' would key into
        _decisions_cache as ('outcomes', False, 20). signals.outcomes()
        with default args would key as ('outcomes', 30, 2, 100). Different
        tuple lengths → different hashes → no collision."""
        from mcp_server.engine.signals import SignalContext

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        # Plant a decision whose file_path is the literal string "outcomes"
        # (the cache-key collision risk).
        _ensure_session(g)
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "test", "outcomes", "ctx"),
        )
        did = cur.lastrowid
        for _ in range(3):
            g.record_outcome(
                session_id="s1",
                file_path="outcomes",
                outcome_type="kept",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        ctx = SignalContext(project_root=isolated_project)

        # Cold cache. First: get decisions for file 'outcomes' (3-tuple key).
        decs = ctx.decisions(file="outcomes")
        assert len(decs) == 1
        assert "decision" in decs[0]
        # Decisions row should NOT have outcome-aggregation keys.
        assert (
            "kept" not in decs[0]
        ), "decisions() returned outcome-shape row — cache collision!"

        # Now: get aggregated outcomes (4-tuple key starting with 'outcomes').
        outs = ctx.outcomes(since_days=30, min_outcomes=2)
        assert len(outs) == 1
        assert outs[0]["kept"] == 3
        assert (
            "score" in outs[0]
        ), "outcomes() returned plain decision row — cache collision!"

        # And: same calls again hit the cache (verify by checking we got
        # the SAME list object).
        decs2 = ctx.decisions(file="outcomes")
        assert decs2 is decs, "decisions cache not hit on second call"
        outs2 = ctx.outcomes(since_days=30, min_outcomes=2)
        assert outs2 is outs, "outcomes cache not hit on second call"

    def test_search_decisions_and_outcomes_caches_do_not_collide(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """search_decisions uses key ('search', query, limit). outcomes
        uses key ('outcomes', ...). learned_rules uses ('rules', ...).
        Different namespaces → no collision. Verify in-process."""
        from mcp_server.engine.signals import SignalContext

        _set_project(monkeypatch, isolated_project)
        # v2.2.0+: search_decisions reads via the JSONL FTS5 backend.
        from mcp_server.storage import decisions_store, paths as store_paths

        store_paths.ensure_dirs()
        decisions_store.record(
            "search-test pattern",
            file_path="f.py",
            session_id="s1",
            context="ctx",
        )

        ctx = SignalContext(project_root=isolated_project)
        sd = ctx.search_decisions("search-test")
        outs = ctx.outcomes()
        rules = ctx.learned_rules()
        # All three return list shapes; verify they're independent.
        assert isinstance(sd, list)
        assert isinstance(outs, list)
        assert isinstance(rules, list)
        # search_decisions found our planted row by its decision text
        assert any(
            "search-test" in (d.get("decision") or "") for d in sd
        ), f"search_decisions didn't return planted row: {sd}"


# =====================================================================
# J8-J11 — Wiring path edge cases (Bug-4 lesson)
# =====================================================================


class TestJ8_WiringPathEdgeCases:
    def test_session_start_with_no_outcomes_emits_no_inject(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end through claude_code_hooks.handle("SessionStart")
        on a project with NO outcomes. Hero 10 should silently allow —
        the wiring layer should emit a plain {"continue": true} payload
        with NO additionalContext (no noise on cold-start projects)."""
        from mcp_server.engine import register_default_policies, reset_policies
        from mcp_server.engine.wiring import claude_code_hooks

        _set_project(monkeypatch, isolated_project)
        # Create empty graph DB
        g = _open_graph(isolated_project)
        g.close()

        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "abc",
            "cwd": str(isolated_project),
            "source": "startup",
            "model": "claude",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("SessionStart")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        assert emitted.get("continue") is True
        # No inject = no hookSpecificOutput.additionalContext (or empty)
        ctx = emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Codevira insights" not in ctx, (
            f"Hero 10 emitted inject content with no outcomes — should be silent. "
            f"Emitted: {emitted}"
        )

    def test_session_start_with_mode_off_emits_no_inject(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end through wiring with CODEVIRA_AI_PROMOTION_MODE=off.
        Even with high-score outcomes planted, no inject is emitted."""
        from mcp_server.engine import register_default_policies, reset_policies
        from mcp_server.engine.wiring import claude_code_hooks

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _plant_stable_decision(g, "auth.py", "use bcrypt", kept=5)
        g.close()

        reset_policies()
        register_default_policies()
        monkeypatch.setenv("CODEVIRA_AI_PROMOTION_MODE", "off")

        raw = {
            "session_id": "abc",
            "cwd": str(isolated_project),
            "source": "startup",
            "model": "claude",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("SessionStart")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        assert emitted.get("continue") is True
        # Even with planted high-score data, mode=off means no inject.
        ctx = emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert (
            "bcrypt" not in ctx
        ), f"mode=off didn't suppress Hero 10 inject. Emitted: {emitted}"

    def test_session_start_with_outcomes_emits_inject(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Positive control for J8 + J9 — verify our wiring path actually
        produces an inject when conditions are met."""
        from mcp_server.engine import register_default_policies, reset_policies
        from mcp_server.engine.wiring import claude_code_hooks

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _plant_stable_decision(g, "auth.py", "use bcrypt over argon2", kept=5)
        g.close()

        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "abc",
            "cwd": str(isolated_project),
            "source": "startup",
            "model": "claude",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("SessionStart")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        ctx = emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "bcrypt" in ctx, (
            f"Positive control failed: Hero 10 should have injected on "
            f"high-score data. Emitted: {emitted}"
        )

    def test_multiple_inject_policies_concatenate_in_priority_order(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Verify the inject combiner concatenates by priority DESC.
        Today only Hero 10 fires on SessionStart, but a future hero may
        also be inject-on-SessionStart. Lock the contract now."""
        from mcp_server.engine import (
            register_policy,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policy import Policy, PolicyVerdict

        class HighPriorityInject(Policy):
            name = "high_inject"
            handles = (EventType.SESSION_START,)
            priority = 200

            def evaluate(self, event, signals=None):
                return PolicyVerdict(
                    action="inject", inject_context="HIGH-CTX", policy=self.name
                )

        class LowPriorityInject(Policy):
            name = "low_inject"
            handles = (EventType.SESSION_START,)
            priority = 1

            def evaluate(self, event, signals=None):
                return PolicyVerdict(
                    action="inject", inject_context="LOW-CTX", policy=self.name
                )

        reset_policies()
        register_policy(HighPriorityInject())
        register_policy(LowPriorityInject())

        event = HookEvent(
            event_type=EventType.SESSION_START,
            project_root=isolated_project,
        )
        v = dispatch(event)
        assert v.action == "inject"
        assert "HIGH-CTX" in v.inject_context
        assert "LOW-CTX" in v.inject_context
        # Order: high-priority first
        assert v.inject_context.index("HIGH-CTX") < v.inject_context.index("LOW-CTX")
        # Metadata records both inject policies in priority order
        ip = v.metadata.get("inject_policies", [])
        assert ip == [
            "high_inject",
            "low_inject",
        ], f"inject_policies metadata order wrong: {ip}"


# =====================================================================
# J12-J14 — CLI _parse_since + locked-decision-no-suggestion
# =====================================================================


class TestJ12_CLIParseSince:
    def test_parse_since_warns_on_malformed_input(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """Silent fallback would be a Bug-3-shape failure: user thinks
        their --since=garbage is honored; actually it's silently 7d.
        Lock that there's a stderr warning."""
        from mcp_server.cli_insights import _parse_since

        result = _parse_since("garbage")
        captured = capsys.readouterr()
        assert result == 7, f"Expected default 7, got {result}"
        assert (
            "warning" in captured.err.lower() or "ignoring" in captured.err.lower()
        ), f"Malformed --since must warn on stderr; got stderr={captured.err!r}"

    def test_parse_since_clamps_huge_values(self):
        from mcp_server.cli_insights import _parse_since

        assert _parse_since("999d") == 365  # ceiling
        assert _parse_since("0d") == 1  # floor
        assert _parse_since("-5d") == 7  # negative falls through regex → default

    def test_parse_since_handles_valid_units(self):
        from mcp_server.cli_insights import _parse_since

        assert _parse_since("7d") == 7
        assert _parse_since("30") == 30  # bare number
        assert _parse_since("14days") == 14
        assert _parse_since("1day") == 1
        assert _parse_since(" 7 d ") == 7  # whitespace tolerated


class TestJ13_LockedDecisionNoSuggestion:
    def test_cli_omits_locking_suggestion_for_already_locked_reverted(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A decision that's both reverted AND already locked should NOT
        get the "consider locking" suggestion — that would be useless
        noise. Tests the `if not locked:` branch in _fmt_reverted_section.
        """
        from mcp_server.cli_insights import cmd_insights

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _ensure_session(g)
        # Plant a LOCKED node + a decision on it + reverted outcomes
        g.conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path, do_not_revert) "
            "VALUES (?, ?, ?, ?, ?)",
            ("style.css:locked", "function", "locked", "style.css", 1),
        )
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "Bootstrap not Tailwind", "style.css", "ctx"),
        )
        did = cur.lastrowid
        for _ in range(4):
            g.record_outcome(
                session_id="s1",
                file_path="style.css",
                outcome_type="reverted",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        out_buf = io.StringIO()
        rc = cmd_insights(
            since="30d",
            top=5,
            project=isolated_project,
            min_outcomes=1,
            ascii_mode=True,
            out=out_buf,
        )
        assert rc == 0
        out = out_buf.getvalue()
        # The reverted decision IS shown
        assert "Bootstrap not Tailwind" in out
        # But NO suggestion to lock (it's already locked)
        assert (
            "consider locking" not in out.lower()
        ), f"CLI suggested locking an already-locked decision (noise): {out}"

    def test_cli_suggestion_appears_for_unlocked_reverted(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Positive control for J13 — the suggestion DOES appear when
        the reverted decision is unlocked."""
        from mcp_server.cli_insights import cmd_insights

        _set_project(monkeypatch, isolated_project)
        g = _open_graph(isolated_project)
        _ensure_session(g)
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "Bootstrap not Tailwind", "style.css", "ctx"),
        )
        did = cur.lastrowid
        for _ in range(4):
            g.record_outcome(
                session_id="s1",
                file_path="style.css",
                outcome_type="reverted",
                decision_id=did,
            )
        g.conn.commit()
        g.close()

        out_buf = io.StringIO()
        rc = cmd_insights(
            since="30d",
            top=5,
            project=isolated_project,
            min_outcomes=1,
            ascii_mode=True,
            out=out_buf,
        )
        assert rc == 0
        out = out_buf.getvalue()
        assert "consider locking" in out.lower(), (
            f"Unlocked reverted decision should get a locking suggestion. "
            f"CLI output: {out}"
        )


class TestJ14_PromotionScoreGracefulFailure:
    def test_aggregate_returns_empty_on_schema_mismatch(self, tmp_path: Path):
        """Bug-1-shape defense: if the SQL hits a missing column, the
        aggregator must return [] (silent), not raise. Hero 10 is
        advisory; data layer flakiness must not break SessionStart."""
        import sqlite3
        from mcp_server.engine.promotion_score import (
            aggregate_decision_outcomes,
            top_stable_decisions,
            top_rules,
        )

        # Build a deliberately-incompatible schema
        bad_db = tmp_path / "bad.db"
        conn = sqlite3.connect(str(bad_db))
        # Missing the `decisions` table entirely → all queries fail
        conn.execute("CREATE TABLE wrong_table (x INTEGER)")
        conn.row_factory = sqlite3.Row

        rows = aggregate_decision_outcomes(conn, since_days=30, min_outcomes=2)
        assert rows == [], "aggregate should return [] on schema mismatch"

        stable = top_stable_decisions(
            conn, since_days=30, min_outcomes=2, min_score=0.7, max_items=5
        )
        assert stable == [], "top_stable should return [] on schema mismatch"

        rules = top_rules(conn, min_confidence=0.7, max_items=5)
        assert rules == [], "top_rules should return [] on schema mismatch"

        conn.close()


# =====================================================================
# J15 — Hero 7 (Week 9) Bug-4 fix doesn't regress on Edit format
# =====================================================================


class TestJ15_Hero7Bug4FixStillCorrect:
    def test_hero_7_still_extracts_edit_after_block(self):
        """Re-verify Bug 4 fix didn't break Edit format parsing.
        The fix made _extract_after_block handle BOTH formats:
          - Edit: --- before / --- after markers
          - Write: raw content (no markers)
        """
        from mcp_server.engine.policies.live_style import _extract_after_block

        # Edit format: marker present; only AFTER block extracted.
        edit = "--- before\nold_text\nmore_old\n--- after\ndef new_fn(): pass\n"
        out = _extract_after_block(edit)
        assert "def new_fn" in out, "Edit-format AFTER block missing"
        assert (
            "old_text" not in out
        ), "Bug 4 fix regression: Edit-format BEFORE block leaked into AFTER"
        assert "--- before" not in out
        assert "--- after" not in out

        # Write format: no marker; whole input IS the after-block.
        write = "def get_user(): pass\n"
        out = _extract_after_block(write)
        assert out == write

        # Empty after section: marker but empty content
        empty_after = "--- before\nold\n--- after\n"
        out = _extract_after_block(empty_after)
        assert out == "", "Empty AFTER block should return empty string"


# =====================================================================
# Mutation tests on the new seams (M11-M15)
# =====================================================================
# Documented; verified manually during this round. Each mutation was
# applied to the source, pytest run, mutation reverted.
#
# M11: Drop the try/except wrapping signals.outcomes call in
#      ai_promotion.evaluate. RuntimeError from a corrupted outcomes
#      table would propagate out of dispatch.
#      → Caught by TestEdgeCases::test_signals_outcomes_raises_does_not_break_policy
#        (in test_ai_promotion.py)
#
# M12: Make _parse_since silently return default for malformed input
#      (drop the stderr warning).
#      → Caught by TestJ12_CLIParseSince::test_parse_since_warns_on_malformed_input
#
# M13: Drop the `if not locked:` check in _fmt_reverted_section.
#      → Caught by TestJ13::test_cli_omits_locking_suggestion_for_already_locked_reverted
#
# M14: Drop the score field assignment in aggregate_decision_outcomes.
#      → Caught by TestI4 (score field check) + TestEngineDispatch
#        (Hero 10 wouldn't find score field for filter)
#
# M15: Drop the `since_days` clamp in promotion_score._clamp_since_days.
#      → Caught by TestJ12::test_parse_since_clamps_huge_values for the
#        CLI side; promotion_score side is harder to catch
#        directly — documented as an at-risk path for v2.1 to add
#        a per-function bound test.
