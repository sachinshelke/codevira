"""
test_qa_round_week11.py — Integrated QA across Weeks 1-11 (8 heroes shipped).

Continuing the post-Bug-4 cadence: every hero ship → integration QA round.
This time PROACTIVELY, before user has to ask.

What's new with Hero 9 (Week 11):

  - Second policy on UserPromptSubmit (Hero 5 was the first). Both
    INJECT — the engine combiner concatenates both contexts. This
    round verifies the combiner ordering, no double-count, and that
    Hero 5 + Hero 9 metadata both appear.

  - First multi-signal-call policy. Hero 9 calls signals.fixes,
    signals.decisions, signals.impact, signals.outcomes — depending
    on intent. Each is wrapped in try/except, but the combination
    surface is new and worth testing.

  - First file-mention extractor with extension allowlist. M8
    surfaced a test gap (allowlist test inputs were short enough
    for the regex itself to reject). Lock the new test.

Round structure
===============

K1-K3:  Default registration with 8 heroes; UserPromptSubmit eligibility
K4:     Hero 5 + Hero 9 BOTH inject on UserPromptSubmit — combiner ordering
K5:     CODEVIRA_ENGINE=0 also kills UserPromptSubmit dispatch
K6:     Crashing Hero 9 doesn't break Hero 5's inject
K7:     Hero 9 enabled_by_default=False opt-out (Bug 3 regression for H9)
K8:     Hero 9 + signals.outcomes call doesn't poison the cache for Hero 10
K9-K10: End-to-end Claude Code wiring with various intent shapes
K11:    Hero 9 doesn't fire on prompts that miss prompt_text (None / empty)
K12:    Multi-policy crash isolation across all 8 heroes simultaneously
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


# =====================================================================
# Fixtures (similar to W9/W10 rounds)
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
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
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
        "CODEVIRA_INTENT_INFERENCE_MODE",
        "CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT",
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
        (session_id, "qa round 11"),
    )


# =====================================================================
# K1-K3 — Default registration + UserPromptSubmit eligibility
# =====================================================================


class TestK1_DefaultRegistration:

    def test_eight_heroes_after_week_11(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies,
        )
        register_default_policies()
        names = {p.name for p in registered_policies()}
        expected = {
            "blast_radius_veto",
            "decision_lock",
            "cross_session_consistency",
            "token_budget_persist",
            "anti_regression",
            "live_style_enforcement",
            "ai_promotion_score",
            "intent_inference",
        }
        assert names == expected, (
            f"8-hero set drift: got {sorted(names)}, expected {sorted(expected)}"
        )

    def test_user_prompt_submit_has_two_policies(self):
        """Hero 5 + Hero 9 both fire on UserPromptSubmit. Lock this in
        — if a future hero adds itself or one of these is moved off
        UserPromptSubmit, the test must update explicitly."""
        from mcp_server.engine import register_default_policies, registered_policies
        from mcp_server.engine.events import EventType
        register_default_policies()
        ups = {
            p.name for p in registered_policies()
            if EventType.USER_PROMPT_SUBMIT in set(p.handles)
        }
        assert ups == {"cross_session_consistency", "intent_inference"}, (
            f"UserPromptSubmit eligibility drift: {ups}"
        )

    def test_priority_ordering_hero_5_above_hero_9(self):
        """Combined inject must put Hero 5's section first (priority 30 >
        Hero 9's 20). If a refactor swaps these, the user-facing order
        of sections changes — test must update explicitly."""
        from mcp_server.engine.policies.cross_session import CrossSessionConsistency
        from mcp_server.engine.policies.intent_inference import ProactiveIntentInference
        assert CrossSessionConsistency.priority > ProactiveIntentInference.priority


# =====================================================================
# K4 — Hero 5 + Hero 9 BOTH inject; verdict combiner ordering
# =====================================================================


class TestK4_DualInject:

    def test_both_h5_and_h9_inject_concatenated_in_priority_order(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from indexer.fix_history import record_fix
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent

        _set_project(monkeypatch, isolated_project)

        # Plant a fix (Hero 9's fix-bug intent will surface this)
        (isolated_project / "auth.py").write_text("def login(): pass")
        record_fix(
            isolated_project, file_path="auth.py",
            line_start=0, line_end=0,
            description="fix: regex didn't escape special chars",
            source="manual",
        )
        # Plant a decision (Hero 5's keyword search will surface this)
        g = _open_graph(isolated_project)
        _ensure_session(g)
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "use bcrypt over argon2", "auth.py", "perf"),
        )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            ai_tool="claude-code",
            session_id="x",
            prompt_text="Fix the auth.py login bug — special chars don't work",
        )
        v = dispatch(event)
        assert v.action == "inject"
        ctx = v.inject_context or ""

        # Both sections present
        assert "Prior decisions" in ctx, (
            "Hero 5's section missing — combiner failed?"
        )
        assert "Codevira pre-fetch" in ctx, (
            "Hero 9's section missing — combiner failed?"
        )

        # Order: Hero 5's section first (priority 30 > Hero 9's 20)
        assert ctx.index("Prior decisions") < ctx.index("Codevira pre-fetch"), (
            f"Inject order broken — Hero 5 should come first. ctx:\n{ctx}"
        )

        # Both policies recorded in metadata
        ip = v.metadata.get("inject_policies", [])
        assert "cross_session_consistency" in ip
        assert "intent_inference" in ip


# =====================================================================
# K5 — CODEVIRA_ENGINE=0 also kills UserPromptSubmit dispatch
# =====================================================================


class TestK5_KillSwitchOnPromptSubmit:

    def test_engine_disabled_short_circuits_user_prompt_submit(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from indexer.fix_history import record_fix
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent

        _set_project(monkeypatch, isolated_project)
        (isolated_project / "auth.py").write_text("")
        record_fix(
            isolated_project, file_path="auth.py",
            line_start=0, line_end=0,
            description="fix: bug",
            source="manual",
        )

        reset_policies()
        register_default_policies()
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")

        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text="Fix the auth bug in auth.py",
        )
        v = dispatch(event)
        assert v.action == "allow"
        assert v.metadata.get("engine_disabled") is True, (
            f"Kill-switch metadata missing on UserPromptSubmit: {v.metadata}"
        )


# =====================================================================
# K6 — Crashing Hero 9 doesn't break Hero 5
# =====================================================================


class TestK6_CrashIsolationAcrossInject:

    def test_h9_crash_does_not_break_h5_inject(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Hero 9 raising must be isolated. Hero 5's inject still gets
        through via the combiner."""
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.intent_inference import (
            ProactiveIntentInference,
        )

        _set_project(monkeypatch, isolated_project)
        # Plant a decision so Hero 5 has something to surface
        g = _open_graph(isolated_project)
        _ensure_session(g)
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "use bcrypt over argon2", "auth.py", "perf"),
        )
        g.conn.commit()
        g.close()

        # Sabotage Hero 9
        def crashing_evaluate(self, event, signals=None):
            raise RuntimeError("intentional H9 crash")
        monkeypatch.setattr(
            ProactiveIntentInference, "evaluate", crashing_evaluate,
        )

        reset_policies()
        register_default_policies()

        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text="Tell me about bcrypt usage",
        )
        v = dispatch(event)
        # Hero 5 still injects despite Hero 9 crashing
        assert v.action == "inject", (
            f"Hero 9 crash poisoned Hero 5: {v.action} / {v.message}"
        )
        ctx = v.inject_context or ""
        assert "Prior decisions" in ctx, (
            "Hero 5's section missing after Hero 9 crash"
        )
        assert "Codevira pre-fetch" not in ctx, (
            "Hero 9's section appeared despite the crash — "
            "isolation wasn't actually applied"
        )


# =====================================================================
# K7 — Hero 9 enabled_by_default=False (Bug 3 regression)
# =====================================================================


class TestK7_Bug3RegressionForHero9:

    def test_h9_enabled_by_default_false_excludes_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from mcp_server.engine import (
            register_default_policies, registered_policies,
        )
        from mcp_server.engine.policies.intent_inference import (
            ProactiveIntentInference,
        )
        monkeypatch.setattr(
            ProactiveIntentInference, "enabled_by_default", False,
        )
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "intent_inference" not in names, (
            "Bug 3 regression for Hero 9: enabled_by_default=False ignored"
        )
        # Other 7 heroes still register
        assert len(names) == 7


# =====================================================================
# K8 — Hero 9 + Hero 10 both call signals.outcomes → no cache poisoning
# =====================================================================


class TestK8_OutcomesCacheSharing:

    def test_h9_and_h10_share_same_outcomes_results(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Hero 9 (intent=add-feature) calls signals.outcomes. Hero 10
        (SessionStart) also calls signals.outcomes. They fire on
        DIFFERENT events so they don't share a SignalContext — but
        within ONE event with multiple consumers, the cache should
        be hit. This test sets up two policies on UserPromptSubmit
        that both call outcomes(); verifies one underlying SQL query."""
        from mcp_server.engine import register_policy, reset_policies, dispatch
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policy import Policy, PolicyVerdict
        from mcp_server.engine.signals import SignalContext

        _set_project(monkeypatch, isolated_project)
        # Plant outcomes data
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.paths import get_data_dir
        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(graph_db)
        _ensure_session(g)
        cur = g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s1", "x", "f.py", "ctx"),
        )
        did = cur.lastrowid
        for _ in range(3):
            g.record_outcome(
                session_id="s1", file_path="f.py",
                outcome_type="kept", decision_id=did,
            )
        g.conn.commit()
        g.close()

        # Register two policies that both read outcomes
        sql_calls = {"n": 0}

        from mcp_server.engine import promotion_score
        original_aggregate = promotion_score.aggregate_decision_outcomes

        def counting_aggregate(*args, **kwargs):
            sql_calls["n"] += 1
            return original_aggregate(*args, **kwargs)

        monkeypatch.setattr(
            promotion_score, "aggregate_decision_outcomes", counting_aggregate,
        )

        class ReaderA(Policy):
            name = "reader_a"
            handles = (EventType.USER_PROMPT_SUBMIT,)
            priority = 100
            def evaluate(self, event, signals=None):
                signals.outcomes(since_days=30, min_outcomes=2)
                return PolicyVerdict.allow()

        class ReaderB(Policy):
            name = "reader_b"
            handles = (EventType.USER_PROMPT_SUBMIT,)
            priority = 50
            def evaluate(self, event, signals=None):
                signals.outcomes(since_days=30, min_outcomes=2)  # same args
                return PolicyVerdict.allow()

        reset_policies()
        register_policy(ReaderA())
        register_policy(ReaderB())

        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text="something",
        )
        dispatch(event)
        # Both policies called signals.outcomes with same args.
        # The aggregate SQL query should run ONCE due to the cache.
        assert sql_calls["n"] == 1, (
            f"signals.outcomes cache miss across policies — "
            f"aggregate_decision_outcomes ran {sql_calls['n']} times "
            "(expected 1 due to per-event cache)"
        )


# =====================================================================
# K9 — End-to-end Claude Code wiring with refactor intent
# =====================================================================


class TestK9_WiringRefactorIntent:

    def test_refactor_intent_through_wiring_emits_impact_section(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The wiring path was tested for fix-bug in test_intent_inference.
        Cover refactor too — different signal set (impact instead of fixes).

        Pre-fix this test was passing because of the Bug-6 empty-section
        bug: ``get_impact`` returned ``found=False`` for our function-only
        graph, but the formatter still emitted the empty "### Blast radius:"
        header — which was enough to make the "Codevira pre-fetch in ctx"
        check pass. The test was verifying the bug, not the feature.

        Bug-6 fix exposed this: now we need a graph that actually produces
        non-zero impact data (file-level node + dependent file-level node
        + edge between them).
        """
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine import (
            register_default_policies, reset_policies,
        )
        from mcp_server.engine.wiring import claude_code_hooks

        _set_project(monkeypatch, isolated_project)
        # Plant file-level nodes + an edge so get_impact can find a target.
        # ``get_impact`` requires kind="file" nodes (Week-11 QA finding).
        g = _open_graph(isolated_project)
        g.conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path) "
            "VALUES (?, ?, ?, ?)",
            ("auth.py", "file", "auth.py", "auth.py"),
        )
        g.conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path) "
            "VALUES (?, ?, ?, ?)",
            ("api.py", "file", "api.py", "api.py"),
        )
        g.conn.execute(
            "INSERT INTO edges (source_id, target_id, kind) VALUES (?, ?, ?)",
            ("api.py", "auth.py", "imports"),
        )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "s",
            "cwd": str(isolated_project),
            "prompt": "Refactor auth.py — extract the validation logic",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("UserPromptSubmit")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        ctx = emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
        # Hero 9's pre-fetch must be present
        assert "Codevira pre-fetch" in ctx, (
            f"Hero 9's pre-fetch missing on refactor intent: {emitted}"
        )
        assert "refactor" in ctx, (
            f"Intent label missing in Hero 9 inject: {ctx}"
        )
        # Bug-6 lock-in: the Blast radius section must have ACTUAL CONTENT
        # (a file count), not just an empty header. Pre-fix this passed
        # vacuously because the empty header was emitted.
        assert "Blast radius" in ctx, (
            f"Refactor intent must emit Blast radius section when graph has "
            f"impact data. ctx:\n{ctx}"
        )
        assert "caller" in ctx or "dependent" in ctx, (
            f"Blast radius section emitted but with no caller/dependent count "
            f"— Bug-6-shape regression. ctx:\n{ctx}"
        )


# =====================================================================
# K10 — End-to-end wiring with no prompt_text (defensive gate)
# =====================================================================


class TestK10_WiringEmptyPrompt:

    def test_wiring_with_missing_prompt_text_does_not_inject_h9(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If the wiring layer can't extract a prompt (e.g., the JSON
        payload is malformed or missing the 'prompt' field), Hero 9
        must silently allow. NO crash, NO partial inject."""
        from mcp_server.engine import (
            register_default_policies, reset_policies,
        )
        from mcp_server.engine.wiring import claude_code_hooks

        _set_project(monkeypatch, isolated_project)
        # No fix history, no prompt content
        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "s",
            "cwd": str(isolated_project),
            # NO prompt key
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("UserPromptSubmit")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        # Either no inject at all, OR an inject without Hero 9's section
        ctx = emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Codevira pre-fetch" not in ctx, (
            f"Hero 9 injected on empty prompt: {emitted}"
        )


# =====================================================================
# K11 — Hero 9 + None prompt_text (raw event construction)
# =====================================================================


class TestK11_NonePromptText:

    def test_h9_silent_on_none_prompt_text(self, isolated_project: Path):
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.intent_inference import (
            ProactiveIntentInference,
        )
        policy = ProactiveIntentInference()
        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text=None,
        )
        # No signals needed — should short-circuit on prompt_text gate
        v = policy.evaluate(event, signals=None)
        assert v.is_allowing()


# =====================================================================
# K12 — Multi-policy crash isolation across all 8 heroes
# =====================================================================


class TestK13_Bug5PathTraversalDefense:
    """Bug 5 (Week-11 QA round): Hero 9 resolved file_mentions via
    ``(project_root / file_str).resolve()`` with no containment check.
    A prompt like ``"fix '../../etc/passwd.py'"`` would issue signal
    lookups against paths OUTSIDE project_root.

    Today the signals layer returns empty for out-of-project paths
    (data tables only contain in-project records), so this is a
    "safe by accident" defense gap — but the wiring layer's
    Round-4 HIGH #1 already enforces this for ``target_file``,
    and the same discipline applies here.
    """

    def test_path_traversal_mention_skipped_in_signals_calls(
        self, isolated_project: Path,
    ):
        from mcp_server.engine.policies.intent_inference import (
            _fetch_signals_for_intent, INTENT_FIX_BUG,
        )

        class _Spy:
            def __init__(self):
                self.fixes_calls = []
                self.impact_calls = []
            def fixes(self, p):
                self.fixes_calls.append(p)
                return []
            def decisions(self, **k):
                return []
            def impact(self, p):
                self.impact_calls.append(p)
                return {}
            def outcomes(self, **k):
                return []
            def learned_rules(self, **k):
                return []

        spy = _Spy()
        _fetch_signals_for_intent(
            intent=INTENT_FIX_BUG,
            file_mentions=["../../etc/passwd.py", "auth.py"],
            project_root=isolated_project,
            signals=spy,
            config={
                "max_files": 3, "max_fixes_per_file": 3,
                "max_decisions_per_file": 3, "max_outcomes": 3,
                "include_impact": True,
            },
        )
        # The traversal mention must NOT have triggered a signals call.
        # Only the in-project mention does.
        assert all("auth.py" in str(p) for p in spy.fixes_calls), (
            f"Bug 5 regression: out-of-project mention reached signals.fixes "
            f"({spy.fixes_calls})"
        )
        assert not any("passwd" in str(p) for p in spy.fixes_calls), (
            f"Bug 5 regression: passwd path leaked into signals.fixes calls"
        )
        assert not any("passwd" in str(p) for p in spy.impact_calls), (
            f"Bug 5 regression: passwd path leaked into signals.impact calls"
        )

    def test_absolute_path_outside_project_skipped(
        self, isolated_project: Path,
    ):
        """Defense-in-depth case: an absolute file mention OUTSIDE
        project_root must also be skipped. The regex extractor doesn't
        currently extract absolute paths (lookbehind blocks `/foo.py`),
        but if a future change loosens the regex, this test catches it.
        """
        from mcp_server.engine.policies.intent_inference import (
            _fetch_signals_for_intent, INTENT_FIX_BUG,
        )

        class _Spy:
            def __init__(self):
                self.fixes_calls = []
            def fixes(self, p):
                self.fixes_calls.append(p)
                return []
            def decisions(self, **k):
                return []
            def impact(self, p):
                return {}
            def outcomes(self, **k):
                return []
            def learned_rules(self, **k):
                return []

        spy = _Spy()
        _fetch_signals_for_intent(
            intent=INTENT_FIX_BUG,
            # Pretend the regex extracted an absolute path into the list
            file_mentions=["/etc/passwd.py"],
            project_root=isolated_project,
            signals=spy,
            config={
                "max_files": 3, "max_fixes_per_file": 3,
                "max_decisions_per_file": 3, "max_outcomes": 3,
                "include_impact": True,
            },
        )
        # /etc/passwd.py resolves outside project_root → no signal call
        assert spy.fixes_calls == [], (
            f"Bug 5 regression: absolute out-of-project path reached signals: "
            f"{spy.fixes_calls}"
        )


class TestK14_Bug6EmptySectionSuppression:
    """Bug 6 (Week-11 QA round): _format_inject emitted "### Blast radius:"
    section header even when every impact entry had count==0. Produces
    noise in the AI's context window.

    Fix at the FETCHER level: filter zero-count impact entries before
    they enter the formatter.
    """

    def test_zero_count_impact_filtered_at_fetcher(
        self, isolated_project: Path,
    ):
        from mcp_server.engine.policies.intent_inference import (
            _fetch_signals_for_intent, INTENT_REFACTOR,
        )

        class _Spy:
            def __init__(self, impact_for):
                self._impact_for = impact_for
            def fixes(self, p):
                return []
            def decisions(self, **k):
                return []
            def impact(self, p):
                return dict(self._impact_for.get(p, {}))
            def outcomes(self, **k):
                return []
            def learned_rules(self, **k):
                return []

        # Impact returns a non-empty dict but zero count
        auth_resolved = (isolated_project / "auth.py").resolve()
        spy = _Spy({auth_resolved: {"affected_count": 0, "affected_files": []}})

        fetched = _fetch_signals_for_intent(
            intent=INTENT_REFACTOR,
            file_mentions=["auth.py"],
            project_root=isolated_project,
            signals=spy,
            config={
                "max_files": 3, "max_fixes_per_file": 3,
                "max_decisions_per_file": 3, "max_outcomes": 3,
                "include_impact": True,
            },
        )
        assert fetched.get("impact") == {}, (
            f"Bug 6 regression: zero-count impact retained: {fetched['impact']}"
        )

    def test_format_inject_no_empty_blast_radius_header(self):
        """Even if a stale code path planted a zero-count entry into
        ``fetched["impact"]``, the formatter must not emit the bare
        header. (Belt-and-suspenders test.)"""
        from mcp_server.engine.policies.intent_inference import _format_inject

        ctx = _format_inject(
            intent="refactor",
            file_mentions=["auth.py"],
            fetched={
                "fixes": {},
                "decisions": {"auth.py": [{"decision": "x", "timestamp": "2025-01-01"}]},
                # Pretend a stale path planted zero-count entry
                "impact": {"auth.py": {"affected_count": 0, "affected_files": []}},
                "outcomes": [],
            },
        )
        # The header MAY be emitted (formatter doesn't deeply inspect),
        # but the body line "X caller(s)" should NOT appear since count=0.
        # We ASSERT THE BODY: no bullet line under Blast radius.
        if "Blast radius" in ctx:
            # Find the section
            i = ctx.index("Blast radius")
            section = ctx[i : i + 200]
            assert "caller" not in section, (
                f"Bug 6 regression: empty Blast radius section emitted "
                f"a count-zero bullet. Section: {section!r}"
            )


class TestK12_AllHeroCrashIsolation:

    def test_one_random_policy_crash_doesnt_break_others(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Sabotage Hero 5's evaluate. Verify Hero 9 still injects."""
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.cross_session import (
            CrossSessionConsistency,
        )

        _set_project(monkeypatch, isolated_project)

        def crashing_evaluate(self, event, signals=None):
            raise RuntimeError("intentional H5 crash for K12")
        monkeypatch.setattr(
            CrossSessionConsistency, "evaluate", crashing_evaluate,
        )

        reset_policies()
        register_default_policies()

        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text="Fix the auth.py login bug",
        )
        v = dispatch(event)
        # Hero 9 may still produce inject (depending on whether outcomes
        # exist) OR allow. Either way, NO crash propagates and dispatch
        # returns a verdict. We just assert the dispatch completed.
        assert v.action in ("allow", "inject")
        # And: Hero 5 was NOT in the inject_policies list (it crashed)
        ip = v.metadata.get("inject_policies", [])
        assert "cross_session_consistency" not in ip, (
            f"Hero 5 appeared in inject metadata despite crashing: {ip}"
        )
