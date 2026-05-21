"""
test_intent_inference.py — Hero 9 acceptance + behavioral + mutation tests.

Tier-0 pre-flight from start (post-Bug-4 muscle memory):
  - Pure-function unit tests for the classifier + file extractor
  - Real graph DB integration via record_fix + insert decisions
  - Behavioral spies on signals.fixes / signals.decisions / signals.impact
    to verify ordering and gating
  - End-to-end dispatch with all 8 heroes registered
  - End-to-end through claude_code_hooks.handle("UserPromptSubmit") with
    realistic JSON payload (Bug-4 lesson: every wiring path must be
    exercised)
  - 10+ mutations from start
  - Bug-shape audit
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.intent_classifier import (
    classify_intent,
    extract_file_mentions,
    INTENT_FIX_BUG,
    INTENT_ADD_FEATURE,
    INTENT_REFACTOR,
    INTENT_EXPLAIN,
    INTENT_TEST,
    INTENT_DOCS,
    INTENT_OTHER,
)
from mcp_server.engine.policies.intent_inference import (
    ProactiveIntentInference,
    _format_inject,
    _truncate,
    _NO_INJECT_INTENTS,
)


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_prompt_event(
    *,
    prompt: str,
    project_root: Path | None = None,
    session_id: str = "s1",
) -> HookEvent:
    return HookEvent(
        event_type=EventType.USER_PROMPT_SUBMIT,
        project_root=project_root or Path("/p"),
        ai_tool="claude-code",
        session_id=session_id,
        prompt_text=prompt,
    )


class _FakeSignals:
    """Honors-args fake. Records every call so tests prove gating order."""

    def __init__(
        self,
        *,
        fixes_for: dict[Path, list[dict[str, Any]]] | None = None,
        decisions_for: dict[str, list[dict[str, Any]]] | None = None,
        impact_for: dict[Path, dict[str, Any]] | None = None,
        outcomes_data: list[dict[str, Any]] | None = None,
    ):
        self._fixes_for = fixes_for or {}
        self._decisions_for = decisions_for or {}
        self._impact_for = impact_for or {}
        self._outcomes = outcomes_data or []
        self.fixes_calls: list[Path] = []
        self.decisions_calls: list[dict[str, Any]] = []
        self.impact_calls: list[Path] = []
        self.outcomes_calls: list[dict[str, Any]] = []

    def fixes(self, file_path: Path) -> list[dict[str, Any]]:
        self.fixes_calls.append(file_path)
        return list(self._fixes_for.get(file_path, []))

    def decisions(
        self,
        *,
        file: str | None = None,
        locked_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.decisions_calls.append({"file": file, "limit": limit})
        return list(self._decisions_for.get(file or "", []))[:limit]

    def impact(self, file_path: Path) -> dict[str, Any]:
        self.impact_calls.append(file_path)
        return dict(self._impact_for.get(file_path, {}))

    def outcomes(self, **k) -> list[dict[str, Any]]:
        self.outcomes_calls.append(dict(k))
        return list(self._outcomes)

    def learned_rules(self, **k) -> list[dict[str, Any]]:
        return []


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
        "CODEVIRA_INTENT_INFERENCE_MODE",
        "CODEVIRA_INTENT_INFERENCE_MAX_FILES",
        "CODEVIRA_INTENT_INFERENCE_MIN_PROMPT_CHARS",
        "CODEVIRA_INTENT_INFERENCE_MAX_FIXES_PER_FILE",
        "CODEVIRA_INTENT_INFERENCE_MAX_DECISIONS_PER_FILE",
        "CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT",
    ):
        monkeypatch.delenv(env, raising=False)


# =====================================================================
# Pure classifier tests
# =====================================================================


class TestIntentClassifier:
    def test_fix_bug_patterns(self):
        for prompt in [
            "Fix the auth flow",
            "this is broken",
            "fix the broken test",  # specificity: fix beats test
            "the login is failing",
            "We have a regression",
            "this doesn't work",
            "got an error",
        ]:
            assert (
                classify_intent(prompt) == INTENT_FIX_BUG
            ), f"Expected fix-bug for {prompt!r}"

    def test_add_feature_patterns(self):
        for prompt in [
            "Add a new feature for user profiles",
            "Implement caching",
            "Create a new endpoint",
            "Build the user dashboard",
        ]:
            assert classify_intent(prompt) == INTENT_ADD_FEATURE

    def test_refactor_patterns(self):
        for prompt in [
            "Refactor auth.py",
            "Clean up the database module",
            "Simplify this function",
            "Rename foo to bar",
        ]:
            assert classify_intent(prompt) == INTENT_REFACTOR

    def test_explain_patterns(self):
        for prompt in [
            "Explain how the auth flow works",
            "What does ratelimit.py do?",
            "How does this handle retries?",
            "Describe the migration script",
            "Summarize this module",
        ]:
            assert classify_intent(prompt) == INTENT_EXPLAIN

    def test_test_patterns(self):
        for prompt in [
            "Write tests for the auth module",
            "Add unit tests for retries.py",
            "Create integration tests",
            "We need test coverage on auth.py",
        ]:
            assert classify_intent(prompt) == INTENT_TEST

    def test_docs_patterns(self):
        for prompt in [
            "Add docstrings to api.py",
            "Document the migration script",
            "Write comments for this function",
        ]:
            assert classify_intent(prompt) == INTENT_DOCS

    def test_other_fallback(self):
        for prompt in ["Hi", "Thanks", "", "Random gibberish about cats"]:
            assert classify_intent(prompt) == INTENT_OTHER

    def test_specificity_fix_beats_test(self):
        """The most-confusing-case test: 'fix the broken test'.
        Should be fix-bug (the user wants to debug), not test
        (the user does NOT want to write a new test)."""
        assert classify_intent("fix the broken test") == INTENT_FIX_BUG

    def test_specificity_fix_beats_docs(self):
        assert classify_intent("fix the docstring") == INTENT_FIX_BUG


class TestFileMentionExtractor:
    def test_simple_paths(self):
        assert extract_file_mentions("Fix auth.py") == ["auth.py"]
        assert extract_file_mentions("Touch src/auth.py") == ["src/auth.py"]
        assert extract_file_mentions("Look at auth.py and users.py") == [
            "auth.py",
            "users.py",
        ]

    def test_paths_in_quotes_or_parens(self):
        assert extract_file_mentions("the file `auth.py` is broken") == ["auth.py"]
        assert extract_file_mentions("see (foo.go) for details") == ["foo.go"]
        assert extract_file_mentions('check "tests/test_auth.py"') == [
            "tests/test_auth.py"
        ]

    def test_extension_allowlist_blocks_non_files(self):
        # Not in the allowlist (extension is short enough for the regex
        # but rejected by the allowlist — this is the actual lock-in:
        # a mutation that drops the allowlist must FAIL this test).
        assert (
            extract_file_mentions("Look at data.csv") == []
        ), "data.csv: 'csv' extension not in allowlist — must be filtered"
        assert (
            extract_file_mentions("Open archive.zip") == []
        ), "archive.zip: 'zip' extension not in allowlist"
        assert (
            extract_file_mentions("Run a.exe") == []
        ), "a.exe: 'exe' extension not in allowlist"
        # Regex itself rejects: extension too long
        assert extract_file_mentions("see foo.unknown_ext") == []
        # Regex itself rejects: leading @ blocks lookbehind
        assert extract_file_mentions("email@example.com") == []
        # Regex itself rejects: version digit isn't [A-Za-z]
        assert extract_file_mentions("v5.0.1") == []

    def test_extension_allowlist_accepts_known_code_files(self):
        """Positive control for the allowlist: known extensions ARE matched."""
        for case in [
            ("Open data.json", ["data.json"]),
            ("See config.yaml", ["config.yaml"]),
            ("Touch README.md", ["README.md"]),
            ("Run script.sh", ["script.sh"]),
            ("Update foo.html", ["foo.html"]),
        ]:
            prompt, expected = case
            assert (
                extract_file_mentions(prompt) == expected
            ), f"Allowlist false-negative on {prompt!r}"

    def test_max_files_cap(self):
        prompt = "auth.py users.py db.py login.py session.py"
        out = extract_file_mentions(prompt, max_files=3)
        assert len(out) == 3
        assert out == ["auth.py", "users.py", "db.py"]

    def test_dedup(self):
        prompt = "fix auth.py — auth.py has a bug"
        assert extract_file_mentions(prompt) == ["auth.py"]

    def test_max_files_clamped_to_floor_and_ceiling(self):
        # 0 → clamped to floor=1
        assert len(extract_file_mentions("a.py b.py", max_files=0)) == 1
        # 100 → clamped to ceiling=10 (we only test that <= 10 returned;
        # extracting 100 distinct files would require a long prompt).
        prompt = " ".join(f"f{i}.py" for i in range(15))
        assert len(extract_file_mentions(prompt, max_files=100)) == 10


# =====================================================================
# Acceptance tests (12 scenarios from spec)
# =====================================================================


class TestAcceptance:
    def test_1_non_prompt_event_allowed(self):
        policy = ProactiveIntentInference()
        spy = _FakeSignals()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",
        )
        v = policy.evaluate(event, signals=spy)
        assert v.is_allowing()
        assert spy.fixes_calls == [] and spy.decisions_calls == []

    def test_2_short_prompt_allowed_silent(self):
        policy = ProactiveIntentInference()
        spy = _FakeSignals()
        v = policy.evaluate(
            _make_prompt_event(prompt="hi"),
            signals=spy,
        )
        assert v.is_allowing()
        assert spy.fixes_calls == []

    def test_3_fix_bug_with_file_injects_fixes_and_decisions(
        self,
        tmp_path: Path,
    ):
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        # Plant fixes for the resolved abs path
        spy = _FakeSignals(
            fixes_for={
                (proj / "auth.py").resolve(): [
                    {
                        "description": "regex didn't escape '+' in email",
                        "commit_date": "2025-04-13",
                    }
                ],
            },
            decisions_for={
                "auth.py": [
                    {
                        "decision": "use bcrypt over argon2",
                        "timestamp": "2025-03-01",
                    }
                ],
            },
        )
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Fix the auth.py login bug — special chars don't work",
                project_root=proj,
            ),
            signals=spy,
        )
        assert v.action == "inject"
        ctx = v.inject_context or ""
        assert "Recent fixes touching this area" in ctx
        assert "regex didn't escape" in ctx
        assert "bcrypt over argon2" in ctx
        assert v.metadata.get("intent") == INTENT_FIX_BUG

    def test_4_add_feature_injects_outcomes(self, tmp_path: Path):
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = _FakeSignals(
            outcomes_data=[
                {
                    "id": 1,
                    "decision": "Tailwind not Bootstrap",
                    "file_path": "style.css",
                    "score": 0.9,
                },
            ],
        )
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Implement a new dashboard endpoint for users",
                project_root=proj,
            ),
            signals=spy,
        )
        assert v.action == "inject"
        ctx = v.inject_context or ""
        assert "Top stable decisions" in ctx
        assert "Tailwind not Bootstrap" in ctx

    def test_5_refactor_injects_impact(self, tmp_path: Path):
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = _FakeSignals(
            impact_for={
                (proj / "auth.py").resolve(): {
                    "affected_count": 7,
                    "affected_files": ["api.py", "login.py"],
                },
            },
        )
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Refactor auth.py — extract the validation logic",
                project_root=proj,
            ),
            signals=spy,
        )
        assert v.action == "inject"
        ctx = v.inject_context or ""
        assert "Blast radius" in ctx
        assert "7 caller" in ctx

    def test_6_test_intent_silent_allow(self, tmp_path: Path):
        policy = ProactiveIntentInference()
        spy = _FakeSignals(
            fixes_for={Path("/p/auth.py").resolve(): [{"description": "fix"}]},
        )
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Write unit tests for auth.py",
                project_root=tmp_path / "p",
            ),
            signals=spy,
        )
        assert v.is_allowing(), "test intent should NOT inject — Hero 5 + others handle"
        # And: signals were NOT called (gate before fetch)
        assert spy.fixes_calls == []

    def test_7_docs_intent_silent_allow(self):
        policy = ProactiveIntentInference()
        spy = _FakeSignals()
        v = policy.evaluate(
            _make_prompt_event(prompt="Add docstrings to api.py"),
            signals=spy,
        )
        assert v.is_allowing()
        assert spy.fixes_calls == [] and spy.decisions_calls == []

    def test_8_other_intent_no_files_silent_allow(self):
        policy = ProactiveIntentInference()
        spy = _FakeSignals()
        v = policy.evaluate(
            _make_prompt_event(prompt="What is the weather like today"),
            signals=spy,
        )
        assert v.is_allowing()

    def test_9_off_mode_skips_signals(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MODE", "off")
        policy = ProactiveIntentInference()
        spy = _FakeSignals(
            fixes_for={
                (tmp_path / "p" / "auth.py").resolve(): [
                    {"description": "fix"},
                ]
            },
        )
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Fix the auth.py bug",
                project_root=tmp_path / "p",
            ),
            signals=spy,
        )
        assert v.is_allowing()
        assert spy.fixes_calls == [], "mode=off must short-circuit BEFORE signal fetch"

    def test_10_max_files_cap_honored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MAX_FILES", "2")
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = _FakeSignals()
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Fix auth.py users.py db.py session.py",
                project_root=proj,
            ),
            signals=spy,
        )
        # Only 2 files extracted → at most 2 fixes_calls + 2 decisions_calls
        # (Each file may not actually trigger every signal; we're capping
        # the WORK, not the inject content.)
        assert len(spy.fixes_calls) <= 2
        # The metadata records the file mentions
        assert (
            len(v.metadata.get("file_mentions", [])) <= 2
            if v.action == "inject"
            else True
        )


# =====================================================================
# Behavioral gates — spies prove gating order
# =====================================================================


class TestBehavioralGates:
    def test_signals_none_short_circuits(self):
        policy = ProactiveIntentInference()
        v = policy.evaluate(
            _make_prompt_event(prompt="Fix the auth bug"),
            signals=None,
        )
        assert v.is_allowing()

    def test_event_type_gate(self):
        policy = ProactiveIntentInference()
        spy = _FakeSignals()
        for evt in (
            EventType.PRE_TOOL_USE,
            EventType.POST_TOOL_USE,
            EventType.SESSION_START,
            EventType.STOP,
        ):
            spy.fixes_calls.clear()
            event = HookEvent(event_type=evt, project_root=Path("/p"))
            policy.evaluate(event, signals=spy)
            assert spy.fixes_calls == [], f"Hero 9 fired on {evt} — handles drift!"

    def test_priority_value_stable(self):
        assert ProactiveIntentInference().priority == 20

    def test_handles_only_user_prompt_submit(self):
        assert ProactiveIntentInference.handles == (EventType.USER_PROMPT_SUBMIT,)

    def test_enabled_by_default_true(self):
        assert ProactiveIntentInference.enabled_by_default is True

    def test_invalid_mode_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MODE", "block")
        cfg = ProactiveIntentInference()._config()
        assert cfg["mode"] == "inject"

    def test_max_files_clamped(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MAX_FILES", "0")
        assert ProactiveIntentInference()._config()["max_files"] == 1
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MAX_FILES", "999")
        assert ProactiveIntentInference()._config()["max_files"] == 10
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_MAX_FILES", "garbage")
        assert ProactiveIntentInference()._config()["max_files"] == 3

    def test_include_impact_env_disables(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        monkeypatch.setenv("CODEVIRA_INTENT_INFERENCE_INCLUDE_IMPACT", "0")
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = _FakeSignals(
            impact_for={(proj / "auth.py").resolve(): {"affected_count": 5}},
        )
        # Discarded — this test verifies side-effect-free behavior (the
        # spy.impact_calls assertion below), not the verdict shape.
        policy.evaluate(
            _make_prompt_event(
                prompt="Refactor auth.py",
                project_root=proj,
            ),
            signals=spy,
        )
        # Impact NOT called because env disables
        assert spy.impact_calls == []

    def test_no_inject_intents_set_correct(self):
        """Lock the test/docs gate. Drift here = Hero 9 fires on tests."""
        assert _NO_INJECT_INTENTS == {INTENT_TEST, INTENT_DOCS}


# =====================================================================
# Edge cases (Bug-shape defenses)
# =====================================================================


class TestEdgeCases:
    def test_signals_fixes_raises_does_not_break_policy(self, tmp_path: Path):
        class CrashingFixesSignals(_FakeSignals):
            def fixes(self, file_path):
                raise RuntimeError("fixes DB corrupt")

        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = CrashingFixesSignals(
            decisions_for={"auth.py": [{"decision": "x", "timestamp": "2025-01-01"}]},
        )
        v = policy.evaluate(
            _make_prompt_event(prompt="Fix auth.py", project_root=proj),
            signals=spy,
        )
        # Policy still produces inject — fixes section is empty,
        # decisions section is present.
        assert v.action == "inject"

    def test_signals_impact_raises_does_not_break_policy(self, tmp_path: Path):
        class CrashingImpactSignals(_FakeSignals):
            def impact(self, file_path):
                raise RuntimeError("graph crashed")

        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = CrashingImpactSignals(
            decisions_for={"auth.py": [{"decision": "x", "timestamp": "2025-01-01"}]},
        )
        v = policy.evaluate(
            _make_prompt_event(prompt="Refactor auth.py", project_root=proj),
            signals=spy,
        )
        # Inject still produced (decisions came through)
        assert v.action == "inject"

    def test_all_signals_empty_returns_allow_silent(self, tmp_path: Path):
        policy = ProactiveIntentInference()
        proj = tmp_path / "p"
        proj.mkdir()
        spy = _FakeSignals()  # everything empty
        v = policy.evaluate(
            _make_prompt_event(
                prompt="Fix auth.py and users.py somehow",
                project_root=proj,
            ),
            signals=spy,
        )
        assert v.is_allowing(), "If all signals return empty, no inject — silent allow"

    def test_format_inject_returns_empty_when_nothing_to_show(self):
        out = _format_inject(
            intent=INTENT_FIX_BUG,
            file_mentions=[],
            fetched={"fixes": {}, "decisions": {}, "impact": {}, "outcomes": []},
        )
        assert out == "", "Empty fetched data must yield empty inject string"

    def test_truncate_strips_newlines(self):
        out = _truncate("line1\nline2\nline3")
        assert "\n" not in out


# =====================================================================
# Real-DB integration — Tier-0 pre-flight (Bug-1 + Bug-4 defense)
# =====================================================================


class TestRealDBIntegration:
    @pytest.mark.xfail(
        reason=(
            "v2.2.0 Phase C: this test writes decisions via the v2.1.x "
            "graph.db path. RelevanceInject (v2.2.0) reads from "
            ".codevira/ instead. Phase E updates the fixture or "
            "deletes this test."
        ),
        strict=False,
    )
    def test_dispatch_with_real_signals_fix_bug_intent(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real DB end-to-end: register all 8 heroes, plant fix +
        decision, fire UserPromptSubmit, assert combined inject contains
        Hero 9's intent-specific section."""
        from indexer.fix_history import record_fix
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

        # Plant a real fix
        (isolated_project / "auth.py").write_text("def login(): pass")
        record_fix(
            isolated_project,
            file_path="auth.py",
            line_start=0,
            line_end=0,
            description="fix: regex didn't escape special chars in email",
            source="manual",
        )
        # Plant a real decision
        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s1", "x"),
        )
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
            session_id="new-session",
            prompt_text="Fix the auth.py login bug — special chars don't work",
        )
        verdict = dispatch(event)
        assert (
            verdict.action == "inject"
        ), f"Expected inject, got {verdict.action}: {verdict.message}"
        ctx = verdict.inject_context or ""
        # Hero 9's section MUST be present
        assert (
            "Codevira pre-fetch" in ctx
        ), f"Hero 9's pre-fetch section missing from combined inject:\n{ctx}"
        assert "fix-bug" in ctx
        assert "regex didn't escape" in ctx
        # Hero 5's section ALSO present (different angle)
        assert (
            "Prior decisions you may want to consider" in ctx
        ), f"Hero 5's section missing — combiner broken? ctx: {ctx}"
        reset_policies()

    def test_hero_9_fires_through_claude_code_wiring(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Bug-4 lesson: end-to-end through the actual Claude Code hook
        handler with realistic JSON. UserPromptSubmit was already a
        proven path (Hero 5 ships through it), but we verify Hero 9
        works through it too — and specifically that the `additionalContext`
        emission carries our intent-specific content."""
        from indexer.fix_history import record_fix
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
        )
        from mcp_server.engine.wiring import claude_code_hooks
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        (isolated_project / "auth.py").write_text("def login(): pass")
        record_fix(
            isolated_project,
            file_path="auth.py",
            line_start=0,
            line_end=0,
            description="fix: regex didn't escape special chars in email",
            source="manual",
        )

        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "s",
            "cwd": str(isolated_project),
            "prompt": "Fix the auth.py login bug",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("UserPromptSubmit")
        assert rc == 0
        emitted = json.loads(stdout_buf.getvalue())
        hso = emitted.get("hookSpecificOutput", {})
        ctx = hso.get("additionalContext", "")
        assert hso.get("hookEventName") == "UserPromptSubmit"
        assert "Codevira pre-fetch" in ctx, (
            f"Hero 9 didn't surface intent-specific context through wiring. "
            f"Emitted: {emitted}"
        )
        assert "regex didn't escape" in ctx
        reset_policies()


# =====================================================================
# Registration
# =====================================================================


class TestRegistration:
    def test_register_default_policies_includes_hero_9(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "intent_inference" in names

    def test_idempotent_with_eight_heroes(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        for n in (
            "ai_promotion_score",
            "anti_regression",
            "blast_radius_veto",
            "relevance_inject",
            "decision_lock",
            "intent_inference",
            "live_style_enforcement",
            "token_budget_persist",
        ):
            assert names.count(n) == 1


# =====================================================================
# Performance
# =====================================================================


class TestPerformance:
    def test_classify_intent_under_1ms(self):
        import time

        prompts = [
            "Fix the auth flow login is broken with special chars",
            "Add a new feature for user profiles in the dashboard",
            "Refactor the database module to be cleaner",
            "Explain how the rate limiter works",
            "Hi, how are you today",
        ]
        durations = []
        for p in prompts * 200:
            t0 = time.perf_counter()
            classify_intent(p)
            durations.append((time.perf_counter() - t0) * 1000)
        durations.sort()
        p95 = durations[int(len(durations) * 0.95)]
        assert p95 < 1.0, f"classify_intent p95={p95:.3f}ms exceeds 1ms"
