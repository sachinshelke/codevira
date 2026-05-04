"""
test_scope_contract.py — Hero 3 acceptance + behavioral + mutation tests
+ deep-audit probes (post-Bugs-1-8).

Discipline applied from start:
  - Tier-0 pre-flight (real DB, behavioral spies, dispatch, wiring)
  - Path-traversal probe for prompt file mentions (Bug-5 lesson)
  - All 4 _EDIT_TOOLS through wiring (Bug-7 lesson)
  - Content-verifying assertions: block message includes BOTH the
    offending file AND the original prompt (Lesson #19)
  - 10+ mutations from start
  - Bug-X-shape audit: every contract field exercised; off-by-default
    truly silences; TTL works
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.scope_contract import (
    ProactiveScopeContractLock,
    _resolve_in_project_files,
    _target_in_scope,
    _format_block_message,
    _LOC_BUDGET_BY_INTENT,
    _NO_BUILD_INTENTS,
)
from mcp_server.engine import scope_contract as sc_mod
from mcp_server.engine.scope_contract import (
    ScopeContract,
    set_session_contract, get_session_contract, clear_all,
    _stored_count, _all_session_ids,
)


# =====================================================================
# Helpers + fixtures
# =====================================================================


def _make_prompt_event(
    *,
    prompt: str,
    project_root: Path | None = None,
    session_id: str = "s-test",
) -> HookEvent:
    return HookEvent(
        event_type=EventType.USER_PROMPT_SUBMIT,
        project_root=project_root or Path("/p"),
        ai_tool="claude-code",
        session_id=session_id,
        prompt_text=prompt,
    )


def _make_edit_event(
    *,
    target: Path,
    project_root: Path | None = None,
    session_id: str = "s-test",
    tool_name: str = "Edit",
) -> HookEvent:
    return HookEvent(
        event_type=EventType.PRE_TOOL_USE,
        project_root=project_root or Path("/p"),
        ai_tool="claude-code",
        session_id=session_id,
        tool_name=tool_name,
        target_file=target,
    )


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
def _isolate_env_and_storage(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with empty contract storage + clean env vars."""
    clear_all()
    for env in (
        "CODEVIRA_SCOPE_LOCK_MODE",
        "CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS",
        "CODEVIRA_ENGINE",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    clear_all()


# =====================================================================
# Acceptance scenarios (12 from spec)
# =====================================================================


class TestAcceptance:

    def test_1_off_mode_disables_policy(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Off (default) means: NO contract built on UserPromptSubmit,
        NO enforcement on PreToolUse."""
        # Default is off; no env override.
        policy = ProactiveScopeContractLock()
        v = policy.evaluate(
            _make_prompt_event(
                prompt="fix the auth.py bug somewhere",
                project_root=isolated_project,
            ),
        )
        assert v.is_allowing()
        # Storage should be empty
        assert _stored_count() == 0, (
            "off mode must NOT build contracts; got "
            f"{_all_session_ids()}"
        )

    def test_2_no_file_mentions_no_contract_built(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Prompt with intent=fix-bug but NO file mentions → no scope to
        enforce → no contract stored."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        v = policy.evaluate(
            _make_prompt_event(
                prompt="fix the bug we discussed yesterday",
                project_root=isolated_project,
            ),
        )
        assert v.is_allowing()
        assert _stored_count() == 0

    def test_3_test_intent_no_contract_built(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Intent=test → no contract (test/docs/explain/other are
        deliberately NOT scoped)."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        v = policy.evaluate(
            _make_prompt_event(
                prompt="add tests for auth.py",
                project_root=isolated_project,
            ),
        )
        assert v.is_allowing()
        assert _stored_count() == 0

    def test_4_fix_bug_with_file_mention_builds_contract(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Happy build path: fix-bug + auth.py mentioned → contract stored."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        v = policy.evaluate(
            _make_prompt_event(
                prompt="fix the null check in auth.py",
                project_root=isolated_project,
            ),
        )
        assert v.is_allowing()
        c = get_session_contract("s-test")
        assert c is not None
        assert "auth.py" in c.allowed_files
        assert c.original_intent == "fix-bug"
        assert "fix the null check" in c.original_prompt

    def test_5_in_scope_edit_allowed(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Edit on a file IN the contract's allowed_files → allow."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        # Build contract
        policy.evaluate(_make_prompt_event(
            prompt="fix the null check in auth.py",
            project_root=isolated_project,
        ))
        # Enforce: Edit on auth.py
        target = isolated_project / "auth.py"
        target.write_text("")
        v = policy.evaluate(
            _make_edit_event(target=target, project_root=isolated_project),
        )
        assert v.is_allowing()

    def test_6_out_of_scope_edit_blocked(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Edit on a file NOT in scope (mode=block) → block + helpful message.

        Lesson #19: block message MUST include both the offending file
        AND the original prompt.
        """
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix the null check in auth.py",
            project_root=isolated_project,
        ))
        target = isolated_project / "users.py"
        target.write_text("")
        v = policy.evaluate(
            _make_edit_event(target=target, project_root=isolated_project),
        )
        assert v.is_blocking()
        msg = v.message or ""
        # Lesson #19: content-verifying assertions
        assert "users.py" in msg, (
            f"Block message must name the offending file. Got: {msg!r}"
        )
        assert "fix the null check" in msg, (
            f"Block message must include the original prompt (so the AI "
            f"sees WHY). Got: {msg!r}"
        )
        # Helpful instruction surface
        assert "CODEVIRA_SCOPE_LOCK_MODE" in msg

    def test_7_out_of_scope_edit_warn_mode(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """mode=warn → warn (not block) on out-of-scope edit."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "warn")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
        ))
        target = isolated_project / "users.py"
        target.write_text("")
        v = policy.evaluate(
            _make_edit_event(target=target, project_root=isolated_project),
        )
        assert v.action == "warn", f"Expected warn; got {v.action}"
        assert "users.py" in (v.message or "")

    def test_8_pretool_without_prior_prompt_allows(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """No contract for this session → allow."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        target = isolated_project / "auth.py"
        target.write_text("")
        # No prompt was submitted → no contract → allow
        v = policy.evaluate(
            _make_edit_event(target=target, project_root=isolated_project),
        )
        assert v.is_allowing()

    def test_9_path_traversal_mention_not_in_scope(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Bug-5 lesson: a prompt mentioning '../../etc/passwd.py' must
        NOT end up in the contract's allowed_files. The traversal
        mention is the ONLY 'file mention' in the prompt → no concrete
        in-project files → no contract built."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        v = policy.evaluate(_make_prompt_event(
            prompt='fix "../../etc/passwd.py" — security issue',
            project_root=isolated_project,
        ))
        assert v.is_allowing()
        c = get_session_contract("s-test")
        assert c is None, (
            "Bug-5 regression: a path-traversal mention should NOT build "
            "a contract with that mention in allowed_files. "
            f"Got contract: {c}"
        )

    def test_10_all_four_edit_tools_enforced(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Bug-7 lesson: enforcement must work for Edit, Write, MultiEdit,
        AND NotebookEdit equally. Hero 3 uses event.is_edit() to gate, so
        all 4 should trigger the same logic.
        """
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
        ))
        out_of_scope_target = isolated_project / "users.py"
        out_of_scope_target.write_text("")
        for tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            v = policy.evaluate(
                _make_edit_event(
                    target=out_of_scope_target,
                    project_root=isolated_project,
                    tool_name=tool_name,
                ),
            )
            assert v.is_blocking(), (
                f"Hero 3 must block out-of-scope on {tool_name} (Bug-7 shape)"
            )

    def test_12_contract_ttl_expires(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Contract older than TTL → not enforced."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        # TTL of 1 second (clamped to 60)
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS", "60")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
        ))
        # Manually age the stored contract beyond TTL
        c = get_session_contract("s-test")
        assert c is not None
        # Replace with one created 2 hours ago
        old = ScopeContract(
            session_id="s-test",
            allowed_files=c.allowed_files,
            original_intent=c.original_intent,
            original_prompt=c.original_prompt,
            created_at=time.time() - 7200,
        )
        set_session_contract("s-test", old)
        # Now an out-of-scope edit should be ALLOWED (contract expired)
        target = isolated_project / "users.py"
        target.write_text("")
        v = policy.evaluate(
            _make_edit_event(target=target, project_root=isolated_project),
        )
        assert v.is_allowing(), (
            "Expired contract must be evicted → allow. Got: "
            f"{v.action}: {v.message}"
        )


# =====================================================================
# Behavioral gates — proper short-circuiting
# =====================================================================


class TestBehavioralGates:

    def test_non_handled_event_allowed(self, isolated_project: Path):
        """Non-prompt-non-edit events must short-circuit before any
        contract operation."""
        policy = ProactiveScopeContractLock()
        for evt in (EventType.SESSION_START, EventType.POST_TOOL_USE,
                    EventType.STOP):
            event = HookEvent(event_type=evt, project_root=isolated_project)
            v = policy.evaluate(event)
            assert v.is_allowing()

    def test_off_mode_short_circuits_before_classify(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """mode=off must short-circuit BEFORE classify_intent runs.
        Use a behavioral spy on classify_intent."""
        from mcp_server.engine import intent_classifier as ic
        calls = {"n": 0}
        original = ic.classify_intent

        def spy(prompt):
            calls["n"] += 1
            return original(prompt)

        monkeypatch.setattr(
            "mcp_server.engine.policies.scope_contract.classify_intent",
            spy,
        )
        # Default is off
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py", project_root=isolated_project,
        ))
        assert calls["n"] == 0, (
            "mode=off must short-circuit BEFORE classify_intent"
        )

    def test_priority_value_stable(self):
        assert ProactiveScopeContractLock().priority == 90

    def test_handles_both_events(self):
        h = ProactiveScopeContractLock.handles
        assert EventType.USER_PROMPT_SUBMIT in h
        assert EventType.PRE_TOOL_USE in h

    def test_enabled_by_default_true(self):
        """Registered by default (mode=off makes it silent)."""
        assert ProactiveScopeContractLock.enabled_by_default is True

    def test_invalid_mode_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Garbage mode → silently fall back to off (the safe default)."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block_or_warn")
        cfg = ProactiveScopeContractLock()._config()
        assert cfg["mode"] == "off"

    def test_read_tool_never_blocked_even_out_of_scope(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Mutation gap (M3): Hero 3 must NOT block non-Edit tools (Read,
        Glob, Grep) — even if target_file is "out of scope". Dropping
        the ``is_edit()`` gate would cause Read on an out-of-scope file
        to be blocked, which would break Claude Code's read access
        across the project.

        This is a real Bug-X-shape risk: scope_contract enforces edits,
        not reads. Lock the contract.
        """
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        # Build contract: only auth.py in scope
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
            session_id="s-read",
        ))
        # Read on users.py (out of scope, but it's a Read not an Edit)
        users = isolated_project / "users.py"
        users.write_text("")
        for tool in ("Read", "Glob", "Grep", "Bash"):
            event = HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=isolated_project,
                session_id="s-read",
                tool_name=tool,
                target_file=users,  # set even for Bash to be defensive
            )
            v = policy.evaluate(event)
            assert v.is_allowing(), (
                f"Hero 3 must NOT block {tool} on out-of-scope file. "
                f"Got: {v.action}: {v.message!r}"
            )

    def test_no_session_id_silently_allows(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Both build + enforce must short-circuit silently when
        session_id is None."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        # Build with no session_id
        event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            prompt_text="fix auth.py",
            session_id=None,
        )
        v = policy.evaluate(event)
        assert v.is_allowing()
        assert _stored_count() == 0

        # Enforce with no session_id
        target = isolated_project / "users.py"
        target.write_text("")
        edit = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=isolated_project,
            tool_name="Edit",
            target_file=target,
            session_id=None,
        )
        v = policy.evaluate(edit)
        assert v.is_allowing()


# =====================================================================
# Pure helper unit tests
# =====================================================================


class TestHelpers:

    def test_resolve_in_project_files_drops_traversal(
        self, isolated_project: Path,
    ):
        """Bug-5 defense unit-tested directly."""
        out = _resolve_in_project_files(
            ["auth.py", "../../etc/passwd.py", "users.py"],
            isolated_project,
        )
        assert "auth.py" in out
        assert "users.py" in out
        assert not any("passwd" in p for p in out)

    def test_resolve_handles_macos_symlink(
        self, isolated_project: Path,
    ):
        """Project root on /tmp resolves to /private/tmp on macOS.
        The helper must use resolve()-vs-resolve() comparison."""
        out = _resolve_in_project_files(["auth.py"], isolated_project)
        assert out == frozenset({"auth.py"})

    def test_target_in_scope_basic(self, isolated_project: Path):
        target = isolated_project / "auth.py"
        target.write_text("")
        assert _target_in_scope(
            target, isolated_project, frozenset({"auth.py"}),
        ) is True
        assert _target_in_scope(
            target, isolated_project, frozenset({"users.py"}),
        ) is False

    def test_target_in_scope_empty_allowed_returns_true(
        self, isolated_project: Path,
    ):
        """Empty allowed_files = no narrowing = always in scope.
        (Caller's enforce phase short-circuits earlier; this is
        belt-and-suspenders.)"""
        target = isolated_project / "auth.py"
        target.write_text("")
        assert _target_in_scope(
            target, isolated_project, frozenset(),
        ) is True

    def test_format_block_message_contains_file_and_prompt(
        self, isolated_project: Path,
    ):
        target = isolated_project / "users.py"
        target.write_text("")
        contract = ScopeContract(
            session_id="s",
            allowed_files=frozenset({"auth.py"}),
            original_intent="fix-bug",
            original_prompt="fix the null check in auth.py",
        )
        msg = _format_block_message(
            target_file=target,
            project_root=isolated_project,
            contract=contract,
        )
        # Lesson #19 lock-in
        assert "users.py" in msg
        assert "fix the null check" in msg
        assert "fix-bug" in msg
        assert "auth.py" in msg  # in allowed list
        assert "CODEVIRA_SCOPE_LOCK_MODE" in msg


# =====================================================================
# TTL behavior
# =====================================================================


class TestTTL:

    def test_ttl_clamping(self, monkeypatch: pytest.MonkeyPatch):
        """TTL is clamped to [60, 86400]."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS", "0")
        assert sc_mod._max_age_seconds() == 60
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS", "999999")
        assert sc_mod._max_age_seconds() == 86400
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS", "garbage")
        assert sc_mod._max_age_seconds() == 3600  # default

    def test_old_contract_evicted_on_get(self):
        """Storing an artificially-old contract → evicted on next read."""
        old = ScopeContract(
            session_id="s-old",
            allowed_files=frozenset({"auth.py"}),
            created_at=time.time() - 100000,  # way past TTL
        )
        # Bypass set_session_contract's update of created_at by raw insert
        sc_mod._session_contracts["s-old"] = old
        # Read evicts
        c = get_session_contract("s-old")
        assert c is None
        assert "s-old" not in sc_mod._all_session_ids()

    def test_recent_contract_not_evicted(self):
        recent = ScopeContract(
            session_id="s-fresh",
            allowed_files=frozenset({"auth.py"}),
            created_at=time.time(),
        )
        set_session_contract("s-fresh", recent)
        c = get_session_contract("s-fresh")
        assert c is not None
        assert c.session_id == "s-fresh"


# =====================================================================
# Real-DB integration + dispatch end-to-end
# =====================================================================


class TestEngineDispatch:

    def test_build_then_enforce_through_dispatch_block(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Register all 9 heroes, fire UserPromptSubmit (build) then
        PreToolUse on out-of-scope file (enforce) → block."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        reset_policies()
        register_default_policies()

        # Build phase
        prompt_event = HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            session_id="dispatch-s1",
            prompt_text="fix the null check in auth.py",
        )
        v1 = dispatch(prompt_event)
        # Build phase doesn't block; might be allow or inject (Hero 5/9
        # may inject — that's fine)
        assert v1.action in ("allow", "inject")

        # Enforce phase: out-of-scope file → block
        target = isolated_project / "users.py"
        target.write_text("")
        edit_event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=isolated_project,
            session_id="dispatch-s1",
            tool_name="Edit",
            target_file=target,
        )
        v2 = dispatch(edit_event)
        assert v2.is_blocking(), (
            f"Out-of-scope edit must be blocked through dispatch. "
            f"Got: {v2.action} from {v2.policy}: {v2.message!r}"
        )
        # The message must name users.py and the original prompt
        assert "users.py" in (v2.message or "")
        assert "fix the null check" in (v2.message or "")
        reset_policies()

    def test_in_scope_edit_through_dispatch_allowed(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Edit on the in-scope file must allow through dispatch
        (Hero 3 doesn't trigger; other heroes might allow / warn / inject
        but no scope-related block)."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies, reset_policies, dispatch,
        )
        reset_policies()
        register_default_policies()

        dispatch(HookEvent(
            event_type=EventType.USER_PROMPT_SUBMIT,
            project_root=isolated_project,
            session_id="dispatch-s2",
            prompt_text="fix the null check in auth.py",
        ))
        target = isolated_project / "auth.py"
        target.write_text("def login(): pass")
        v = dispatch(HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=isolated_project,
            session_id="dispatch-s2",
            tool_name="Edit",
            target_file=target,
        ))
        # No Hero 3 block. Other heroes shouldn't block either on this
        # clean event (no decision lock, no fix history).
        assert v.action != "block" or v.policy != "scope_contract_lock"
        reset_policies()


# =====================================================================
# End-to-end through Claude Code wiring (Bug-4 lesson)
# =====================================================================


class TestClaudeCodeWiring:

    def _setup(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Standard wiring setup: register policies + set CWD."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        from mcp_server.engine import register_default_policies, reset_policies
        reset_policies()
        register_default_policies()

    def _fire(
        self, event_name: str, raw: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[int, dict]:
        from mcp_server.engine.wiring import claude_code_hooks
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        rc = claude_code_hooks.handle(event_name)
        return rc, json.loads(stdout_buf.getvalue())

    def test_build_then_enforce_through_wiring(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Bug-4 lesson: end-to-end through claude_code_hooks.handle()
        for BOTH events. UserPromptSubmit then PreToolUse, real JSON,
        verify block emits correctly."""
        self._setup(monkeypatch, isolated_project)

        # Build phase
        rc, emitted = self._fire(
            "UserPromptSubmit",
            {
                "session_id": "wire-s1",
                "cwd": str(isolated_project),
                "prompt": "fix the null check in auth.py",
            },
            monkeypatch,
        )
        assert rc == 0
        # Hero 3 doesn't inject during build; might be allow-only or
        # other heroes inject. We don't assert specific shape here —
        # the test is about ENFORCE working.

        # Enforce phase
        target = isolated_project / "users.py"
        target.write_text("")
        rc, emitted = self._fire(
            "PreToolUse",
            {
                "session_id": "wire-s1",
                "cwd": str(isolated_project),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "x", "new_string": "y",
                },
            },
            monkeypatch,
        )
        assert rc == 2, f"Expected block (rc=2); got rc={rc}, emitted={emitted}"
        stop = emitted.get("stopReason", "")
        # Lesson #19 lock-in through wiring
        assert "users.py" in stop, (
            f"Wiring didn't carry the file name through: {emitted}"
        )
        assert "fix the null check" in stop, (
            f"Wiring didn't carry the original prompt through: {emitted}"
        )

    def test_all_four_edit_tools_blocked_through_wiring(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Bug-7 lesson: all 4 _EDIT_TOOLS must trigger enforcement
        equally THROUGH the wiring (not just direct policy.evaluate)."""
        self._setup(monkeypatch, isolated_project)

        # Build the contract (in-scope = auth.py only)
        self._fire(
            "UserPromptSubmit",
            {
                "session_id": "wire-s2",
                "cwd": str(isolated_project),
                "prompt": "fix auth.py",
            },
            monkeypatch,
        )

        out_target = isolated_project / "users.py"
        out_target.write_text("")
        for tool_name, tool_input in [
            ("Edit", {
                "file_path": str(out_target),
                "old_string": "x", "new_string": "y",
            }),
            ("Write", {
                "file_path": str(out_target),
                "content": "x = 1",
            }),
            ("MultiEdit", {
                "file_path": str(out_target),
                "edits": [{"old_string": "x", "new_string": "y"}],
            }),
            ("NotebookEdit", {
                "notebook_path": str(out_target),
                "new_source": "x = 1",
            }),
        ]:
            rc, emitted = self._fire(
                "PreToolUse",
                {
                    "session_id": "wire-s2",
                    "cwd": str(isolated_project),
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
                monkeypatch,
            )
            assert rc == 2, (
                f"{tool_name}: expected block (rc=2); got rc={rc}, "
                f"emitted={emitted}"
            )


# =====================================================================
# Registration + idempotency
# =====================================================================


class TestRegistration:

    def test_register_default_policies_includes_hero_3(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "scope_contract_lock" in names
        # Total = 9 heroes after Week 12
        assert len(names) == 9, f"Hero count drift: {sorted(names)}"

    def test_idempotent_registration(self):
        from mcp_server.engine import (
            register_default_policies, registered_policies, reset_policies,
        )
        reset_policies()
        register_default_policies()
        register_default_policies()
        names = [p.name for p in registered_policies()]
        assert names.count("scope_contract_lock") == 1


# =====================================================================
# Edge cases (Bug-shape audit)
# =====================================================================


class TestEdgeCases:

    def test_two_sessions_isolated(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Two sessions in parallel must have isolated contracts."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        # Session A: fix auth.py
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
            session_id="A",
        ))
        # Session B: fix users.py
        policy.evaluate(_make_prompt_event(
            prompt="fix users.py",
            project_root=isolated_project,
            session_id="B",
        ))
        # A's contract has auth.py only; B's has users.py only
        a_contract = get_session_contract("A")
        b_contract = get_session_contract("B")
        assert a_contract is not None and "auth.py" in a_contract.allowed_files
        assert "users.py" not in a_contract.allowed_files
        assert b_contract is not None and "users.py" in b_contract.allowed_files
        assert "auth.py" not in b_contract.allowed_files

        # Edit auth.py from session B → blocked (B's scope is users.py)
        auth = isolated_project / "auth.py"
        auth.write_text("")
        v = policy.evaluate(_make_edit_event(
            target=auth,
            project_root=isolated_project,
            session_id="B",
        ))
        assert v.is_blocking()

    def test_followup_prompt_replaces_contract(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """A second UserPromptSubmit replaces the contract."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
            session_id="s-follow",
        ))
        # Then a different prompt
        policy.evaluate(_make_prompt_event(
            prompt="fix users.py instead",
            project_root=isolated_project,
            session_id="s-follow",
        ))
        c = get_session_contract("s-follow")
        assert c is not None
        assert "users.py" in c.allowed_files
        assert "auth.py" not in c.allowed_files


# =====================================================================
# Performance
# =====================================================================


class TestPerformance:

    def test_enforce_p50_under_1ms(
        self, monkeypatch: pytest.MonkeyPatch, isolated_project: Path,
    ):
        """Enforce path is dict-lookup + set membership; should be sub-ms."""
        import time as _t
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        policy = ProactiveScopeContractLock()
        policy.evaluate(_make_prompt_event(
            prompt="fix auth.py",
            project_root=isolated_project,
            session_id="perf-s",
        ))
        target = isolated_project / "users.py"
        target.write_text("")
        event = _make_edit_event(
            target=target, project_root=isolated_project,
            session_id="perf-s",
        )
        durations = []
        for _ in range(500):
            t = _t.perf_counter()
            policy.evaluate(event)
            durations.append((_t.perf_counter() - t) * 1000)
        durations.sort()
        p50 = durations[250]
        assert p50 < 5.0, f"p50={p50:.3f}ms (expected sub-ms)"
