"""
test_qa_round_week12.py — Integrated QA across Weeks 1-12 (9 heroes shipped).

Proactive deep-audit round applied from the start (post-Bug-1-8). The
checklist:

  1. Path-traversal probe for every NEW user-controlled path argument
  2. Empty-section probe for every formatter
  3. Content-verifying assertions (no header-only checks)
  4. Cache-collision probe for every new signal accessor
  5. Bug-X-shape audit (declared support traces end-to-end)
  6. All 4 _EDIT_TOOLS through wiring (Bug-7 lesson)

Hero 3 is the first multi-event policy and the highest-risk hero in the
master plan. This round verifies:

  - Default registration includes Hero 3 (off-by-default)
  - UserPromptSubmit eligibility is now {Hero 5, Hero 9, Hero 3}
  - PreToolUse eligibility includes Hero 3 alongside Heroes 1, 2, 4
  - Hero 3 + other PreToolUse policies coexist (priority order, isolation)
  - CODEVIRA_ENGINE=0 also kills the build phase (UserPromptSubmit)
  - Hero 3's contract storage is process-global; doesn't leak to other
    SignalContext events
  - Path-traversal in prompt mention NEVER reaches signal calls
  - All 4 _EDIT_TOOLS enforced equally through wiring
  - The wiring path for BOTH events (Bug-4 lesson)
"""

from __future__ import annotations

import io
import json
import sys
import time
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
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    return project


@pytest.fixture(autouse=True)
def _isolate_engine_and_storage(monkeypatch: pytest.MonkeyPatch):
    from mcp_server.engine.runner import reset_policies
    from mcp_server.engine.scope_contract import clear_all

    reset_policies()
    clear_all()
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
        "CODEVIRA_SCOPE_LOCK_MODE",
        "CODEVIRA_SCOPE_LOCK_MAX_AGE_SECONDS",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_policies()
    clear_all()


def _set_project(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()


# =====================================================================
# L1 — Default registration + multi-event eligibility (Hero 3 lessons)
# =====================================================================


class TestL1_RegistrationAndEligibility:
    def test_nine_heroes_registered_default(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
        )

        register_default_policies()
        names = {p.name for p in registered_policies()}
        assert "scope_contract_lock" in names
        # v2.1.2 Item 4: post_edit_graph_refresh raises the count to 10.
        assert "post_edit_graph_refresh" in names
        assert len(names) == 10

    def test_pre_tool_use_eligibility_includes_hero_3(self):
        """Hero 3 enforces on PreToolUse alongside Hero 1 (decision_lock),
        Hero 2 (anti_regression), Hero 4 (blast_radius). Lock the set."""
        from mcp_server.engine import register_default_policies, registered_policies
        from mcp_server.engine.events import EventType

        register_default_policies()
        pre = {
            p.name
            for p in registered_policies()
            if EventType.PRE_TOOL_USE in set(p.handles)
        }
        # Bug-3-shape lock-in: explicit set, not loose >= check
        assert pre == {
            "decision_lock",
            "anti_regression",
            "blast_radius_veto",
            "scope_contract_lock",
        }, f"PRE_TOOL_USE eligibility drift: {pre}"

    def test_hero_3_off_by_default(
        self,
        isolated_project: Path,
    ):
        """No env override → mode=off → silent allow on EVERY event.
        Confirms Hero 3 is opt-in. Stays out of the way unless user
        explicitly enables."""
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.scope_contract import _stored_count

        reset_policies()
        register_default_policies()

        # Build phase
        dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="off-test",
                prompt_text="fix auth.py",
            )
        )
        # No contract built (mode=off)
        assert _stored_count() == 0

        # Enforce phase
        target = isolated_project / "users.py"
        target.write_text("")
        v = dispatch(
            HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=isolated_project,
                session_id="off-test",
                tool_name="Edit",
                target_file=target,
            )
        )
        assert v.action != "block" or v.policy != "scope_contract_lock"


# =====================================================================
# L2 — Hero 3 + other PreToolUse policies coexist
# =====================================================================


class TestL2_MultiPolicyOnPreToolUse:
    def test_decision_lock_blocks_first_when_both_could_fire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """Decision Lock (priority 100) > Hero 3 (priority 90). When both
        would block on the same Edit, Decision Lock's message is primary;
        Hero 3 is in `other_blocking_policies` metadata.
        """
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from indexer.sqlite_graph import SQLiteGraph

        _set_project(monkeypatch, isolated_project)

        # Plant a locked decision on a file users.py
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s-multi", "x"),
        )
        g.conn.execute(
            "INSERT INTO nodes (id, kind, name, file_path, do_not_revert) "
            "VALUES (?, ?, ?, ?, ?)",
            ("users.py:locked", "function", "locked_user_fn", "users.py", 1),
        )
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s-multi", "we lock users.py code", "users.py", "ctx"),
        )
        g.conn.commit()
        g.close()

        reset_policies()
        register_default_policies()

        # Build a Hero 3 contract that EXCLUDES users.py
        dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="s-multi",
                prompt_text="fix the null check in auth.py",
            )
        )

        # Edit on users.py would trigger BOTH:
        #  - Decision Lock: users.py is do_not_revert
        #  - Hero 3: users.py is out of contract scope
        target = isolated_project / "users.py"
        target.write_text("")
        v = dispatch(
            HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=isolated_project,
                session_id="s-multi",
                tool_name="Edit",
                target_file=target,
            )
        )
        assert v.is_blocking()
        # Decision Lock wins (priority 100 > 90)
        assert v.policy == "decision_lock"
        # Hero 3 is recorded as a co-blocker
        others = v.metadata.get("other_blocking_policies", [])
        assert (
            "scope_contract_lock" in others
        ), f"Hero 3 should appear in other_blocking_policies. Got: {others}"


# =====================================================================
# L3 — Engine kill switch also disables Hero 3 build phase
# =====================================================================


class TestL3_KillSwitch:
    def test_engine_disabled_skips_hero_3_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """CODEVIRA_ENGINE=0 short-circuits dispatch entirely. Even
        though Hero 3 is mode=block (would otherwise build), the kill
        switch prevents the build phase too. Verify storage stays empty.
        """
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.scope_contract import _stored_count

        _set_project(monkeypatch, isolated_project)
        reset_policies()
        register_default_policies()

        dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="kill",
                prompt_text="fix auth.py",
            )
        )
        # Nothing built
        assert _stored_count() == 0


# =====================================================================
# L4 — Crash isolation: Hero 3 raising doesn't break other policies
# =====================================================================


@pytest.mark.xfail(
    reason=(
        "v2.2.0 Phase C: cross_session.CrossSessionConsistency was replaced by relevance_inject.RelevanceInject in register_default_policies(). This test asserts behavior specific to the deprecated h5 policy. Phase E deletes cross_session.py + this test entirely."
    ),
    strict=True,
)
class TestL4_CrashIsolation:
    def test_h3_crash_on_prompt_does_not_break_h5_inject(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """Hero 3 raising on UserPromptSubmit must not break Hero 5
        (which injects on the same event)."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.scope_contract import (
            ProactiveScopeContractLock,
        )
        from indexer.sqlite_graph import SQLiteGraph

        _set_project(monkeypatch, isolated_project)
        # Plant a decision so Hero 5 has something to surface
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(graph_db)
        g.conn.execute(
            "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
            ("s-crash", "x"),
        )
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("s-crash", "use bcrypt over argon2", "auth.py", "perf"),
        )
        g.conn.commit()
        g.close()

        # Sabotage Hero 3
        def crashing_evaluate(self, event, signals=None):
            raise RuntimeError("intentional H3 crash for L4")

        monkeypatch.setattr(
            ProactiveScopeContractLock,
            "evaluate",
            crashing_evaluate,
        )

        reset_policies()
        register_default_policies()

        v = dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="s-crash",
                prompt_text="tell me about bcrypt usage",
            )
        )
        # Hero 5 still injects (priority 30 > Hero 3's 90 was for PreToolUse;
        # Hero 5 is on UserPromptSubmit independently)
        assert v.action == "inject", (
            f"Hero 5 should inject despite Hero 3 crash. Got: "
            f"{v.action}: {v.message!r}"
        )


# =====================================================================
# L5 — Path-traversal probe through wiring (Bug-5 + Bug-7 combined)
# =====================================================================


class TestL5_PathTraversalThroughWiring:
    def test_path_traversal_in_prompt_does_not_build_dangerous_contract(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """End-to-end through claude_code_hooks: a prompt with a path-
        traversal mention must not produce a contract whose allowed_files
        contains an out-of-project path. If it did, the AI could trick
        Hero 3 into "approving" edits at /etc/passwd.py.

        This combines Bug 5 (path traversal in file mentions) + Bug 7
        (wiring path) — the user's challenge that surfaced both bugs in
        Week 11 retrospective."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
        )
        from mcp_server.engine.wiring import claude_code_hooks
        from mcp_server.engine.scope_contract import (
            get_session_contract,
        )

        _set_project(monkeypatch, isolated_project)
        reset_policies()
        register_default_policies()

        raw = {
            "session_id": "trav-s",
            "cwd": str(isolated_project),
            "prompt": 'fix "../../etc/passwd.py" — security',
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = claude_code_hooks.handle("UserPromptSubmit")
        assert rc == 0
        # No contract built (only mention was a traversal one → stripped)
        c = get_session_contract("trav-s")
        assert c is None or len(c.allowed_files) == 0, (
            "Bug 5 regression through wiring: traversal mention reached "
            f"contract. Contract: {c}"
        )


# =====================================================================
# L6 — All 4 _EDIT_TOOLS enforced through wiring (Bug-7 lesson)
# =====================================================================


class TestL6_AllEditToolsThroughWiring:
    def _setup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        _set_project(monkeypatch, isolated_project)
        from mcp_server.engine import register_default_policies, reset_policies

        reset_policies()
        register_default_policies()

        # Build the contract: scope = auth.py
        from mcp_server.engine.wiring import claude_code_hooks

        raw = {
            "session_id": "all-tools",
            "cwd": str(isolated_project),
            "prompt": "fix auth.py",
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        claude_code_hooks.handle("UserPromptSubmit")

    def _fire_pretool(
        self,
        tool_name: str,
        tool_input: dict,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[int, dict]:
        from mcp_server.engine.wiring import claude_code_hooks

        raw = {
            "session_id": "all-tools",
            "cwd": str(isolated_project),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        rc = claude_code_hooks.handle("PreToolUse")
        return rc, json.loads(stdout_buf.getvalue())

    def test_all_four_edit_tools_blocked_for_out_of_scope_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """Bug-7 lesson: enforcement applies equally across all 4
        _EDIT_TOOLS through the wiring layer."""
        self._setup(monkeypatch, isolated_project)
        out = isolated_project / "users.py"
        out.write_text("")

        for tool_name, tool_input in [
            (
                "Edit",
                {
                    "file_path": str(out),
                    "old_string": "x",
                    "new_string": "y",
                },
            ),
            ("Write", {"file_path": str(out), "content": "x = 1"}),
            (
                "MultiEdit",
                {
                    "file_path": str(out),
                    "edits": [{"old_string": "x", "new_string": "y"}],
                },
            ),
            (
                "NotebookEdit",
                {
                    "notebook_path": str(out),
                    "new_source": "x = 1",
                },
            ),
        ]:
            rc, emitted = self._fire_pretool(
                tool_name,
                tool_input,
                isolated_project,
                monkeypatch,
            )
            assert rc == 2, f"{tool_name}: expected block (rc=2). Got: {rc}, {emitted}"
            # Lesson #19: block message must contain the offending file
            stop = emitted.get("stopReason", "")
            assert (
                "users.py" in stop
            ), f"{tool_name}: block message missing offending file: {emitted}"

    def test_in_scope_edit_allowed_for_all_four_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """Positive control: in-scope file passes through all 4 tools."""
        self._setup(monkeypatch, isolated_project)
        target = isolated_project / "auth.py"
        target.write_text("def login(): pass")

        for tool_name, tool_input in [
            (
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "pass",
                    "new_string": "return None",
                },
            ),
            ("Write", {"file_path": str(target), "content": "x = 1"}),
            (
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [{"old_string": "pass", "new_string": "return None"}],
                },
            ),
            (
                "NotebookEdit",
                {
                    "notebook_path": str(target),
                    "new_source": "x = 1",
                },
            ),
        ]:
            rc, emitted = self._fire_pretool(
                tool_name,
                tool_input,
                isolated_project,
                monkeypatch,
            )
            assert rc == 0, (
                f"In-scope {tool_name}: expected allow (rc=0). "
                f"Got: rc={rc}, {emitted}"
            )


# =====================================================================
# L7 — TTL behavior across event boundaries
# =====================================================================


class TestL7_TTLAcrossEvents:
    def test_old_contract_does_not_block_new_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """A stale contract for session A must not interfere with
        session B. Tests the per-session keying explicitly."""
        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        from mcp_server.engine import (
            register_default_policies,
            reset_policies,
            dispatch,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.scope_contract import (
            set_session_contract,
            ScopeContract,
        )

        _set_project(monkeypatch, isolated_project)
        reset_policies()
        register_default_policies()

        # Plant an old contract for session "ghost"
        old_contract = ScopeContract(
            session_id="ghost",
            allowed_files=frozenset({"auth.py"}),
            original_intent="fix-bug",
            original_prompt="ancient prompt",
            created_at=time.time(),
        )
        set_session_contract("ghost", old_contract)

        # Now session "live" submits a prompt that builds its own contract
        dispatch(
            HookEvent(
                event_type=EventType.USER_PROMPT_SUBMIT,
                project_root=isolated_project,
                session_id="live",
                prompt_text="fix users.py",
            )
        )

        # Edit users.py from session "live" → in scope → allow
        target = isolated_project / "users.py"
        target.write_text("")
        v = dispatch(
            HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=isolated_project,
                session_id="live",
                tool_name="Edit",
                target_file=target,
            )
        )
        # Hero 3 must NOT block (live's contract has users.py).
        assert (
            v.action != "block" or v.policy != "scope_contract_lock"
        ), f"Hero 3 cross-session bleed: {v.action} from {v.policy}"


# =====================================================================
# L8 — Empty-section / vacuous-assertion sweep (Lesson #19)
# =====================================================================


class TestL8_BlockMessageSemantics:
    """Lesson #19: tests that assert a header exists must also assert
    body content. For Hero 3, the block message is a single block (not
    multi-section), so we verify EVERY required field appears."""

    def test_block_message_contains_all_required_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        from mcp_server.engine.policies.scope_contract import (
            ProactiveScopeContractLock,
        )
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.scope_contract import (
            set_session_contract,
            ScopeContract,
        )

        monkeypatch.setenv("CODEVIRA_SCOPE_LOCK_MODE", "block")
        contract = ScopeContract(
            session_id="msg-test",
            allowed_files=frozenset({"auth.py", "auth_helpers.py"}),
            original_intent="fix-bug",
            original_prompt="fix the null check in auth.py",
            created_at=time.time(),
        )
        set_session_contract("msg-test", contract)

        target = isolated_project / "wallet.py"
        target.write_text("")

        policy = ProactiveScopeContractLock()
        v = policy.evaluate(
            HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=isolated_project,
                session_id="msg-test",
                tool_name="Edit",
                target_file=target,
            )
        )
        assert v.is_blocking()
        msg = v.message or ""

        # Lesson #19: every required field must appear (no header-only)
        required = [
            "wallet.py",  # offending file
            "fix the null check",  # original prompt
            "fix-bug",  # intent
            "auth.py",  # allowed file
            "auth_helpers.py",  # second allowed file
            "CODEVIRA_SCOPE_LOCK_MODE",  # how to override
            "scope-lock veto",  # action label
        ]
        missing = [s for s in required if s not in msg.lower() and s not in msg]
        assert not missing, (
            f"Block message missing required fields: {missing}\n"
            f"Got message: {msg!r}"
        )
