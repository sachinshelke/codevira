"""
Tests for mcp_server.storage.activity_store — v3.1.0 M4 Phase 1.

Coverage:
  - add() input validation + schema (A-id, _schema_v: 1, origin)
  - list_recent: filtering by kind / node_id / since
  - list_top_k_files: weighted ranking + custom weights
  - visit_count_30d: rolling-window counter for spatial_nearby
  - compact: retention drop
  - memory_fanout integration: file-edit observations also write
    activity rows; Bash observations don't
  - decisions_store integration: record() with file_path emits
    a decision_ref activity row
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import activity_store, jsonl_store, paths


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# add() — schema + validation
# ──────────────────────────────────────────────────────────────────────


class TestAdd:
    def test_basic_returns_a_id(self, project: Path) -> None:
        aid = activity_store.add("src/foo.py", kind="edit")
        assert aid.startswith("A")

    def test_schema_v_and_origin_stamped(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        activity_store.add("src/foo.py", kind="edit")
        rec = jsonl_store.read_all(paths.activity_path())[0]
        assert rec["_schema_v"] == 1
        assert rec["origin"]["ide"] == "cursor"
        assert rec["node_id"] == "src/foo.py"
        assert rec["kind"] == "edit"

    def test_empty_node_id_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="node_id"):
            activity_store.add("   ", kind="edit")

    def test_invalid_kind_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="kind"):
            activity_store.add("src/foo.py", kind="visit")  # reserved for v3.2

    def test_decision_ref_kind_accepted(self, project: Path) -> None:
        activity_store.add("src/auth.py", kind=activity_store.KIND_DECISION_REF)
        rec = jsonl_store.read_all(paths.activity_path())[0]
        assert rec["kind"] == "decision_ref"


# ──────────────────────────────────────────────────────────────────────
# list_recent
# ──────────────────────────────────────────────────────────────────────


class TestListRecent:
    def test_newest_first(self, project: Path) -> None:
        activity_store.add("a.py", kind="edit")
        time.sleep(0.005)
        activity_store.add("b.py", kind="edit")
        time.sleep(0.005)
        activity_store.add("c.py", kind="edit")
        recent = activity_store.list_recent(limit=3)
        assert [r["node_id"] for r in recent] == ["c.py", "b.py", "a.py"]

    def test_kind_filter(self, project: Path) -> None:
        activity_store.add("a.py", kind="edit")
        activity_store.add("b.py", kind="decision_ref")
        only_dec = activity_store.list_recent(kind="decision_ref")
        assert {r["node_id"] for r in only_dec} == {"b.py"}

    def test_node_filter(self, project: Path) -> None:
        activity_store.add("a.py", kind="edit")
        activity_store.add("b.py", kind="edit")
        only_a = activity_store.list_recent(node_id="a.py")
        assert {r["node_id"] for r in only_a} == {"a.py"}

    def test_since_filter(self, project: Path) -> None:
        # Inject a stale row directly.
        old_ts = (datetime(2020, 1, 1, tzinfo=timezone.utc)).isoformat()
        jsonl_store.append(
            paths.activity_path(),
            {
                "id": "A000001",
                "ts": old_ts,
                "node_id": "stale.py",
                "kind": "edit",
                "_schema_v": 1,
            },
        )
        activity_store.add("fresh.py", kind="edit")
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        recent = activity_store.list_recent(since=cutoff)
        assert {r["node_id"] for r in recent} == {"fresh.py"}


# ──────────────────────────────────────────────────────────────────────
# list_top_k_files (heatmap ranking)
# ──────────────────────────────────────────────────────────────────────


class TestListTopKFiles:
    def test_score_weights_edit_and_decision_ref(self, project: Path) -> None:
        # File A: 3 edits → score = 3.0
        for _ in range(3):
            activity_store.add("a.py", kind="edit")
        # File B: 1 edit + 1 decision_ref → score = 1 + 2 = 3.0
        activity_store.add("b.py", kind="edit")
        activity_store.add("b.py", kind="decision_ref")
        # File C: 1 edit → score = 1.0
        activity_store.add("c.py", kind="edit")
        ranked = activity_store.list_top_k_files(top_k=10)
        scores = {r["node_id"]: r["score"] for r in ranked}
        assert scores["a.py"] == 3.0
        assert scores["b.py"] == 3.0
        assert scores["c.py"] == 1.0

    def test_custom_weights(self, project: Path) -> None:
        activity_store.add("a.py", kind="edit")
        activity_store.add("a.py", kind="decision_ref")
        # Override: edits weigh 5, decisions weigh 0.5 — flip the
        # default emphasis.
        ranked = activity_store.list_top_k_files(
            weights={"edit": 5.0, "decision_ref": 0.5}
        )
        assert ranked[0]["score"] == 5.5

    def test_top_k_caps_output(self, project: Path) -> None:
        for i in range(20):
            activity_store.add(f"f{i}.py", kind="edit")
        ranked = activity_store.list_top_k_files(top_k=3)
        assert len(ranked) == 3

    def test_empty_store_returns_empty(self, project: Path) -> None:
        assert activity_store.list_top_k_files() == []


# ──────────────────────────────────────────────────────────────────────
# visit_count_30d
# ──────────────────────────────────────────────────────────────────────


class TestVisitCount30d:
    def test_counts_within_window(self, project: Path) -> None:
        for _ in range(3):
            activity_store.add("a.py", kind="edit")
        activity_store.add("a.py", kind="decision_ref")
        # Total = 4.
        assert activity_store.visit_count_30d("a.py") == 4

    def test_excludes_outside_window(self, project: Path) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=45)
        jsonl_store.append(
            paths.activity_path(),
            {
                "id": "A000001",
                "ts": old.isoformat(),
                "node_id": "a.py",
                "kind": "edit",
                "_schema_v": 1,
            },
        )
        # Fresh row.
        activity_store.add("a.py", kind="edit")
        assert activity_store.visit_count_30d("a.py") == 1

    def test_other_node_ids_not_counted(self, project: Path) -> None:
        activity_store.add("a.py", kind="edit")
        activity_store.add("b.py", kind="edit")
        assert activity_store.visit_count_30d("a.py") == 1


# ──────────────────────────────────────────────────────────────────────
# compact
# ──────────────────────────────────────────────────────────────────────


class TestCompact:
    def test_drops_old_rows(self, project: Path) -> None:
        old_ts = (datetime(2020, 1, 1, tzinfo=timezone.utc)).isoformat()
        jsonl_store.append(
            paths.activity_path(),
            {
                "id": "A000001",
                "ts": old_ts,
                "node_id": "stale.py",
                "kind": "edit",
                "_schema_v": 1,
            },
        )
        activity_store.add("fresh.py", kind="edit")

        dropped = activity_store.compact(retention_days=90)
        assert dropped == 1
        remaining = jsonl_store.read_all(paths.activity_path())
        assert [r["node_id"] for r in remaining] == ["fresh.py"]

    def test_compact_missing_file_returns_zero(self, project: Path) -> None:
        # No activity.jsonl exists yet.
        assert activity_store.compact() == 0


# ──────────────────────────────────────────────────────────────────────
# memory_fanout integration
# ──────────────────────────────────────────────────────────────────────


class TestMemoryFanoutIntegration:
    """v3.1.0 M4: PostToolUse Edit events should produce BOTH a working
    observation AND an activity row. Bash should produce only the
    working observation."""

    def _post_event(self, tool_name, tool_input=None, project_root=None):
        from mcp_server.engine.events import EventType, HookEvent

        return HookEvent(
            event_type=EventType.POST_TOOL_USE,
            project_root=project_root or Path("/tmp"),
            ai_tool="mcp",
            session_id=None,
            tool_name=tool_name,
            tool_input=tool_input or {},
            tool_output={},
            timestamp=time.time(),
            raw={"source": "test"},
        )

    def test_edit_produces_both_working_and_activity(self, project: Path) -> None:
        from mcp_server.engine import memory_fanout

        memory_fanout.reset_buffer()
        memory_fanout.dispatch(
            self._post_event(
                "Edit",
                tool_input={"file_path": "src/auth.py"},
                project_root=project,
            )
        )
        memory_fanout.flush()

        # Working observation landed.
        working_rows = jsonl_store.read_all(paths.working_path())
        assert len(working_rows) == 1
        assert "src/auth.py" in working_rows[0]["content"]

        # Activity row also landed with kind=edit.
        act_rows = jsonl_store.read_all(paths.activity_path())
        bases = [r for r in act_rows if not r.get("_amendment_to_id")]
        assert len(bases) == 1
        assert bases[0]["node_id"] == "src/auth.py"
        assert bases[0]["kind"] == "edit"

    def test_bash_does_not_write_activity(self, project: Path) -> None:
        from mcp_server.engine import memory_fanout

        memory_fanout.reset_buffer()
        memory_fanout.dispatch(
            self._post_event(
                "Bash",
                tool_input={"command": "git status"},
                project_root=project,
            )
        )
        memory_fanout.flush()
        # Working observation exists, activity does not.
        assert jsonl_store.read_all(paths.working_path())
        # activity.jsonl shouldn't exist (no rows written).
        assert not paths.activity_path().is_file()

    def test_unknown_file_path_skips_activity(self, project: Path) -> None:
        """Edit with no file_path arg still produces a working
        observation but no activity row (we can't attribute the
        attention to a specific node)."""
        from mcp_server.engine import memory_fanout

        memory_fanout.reset_buffer()
        memory_fanout.dispatch(self._post_event("Edit", project_root=project))
        memory_fanout.flush()
        # No activity row.
        assert not paths.activity_path().is_file()


# ──────────────────────────────────────────────────────────────────────
# decisions_store integration
# ──────────────────────────────────────────────────────────────────────


class TestDecisionsStoreIntegration:
    """v3.1.0 M4: record() with file_path emits a decision_ref
    activity row alongside the canonical JSONL write."""

    def test_record_with_file_path_emits_decision_ref(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing",
            file_path="auth/middleware.py",
        )
        rows = jsonl_store.read_all(paths.activity_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 1
        assert bases[0]["kind"] == "decision_ref"
        assert bases[0]["node_id"] == "auth/middleware.py"

    def test_record_without_file_path_skips_activity(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(decision="Project-wide doctrine")
        # No file_path → no activity row.
        assert not paths.activity_path().is_file()
