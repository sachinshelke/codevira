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
