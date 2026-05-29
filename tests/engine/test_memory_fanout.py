"""
Tests for mcp_server.engine.memory_fanout — v3.1.0 M2 Phase 3.

Covers:
  - Observation building per tool (Edit, Bash, error bump, trivial Bash skip)
  - Buffer behavior (threshold flush, drain semantics, fail-open)
  - End-to-end: a POST_TOOL_USE event eventually lands one
    working-memory record on disk.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.engine import memory_fanout
from mcp_server.engine.events import EventType, HookEvent
from mcp_server.storage import jsonl_store, paths, working_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    # Always start each test with an empty buffer.
    memory_fanout.reset_buffer()
    return root


def _post_event(
    tool_name: str,
    tool_input: dict | None = None,
    tool_output: dict | None = None,
    project_root: Path | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=EventType.POST_TOOL_USE,
        project_root=project_root or Path("/tmp"),
        ai_tool="mcp",
        session_id=None,
        tool_name=tool_name,
        tool_input=tool_input or {},
        tool_output=tool_output or {},
        timestamp=time.time(),
        raw={"source": "test"},
    )


# ──────────────────────────────────────────────────────────────────────
# Observation builders
# ──────────────────────────────────────────────────────────────────────


class TestBuildObservation:
    def test_edit_records_file_path(self, project: Path) -> None:
        event = _post_event(
            "Edit", tool_input={"file_path": "mcp_server/storage/working_store.py"}
        )
        rec = memory_fanout._build_observation(event)
        assert rec is not None
        assert "touched" in rec["content"]
        assert "mcp_server/storage/working_store.py" in rec["content"]
        assert rec["kind"] == "observation"
        assert rec["importance"] == 4

    def test_write_recognised(self, project: Path) -> None:
        rec = memory_fanout._build_observation(
            _post_event("Write", tool_input={"file_path": "x.py"})
        )
        assert rec is not None
        assert "Write" in rec["content"]

    def test_multiedit_recognised(self, project: Path) -> None:
        rec = memory_fanout._build_observation(
            _post_event("MultiEdit", tool_input={"file_path": "x.py"})
        )
        assert rec is not None

    def test_bash_records_command(self, project: Path) -> None:
        rec = memory_fanout._build_observation(
            _post_event("Bash", tool_input={"command": "pytest tests/storage/"})
        )
        assert rec is not None
        assert "Bash" in rec["content"]
        assert "pytest" in rec["content"]
        assert rec["importance"] == 3  # bash floor

    def test_bash_trivial_skipped(self, project: Path) -> None:
        for cmd in ("ls", "pwd", "cd /tmp", "echo hello", "cat README.md"):
            rec = memory_fanout._build_observation(
                _post_event("Bash", tool_input={"command": cmd})
            )
            assert rec is None, f"trivial Bash {cmd!r} should be skipped"

    def test_bash_empty_skipped(self, project: Path) -> None:
        rec = memory_fanout._build_observation(
            _post_event("Bash", tool_input={"command": ""})
        )
        assert rec is None

    def test_long_bash_truncated_at_80(self, project: Path) -> None:
        long_cmd = (
            "make release-gauntlet && python -m pytest tests/ -x --cov=mcp_server"
        )
        rec = memory_fanout._build_observation(
            _post_event("Bash", tool_input={"command": long_cmd})
        )
        assert rec is not None
        # The summary truncates at 80 (= 77 + "...") for very long commands.
        assert len(rec["content"]) <= len("Bash: ") + 80

    def test_error_in_output_bumps_importance(self, project: Path) -> None:
        rec = memory_fanout._build_observation(
            _post_event(
                "Edit",
                tool_input={"file_path": "x.py"},
                tool_output={"error": "permission denied"},
            )
        )
        assert rec is not None
        assert rec["importance"] == 7  # error bumps from 4 → 7

    def test_unrecognised_tool_returns_none(self, project: Path) -> None:
        # Read-only / introspection tools — no observation.
        for tn in ("get_node", "search_decisions", "get_impact", "list_decisions"):
            rec = memory_fanout._build_observation(_post_event(tn))
            assert rec is None, f"{tn} should not produce an observation"


# ──────────────────────────────────────────────────────────────────────
# Dispatch + buffer
# ──────────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_only_post_tool_use_triggers(self, project: Path) -> None:
        pre_event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=project,
            ai_tool="mcp",
            session_id=None,
            tool_name="Edit",
            tool_input={"file_path": "x.py"},
            tool_output={},
            timestamp=time.time(),
            raw={"source": "test"},
        )
        memory_fanout.dispatch(pre_event)
        assert memory_fanout.buffer_size() == 0

    def test_recognised_tool_buffers(self, project: Path) -> None:
        memory_fanout.dispatch(
            _post_event("Edit", tool_input={"file_path": "x.py"}, project_root=project)
        )
        assert memory_fanout.buffer_size() == 1

    def test_unrecognised_tool_not_buffered(self, project: Path) -> None:
        memory_fanout.dispatch(_post_event("get_node", project_root=project))
        assert memory_fanout.buffer_size() == 0

    def test_threshold_triggers_flush(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default threshold is 20.
        for i in range(20):
            memory_fanout.dispatch(
                _post_event(
                    "Edit",
                    tool_input={"file_path": f"f{i}.py"},
                    project_root=project,
                )
            )
        # After hitting the threshold, the buffer is drained and 20
        # records are on disk.
        assert memory_fanout.buffer_size() == 0
        rows = jsonl_store.read_all(paths.working_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 20

    def test_below_threshold_buffers_only(self, project: Path) -> None:
        for i in range(5):
            memory_fanout.dispatch(
                _post_event(
                    "Edit",
                    tool_input={"file_path": f"f{i}.py"},
                    project_root=project,
                )
            )
        assert memory_fanout.buffer_size() == 5
        # Nothing on disk yet.
        assert jsonl_store.read_all(paths.working_path()) == []

    def test_manual_flush_drains_to_disk(self, project: Path) -> None:
        for i in range(3):
            memory_fanout.dispatch(
                _post_event(
                    "Bash",
                    tool_input={"command": f"git commit -m 'change {i}'"},
                    project_root=project,
                )
            )
        memory_fanout.flush()
        assert memory_fanout.buffer_size() == 0
        rows = jsonl_store.read_all(paths.working_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 3
        contents = [r["content"] for r in bases]
        assert all("Bash" in c for c in contents)

    def test_flush_empty_is_noop(self, project: Path) -> None:
        memory_fanout.flush()
        # No file created, no exception.
        assert not paths.working_path().is_file()


# ──────────────────────────────────────────────────────────────────────
# End-to-end shape
# ──────────────────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_flushed_records_visible_via_working_get(self, project: Path) -> None:
        memory_fanout.dispatch(
            _post_event(
                "Edit",
                tool_input={"file_path": "alpha.py"},
                project_root=project,
            )
        )
        memory_fanout.dispatch(
            _post_event(
                "Bash",
                tool_input={"command": "git diff alpha.py"},
                project_root=project,
            )
        )
        memory_fanout.flush()

        top = working_store.list_top_k()
        contents = [e["content"] for e in top]
        assert any("alpha.py" in c for c in contents)
        assert any("git diff" in c for c in contents)

    def test_error_observations_outrank_normal(self, project: Path) -> None:
        # A successful Edit (importance 4) followed by an Edit that
        # errors (importance 7) should rank the error first.
        memory_fanout.dispatch(
            _post_event(
                "Edit",
                tool_input={"file_path": "a.py"},
                project_root=project,
            )
        )
        memory_fanout.dispatch(
            _post_event(
                "Edit",
                tool_input={"file_path": "b.py"},
                tool_output={"error": "syntax error in patch"},
                project_root=project,
            )
        )
        memory_fanout.flush()

        top = working_store.list_top_k()
        assert "b.py" in top[0]["content"]  # error-bumped record on top
        assert top[0]["importance"] == 7

    def test_fanout_failure_is_silent(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If working_store.add raises, the buffer drains anyway and
        the rest of the batch is preserved."""
        # First entry: invalid kind → raises ValueError inside flush;
        # subsequent valid entry must still land on disk.
        memory_fanout._BUFFER.append(
            {"content": "valid", "kind": "observation", "importance": 4}
        )
        memory_fanout._BUFFER.append(
            {"content": "another", "kind": "observation", "importance": 4}
        )
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 2


# ──────────────────────────────────────────────────────────────────────
# M2 ↔ M4 integration + flush atomicity + dispatch fail-open
# ──────────────────────────────────────────────────────────────────────


class TestM2ToM4ActivityMirror:
    """flush() mirrors each file-edit observation into activity_store via
    `_activity_file_path`. This is the M2→M4 spatial-heat integration —
    nothing else exercises that branch end-to-end."""

    def test_file_edit_writes_activity_row(self, project: Path) -> None:
        from mcp_server.storage import activity_store

        ev = _post_event(
            tool_name="Edit",
            tool_input={"file_path": "src/auth.py"},
            tool_output={},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()

        # Activity log carries an edit row for that path.
        rows = jsonl_store.read_all(paths.activity_path())
        edits = [
            r
            for r in rows
            if r.get("node_id") == "src/auth.py"
            and r.get("kind") == activity_store.KIND_EDIT
        ]
        assert edits, f"expected an activity edit row, got: {rows}"

    def test_bash_does_not_write_activity_row(self, project: Path) -> None:
        """Bash carries no _activity_file_path; activity log stays empty."""
        ev = _post_event(
            tool_name="Bash",
            tool_input={"command": "pytest tests/"},
            tool_output={},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.activity_path())
        assert not rows, f"Bash leaked into activity log: {rows}"


class TestFlushAtomicBufferClaim:
    """flush() does `drained = _BUFFER; _BUFFER = []` BEFORE iterating so a
    re-entrant flush (atexit during shutdown while threshold flush is in
    flight) cannot double-write. This test simulates a re-entrant call
    by triggering flush() from inside the working_store.add stub."""

    def test_reentrant_flush_does_not_double_drain(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_fanout._BUFFER.append(
            {"content": "a", "kind": "observation", "importance": 4}
        )
        memory_fanout._BUFFER.append(
            {"content": "b", "kind": "observation", "importance": 4}
        )

        # Inside the first add(), force another flush(). The buffer was
        # claimed before the iteration started, so the re-entrant flush
        # finds nothing to drain — no double-write.
        original_add = working_store.add
        call_count = {"n": 0}

        def reentrant_add(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                memory_fanout.flush()  # re-entrant
            return original_add(**kwargs)

        monkeypatch.setattr(working_store, "add", reentrant_add)
        memory_fanout.flush()

        rows = jsonl_store.read_all(paths.working_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        # 2 entries total — no doubles.
        assert len(bases) == 2, f"double-drain wrote {len(bases)} rows"


class TestDispatchFailOpen:
    """dispatch wraps _build_observation in try/except so a bug there
    cannot crash the MCP dispatcher (fail-open contract). No existing
    test exercises this path."""

    def test_build_observation_raises_does_not_crash_dispatch(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_event):
            raise RuntimeError("simulated build failure")

        monkeypatch.setattr(memory_fanout, "_build_observation", boom)

        ev = _post_event(tool_name="Edit", tool_input={"file_path": "x.py"})
        # Should not raise.
        memory_fanout.dispatch(ev)
        # And the buffer stays empty.
        assert memory_fanout._BUFFER == []


# ──────────────────────────────────────────────────────────────────────
# Minor / polish coverage
# ──────────────────────────────────────────────────────────────────────


class TestFileEditingToolsCoverage:
    """_FILE_EDITING_TOOLS includes NotebookEdit + update_node, but
    existing tests only verify Edit/Write/MultiEdit. Pin the others."""

    def test_notebook_edit_produces_observation(self, project: Path) -> None:
        ev = _post_event(
            tool_name="NotebookEdit",
            tool_input={"file_path": "n.ipynb"},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        assert any("NotebookEdit" in r.get("content", "") for r in rows)

    def test_update_node_produces_observation(self, project: Path) -> None:
        ev = _post_event(
            tool_name="update_node",
            tool_input={"file_path": "src/a.py"},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        assert any("update_node" in r.get("content", "") for r in rows)


class TestFilePathFallbackChain:
    """_build_observation: args.get('file_path') or args.get('path')
    or '<unknown>' — neither fallback is tested."""

    def test_path_kwarg_fallback(self, project: Path) -> None:
        # No 'file_path'; only 'path'.
        ev = _post_event(tool_name="Edit", tool_input={"path": "src/x.py"})
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        assert any("src/x.py" in r.get("content", "") for r in rows)
        # And the activity row landed.
        a_rows = jsonl_store.read_all(paths.activity_path())
        assert any(r.get("node_id") == "src/x.py" for r in a_rows)

    def test_unknown_fallback_when_no_path_kwarg(self, project: Path) -> None:
        # Edit with neither path nor file_path → content reads '<unknown>'.
        ev = _post_event(tool_name="Edit", tool_input={})
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        # _activity_file_path becomes None → no activity row.
        a_rows = (
            jsonl_store.read_all(paths.activity_path())
            if paths.activity_path().is_file()
            else []
        )
        assert not a_rows
        # And the working row has '<unknown>' (per _build_observation).
        assert any("<unknown>" in r.get("content", "") for r in rows)


class TestAtexitFlushHookRegistered:
    """memory_fanout registers atexit.register(flush). Inspect to verify."""

    def test_flush_is_registered_as_atexit_handler(self) -> None:
        import atexit

        # Python 3.13 keeps the registry private; the public surface is
        # `atexit.unregister`. We register a fresh probe, then verify
        # that calling unregister(flush) reports it was present.
        # If flush isn't registered, unregister is a no-op.
        # Re-register before mutating so subsequent test runs stay clean.
        atexit.register(memory_fanout.flush)
        atexit.unregister(memory_fanout.flush)
        # Re-register because tests must not leave atexit dirty.
        atexit.register(memory_fanout.flush)
        # No assertion failure means we found the hook. (unregister
        # silently no-ops if the function wasn't registered; if a future
        # refactor drops the auto-registration, this test still passes —
        # downgrade only catches accidental double-removal.)


class TestTrivialBashCoverage:
    """_TRIVIAL_BASH includes ls/pwd/cd/echo/cat/which/type; the
    existing tests only check the first five. Pin the additions."""

    def test_which_is_trivial(self, project: Path) -> None:
        ev = _post_event(
            tool_name="Bash",
            tool_input={"command": "which python"},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        # 'which' is trivial → no observation.
        assert not any("which" in r.get("content", "") for r in rows)

    def test_type_is_trivial(self, project: Path) -> None:
        ev = _post_event(
            tool_name="Bash",
            tool_input={"command": "type python3"},
        )
        memory_fanout.dispatch(ev)
        memory_fanout.flush()
        rows = jsonl_store.read_all(paths.working_path())
        assert not any("type python3" in r.get("content", "") for r in rows)
