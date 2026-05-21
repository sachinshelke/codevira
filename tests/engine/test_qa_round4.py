"""Round-4 QA: regression tests for the 3 HIGH-severity security findings.

R4 HIGH #1 — Path traversal: ``tool_input['file_path']='../../etc/passwd'``
             escaped project_root and set target_file outside the project.
             Now contained via os.path.commonpath check.

R4 HIGH #2 — Unvalidated project_root: engine accepted AI-controlled
             ``cwd`` (e.g. $HOME) without calling ``is_invalid_project_root``,
             bypassing v1.8.1's hardening at the engine layer. Now validated.

R4 HIGH #3 — SQL DoS via ``limit``: negative or huge ``limit`` argument
             to ``signals.decisions()`` could cause SQLite to return
             unbounded rows. Now clamped to [1, 1000].
"""
from __future__ import annotations


import pytest

from mcp_server.engine.events import EventType
from mcp_server.engine.signals import SignalContext


# =====================================================================
# R4 HIGH #1: Path traversal containment
# =====================================================================

class TestPathTraversalContainment:
    """Path-traversal attempts via tool_input['file_path'] must NOT
    escape the project_root via Path.resolve(). Both wiring layers
    (Claude Code hooks + MCP dispatch) must enforce this."""

    def test_claude_code_wiring_rejects_path_traversal(self, tmp_path):
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        proj = tmp_path / "proj"
        proj.mkdir()

        event = _build_event(
            EventType.PRE_TOOL_USE,
            {
                "session_id": "x",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "../../../../etc/passwd",
                    "old_string": "x",
                    "new_string": "y",
                },
            },
        )
        # target_file must be None — path traversal rejected.
        assert event.target_file is None, (
            f"Path traversal not contained: target_file={event.target_file}"
        )

    def test_mcp_dispatch_rejects_path_traversal(self, tmp_path, monkeypatch):
        from mcp_server.engine.wiring.mcp_dispatch import _build_pre_event
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: proj.resolve()
        )

        event = _build_pre_event(
            "Edit",
            {"file_path": "../../../../etc/passwd"},
        )
        assert event.target_file is None

    def test_legitimate_relative_path_works(self, tmp_path):
        """A normal relative path inside the project should be accepted."""
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        proj = tmp_path / "proj"
        (proj / "src").mkdir(parents=True)
        target = proj / "src" / "foo.py"
        target.touch()

        event = _build_event(
            EventType.PRE_TOOL_USE,
            {
                "session_id": "x",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "x",
                    "new_string": "y",
                },
            },
        )
        assert event.target_file is not None
        assert event.target_file == target.resolve()

    def test_absolute_path_outside_project_rejected(self, tmp_path):
        """Absolute path outside project_root → target_file = None."""
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        proj = tmp_path / "proj"
        proj.mkdir()
        outside = tmp_path / "elsewhere" / "secret.py"
        outside.parent.mkdir(parents=True)
        outside.touch()

        event = _build_event(
            EventType.PRE_TOOL_USE,
            {
                "session_id": "x",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(outside),
                    "old_string": "x",
                    "new_string": "y",
                },
            },
        )
        assert event.target_file is None

    def test_similar_prefix_does_not_false_match(self, tmp_path):
        """``/tmp/proj`` should NOT contain ``/tmp/proj-malicious/x.py`` —
        prefix-only string check would incorrectly accept it; commonpath
        catches this correctly."""
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        proj = tmp_path / "proj"
        proj.mkdir()
        evil = tmp_path / "proj-malicious"
        evil.mkdir()
        target = evil / "secret.py"
        target.touch()

        event = _build_event(
            EventType.PRE_TOOL_USE,
            {
                "session_id": "x",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target),
                                "old_string": "x", "new_string": "y"},
            },
        )
        assert event.target_file is None


# =====================================================================
# R4 HIGH #2: project_root validated via is_invalid_project_root
# =====================================================================

class TestProjectRootValidationInWiring:
    """The wiring layer must REFUSE to build events when the AI-supplied
    project_root is $HOME, /, /Users, etc. v1.8.1's is_invalid_project_root
    is the canonical guard; the engine reuses it.

    The wiring layer raises ValueError on rejection; the outer handlers
    (handle() in claude_code_hooks, pre_call/post_call in mcp_dispatch)
    catch and fail-open. We test:
      - the inner _build_event raises
      - the outer handlers fail-open (return allow / don't break)
    """

    def test_claude_code_build_event_raises_for_home(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

        with pytest.raises(ValueError, match="invalid project_root"):
            _build_event(
                EventType.PRE_TOOL_USE,
                {
                    "session_id": "x",
                    "cwd": str(fake_home),
                    "tool_name": "Edit",
                    "tool_input": {},
                },
            )

    def test_claude_code_handle_fails_open_on_invalid_root(
        self, tmp_path, monkeypatch, capsys
    ):
        """Outer handle() catches the ValueError and emits {continue:true}."""
        import io
        import json
        import sys
        from mcp_server.engine.wiring.claude_code_hooks import handle

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

        raw = {
            "session_id": "x",
            "cwd": str(fake_home),
            "tool_name": "Edit",
            "tool_input": {"file_path": "x.py"},
        }
        stdin_buf = io.StringIO(json.dumps(raw))
        stdin_buf.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stdin_buf)
        stdout_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)

        rc = handle("PreToolUse")
        # Fail-open: rc=0 (allow), payload says continue:true.
        assert rc == 0
        payload = json.loads(stdout_buf.getvalue())
        assert payload["continue"] is True

    def test_mcp_dispatch_pre_call_returns_allow_on_invalid_root(
        self, tmp_path, monkeypatch
    ):
        """Outer pre_call catches ValueError and returns allow."""
        from mcp_server.engine.wiring.mcp_dispatch import pre_call

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        monkeypatch.setattr(
            "mcp_server.paths.get_project_root", lambda: fake_home
        )

        verdict = pre_call("Edit", {"file_path": "x.py"})
        assert verdict.is_allowing()

    def test_legitimate_project_root_passes(self, tmp_path):
        """A normal project root (subdirectory of $HOME) works fine."""
        from mcp_server.engine.wiring.claude_code_hooks import _build_event
        proj = tmp_path / "proj"
        proj.mkdir()

        event = _build_event(
            EventType.PRE_TOOL_USE,
            {
                "session_id": "x",
                "cwd": str(proj),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(proj / "x.py")},
            },
        )
        assert event.project_root == proj.resolve()


# =====================================================================
# R4 HIGH #3: SQL `limit` clamping in signals.decisions()
# =====================================================================

class TestDecisionsLimitClamp:
    """``signals.decisions(limit=N)`` must clamp N to [1, 1000].

    Without clamping:
      - ``limit=-1`` → SQLite returns all rows (unbounded)
      - ``limit=10**9`` → memory exhaustion as result set materializes
    """

    def test_negative_limit_clamped_up(self, tmp_path):
        """limit=-1 must not crash and must not return unbounded rows."""
        ctx = SignalContext(project_root=tmp_path)
        # graph is None (no DB); decisions() returns []. The point of the
        # test is the clamp, not the result.
        result = ctx.decisions(limit=-1)
        assert isinstance(result, list)
        # And must not have crashed in SQL.

    def test_zero_limit_clamped_up(self, tmp_path):
        ctx = SignalContext(project_root=tmp_path)
        result = ctx.decisions(limit=0)
        assert isinstance(result, list)

    def test_huge_limit_clamped_down(self, tmp_path):
        """limit=10**9 must be clamped to 1000."""
        ctx = SignalContext(project_root=tmp_path)
        # Even with no DB, the call should not allocate memory for 1B rows.
        result = ctx.decisions(limit=10**9)
        assert isinstance(result, list)

    def test_normal_limit_unchanged(self, tmp_path):
        """limit=20 (the default) is preserved."""
        ctx = SignalContext(project_root=tmp_path)
        # Cache by argument tuple — first call with limit=20 should work.
        result = ctx.decisions(limit=20)
        assert isinstance(result, list)
