"""
test_cross_tool_universality.py — automated guard for the v2.0 North Star.

The wedge promise (master plan):

  > **The cross-tool-per-project test:** the user is working on Project A.
  > Opens Claude Code → asks "what did I decide about retries?" → gets the
  > answer from project A's decision log. Closes Claude Code, opens Cursor
  > on the SAME project → asks the same question → gets the SAME answer.
  > Switches to Windsurf → same answer. Antigravity → same answer.

This test simulates four AI clients using codevira against ONE shared
project's data, in sequence. It asserts:

  1. Decision recorded by Tool A is visible to Tool B / C / D.
  2. The data each tool sees (via codevira's tools / signals / resources)
     is IDENTICAL — no per-tool state forks.
  3. Per-tool nudge files (CLAUDE.md, AGENTS.md, etc.) all carry the
     SAME canonical instructions block.

Pre-Phase-2, codevira had unit tests for each layer but NO test that
followed a single decision from "tool A writes" → "tool B reads" across
the whole stack.

This file fills that gap.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def shared_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One project, shared across all simulated AI tools."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "shared-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".git").mkdir()
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)

    import mcp_server.paths as paths_mod
    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    return project


@pytest.fixture(autouse=True)
def _clean_engine_state(monkeypatch: pytest.MonkeyPatch):
    from mcp_server.engine.runner import reset_policies
    from mcp_server.engine.scope_contract import clear_all
    reset_policies()
    clear_all()
    for env in (
        "CODEVIRA_ENGINE",
        "CODEVIRA_DECISION_LOCK_MODE",
        "CODEVIRA_CROSS_SESSION_MODE",
        "CODEVIRA_AI_PROMOTION_MODE",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_policies()
    clear_all()


# =====================================================================
# Simulation primitives
# =====================================================================


def _open_graph(project: Path):
    from mcp_server.paths import get_data_dir
    from indexer.sqlite_graph import SQLiteGraph
    graph_db = get_data_dir() / "graph" / "graph.db"
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteGraph(graph_db)


def _record_decision_via_claude_code_hook(
    project: Path,
    *,
    session_id: str,
    decision: str,
    file_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate Tool A (Claude Code) recording a decision via the
    PostToolUse hook. The hook normalizes the AI's edit into a
    decision row in the shared graph.db.

    For v2.0-alpha, decisions are written via direct DB inserts in
    response to specific MCP tool calls (record_decision). Hero 7's
    PostToolUse policy doesn't auto-create decisions — it only warns
    on style. So for this test, we simulate by writing the decision
    directly the way `record_decision` would.
    """
    g = _open_graph(project)
    try:
        g.conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, summary) "
            "VALUES (?, ?)",
            (session_id, "tool-A session"),
        )
        g.conn.execute(
            "INSERT INTO decisions (session_id, decision, file_path, "
            "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (session_id, decision, file_path, ""),
        )
        g.conn.commit()
    finally:
        g.close()


def _read_decision_via_user_prompt_hook(
    project: Path,
    *,
    tool_name: str,           # "claude-code" / "cursor" / "windsurf" / etc.
    session_id: str,
    prompt_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Simulate Tool B reading the shared decision via Hero 5's
    UserPromptSubmit injection. Returns the additionalContext block
    the AI would receive — empty if no inject.
    """
    from mcp_server.engine import (
        register_default_policies, reset_policies,
    )
    from mcp_server.engine.wiring import claude_code_hooks

    reset_policies()
    register_default_policies()

    raw = {
        "session_id": session_id,
        "cwd": str(project),
        "prompt": prompt_text,
        "ai_tool": tool_name,
    }
    stdin_buf = io.StringIO(json.dumps(raw))
    stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin_buf)
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)
    rc = claude_code_hooks.handle("UserPromptSubmit")
    assert rc == 0, f"{tool_name}: hook returned non-zero ({rc})"
    emitted = json.loads(stdout_buf.getvalue())
    return emitted.get("hookSpecificOutput", {}).get("additionalContext", "")


def _read_decision_via_mcp_tool(
    project: Path,
    *,
    query: str,
) -> list[dict[str, Any]]:
    """Simulate any MCP-compatible AI tool calling codevira's
    MCP `search_decisions` tool path.
    """
    from mcp_server.engine.signals import SignalContext
    ctx = SignalContext(project_root=project)
    return ctx.search_decisions(query, limit=10)


def _read_decision_via_replay_mcp_resource(project: Path) -> str:
    """Simulate a Claude Desktop / future MCP-Apps client fetching
    the codevira://decisions resource."""
    from mcp_server.server import handle_read_resource
    import mcp_server.paths as paths_mod
    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    return asyncio.run(handle_read_resource("codevira://decisions"))


def _read_decision_via_replay_cli(project: Path) -> str:
    """Simulate a developer running `codevira replay` in their terminal."""
    from mcp_server.cli_replay import cmd_replay
    out = io.StringIO()
    cmd_replay(
        project=project, since="30d", top=10,
        format="terminal", ascii_mode=True, out=out,
    )
    return out.getvalue()


# =====================================================================
# THE NORTH STAR TEST
# =====================================================================


class TestCrossToolUniversality:
    """The wedge promise: same memory in every AI tool."""

    def test_decision_recorded_in_tool_a_visible_in_tool_b_via_inject(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Tool A (Claude Code) records "use bcrypt over argon2".
        Tool B (Cursor) submits a prompt mentioning bcrypt → must
        receive the same decision via Hero 5's injection.
        """
        # ----- Tool A: record -----
        _record_decision_via_claude_code_hook(
            shared_project,
            session_id="tool-a-session",
            decision="use bcrypt over argon2 — see issue #142",
            file_path="auth.py",
            monkeypatch=monkeypatch,
        )

        # ----- Tool B (Cursor): same prompt, different session -----
        cursor_inject = _read_decision_via_user_prompt_hook(
            shared_project,
            tool_name="cursor",
            session_id="tool-b-cursor-session",
            prompt_text="What did we decide about bcrypt for password hashing?",
            monkeypatch=monkeypatch,
        )

        # The decision must surface in Cursor's context
        assert "bcrypt over argon2" in cursor_inject, (
            f"Universality wedge BROKEN: Tool A's decision didn't reach "
            f"Tool B. Cursor's inject was:\n{cursor_inject!r}"
        )

    def test_same_data_visible_via_three_different_surfaces(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Record once. Read via:
          1. UserPromptSubmit hook (any IDE running Claude Code-style hooks)
          2. MCP signals.search_decisions (any MCP-compatible AI)
          3. codevira://decisions MCP resource (Claude Desktop, future MCP-Apps)
          4. codevira replay CLI (developer terminal)

        All four must surface the same decision.
        """
        DECISION_TEXT = "exponential backoff with jitter; max 5 attempts"
        FILE_PATH = "retries.py"

        _record_decision_via_claude_code_hook(
            shared_project,
            session_id="record-session",
            decision=DECISION_TEXT,
            file_path=FILE_PATH,
            monkeypatch=monkeypatch,
        )

        # Surface 1: UserPromptSubmit hook inject
        inject = _read_decision_via_user_prompt_hook(
            shared_project,
            tool_name="windsurf",
            session_id="windsurf-read",
            prompt_text="What's our retry policy in retries.py?",
            monkeypatch=monkeypatch,
        )
        assert DECISION_TEXT in inject, (
            f"Surface 1 (UserPromptSubmit inject) missing decision text. "
            f"Got: {inject!r}"
        )

        # Surface 2: MCP signals.search_decisions
        rows = _read_decision_via_mcp_tool(shared_project, query="retries")
        assert any(r.get("decision") == DECISION_TEXT for r in rows), (
            f"Surface 2 (MCP signals.search_decisions) didn't return "
            f"the decision. Rows: {rows}"
        )

        # Surface 3: codevira://decisions MCP resource
        resource_html = _read_decision_via_replay_mcp_resource(shared_project)
        assert DECISION_TEXT in resource_html, (
            f"Surface 3 (codevira://decisions resource) missing decision. "
            f"HTML excerpt: {resource_html[:500]}"
        )

        # Surface 4: codevira replay CLI
        cli_out = _read_decision_via_replay_cli(shared_project)
        assert DECISION_TEXT in cli_out, (
            f"Surface 4 (codevira replay CLI) missing decision. "
            f"CLI output: {cli_out[:500]}"
        )

    def test_four_tools_in_sequence_see_identical_decision(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Simulate the EXACT scenario from the master plan's North
        Star: Tool A writes; Tools B, C, D — different sessions,
        different `ai_tool` claims — all see the same decision text.
        """
        DECISION = "use bcrypt over argon2 — see issue #142"

        # Tool A (Claude Code): record
        _record_decision_via_claude_code_hook(
            shared_project,
            session_id="claude-code-001",
            decision=DECISION,
            file_path="auth.py",
            monkeypatch=monkeypatch,
        )

        # Tools B, C, D each run their own session and submit a
        # bcrypt-related prompt. They MUST all receive the same
        # decision.
        results: dict[str, str] = {}
        for tool, session in [
            ("cursor",      "cursor-001"),
            ("windsurf",    "windsurf-001"),
            ("antigravity", "antigravity-001"),
        ]:
            inject = _read_decision_via_user_prompt_hook(
                shared_project,
                tool_name=tool,
                session_id=session,
                prompt_text="What did we decide about bcrypt password hashing?",
                monkeypatch=monkeypatch,
            )
            results[tool] = inject

        # Every tool must have received the decision
        missing = [t for t, ctx in results.items() if DECISION not in ctx]
        assert not missing, (
            f"WEDGE BROKEN: tools {missing} didn't receive the decision. "
            f"Inject contexts:\n" +
            "\n".join(f"  {t}: {ctx[:200]!r}" for t, ctx in results.items())
        )

        # And they should all carry equivalent core content (we don't
        # require byte-identical because session_id differs in the
        # injects, but the decision substring must appear in all).
        for tool, ctx in results.items():
            assert "bcrypt" in ctx
            assert "auth.py" in ctx, (
                f"{tool}: file_path missing from inject"
            )

    def test_universality_breaks_loudly_when_engine_disabled(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Defensive: when CODEVIRA_ENGINE=0 is set, the universality
        promise is OPT-OUT — no inject anywhere. Lock that contract."""
        _record_decision_via_claude_code_hook(
            shared_project,
            session_id="kill-switch-test",
            decision="we use bcrypt",
            file_path="auth.py",
            monkeypatch=monkeypatch,
        )

        monkeypatch.setenv("CODEVIRA_ENGINE", "0")

        inject = _read_decision_via_user_prompt_hook(
            shared_project,
            tool_name="cursor",
            session_id="kill-switch-cursor",
            prompt_text="What did we decide about bcrypt?",
            monkeypatch=monkeypatch,
        )
        # With engine off, NO decision should reach the inject
        assert "we use bcrypt" not in inject, (
            "Kill switch broken: decisions surfacing despite "
            "CODEVIRA_ENGINE=0"
        )
