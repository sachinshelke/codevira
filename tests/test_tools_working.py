"""
Tests for mcp_server.tools.working — v3.1.0 M2 Phase 2 MCP tools.

Verifies the four-tool surface (working_add, working_get,
working_promote, get_working_context) against the contract documented
in mcp_server/tools/working.py. Storage-layer correctness is tested
separately in tests/storage/test_working_store.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import working_store
from mcp_server.tools import working


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# working_add
# ──────────────────────────────────────────────────────────────────────


class TestWorkingAdd:
    def test_basic_returns_entry_id(self, project: Path) -> None:
        r = working.working_add("Touched paths.py")
        assert r["recorded"] is True
        assert r["entry_id"].startswith("W")
        assert r["kind"] == "observation"
        assert "hint" in r

    def test_goal_kind(self, project: Path) -> None:
        r = working.working_add("Implement M2 working memory", kind="goal")
        assert r["recorded"] is True
        assert r["kind"] == "goal"

    def test_invalid_kind_returns_structured_error(self, project: Path) -> None:
        r = working.working_add("oops", kind="hypothesis")
        assert r["recorded"] is False
        assert "kind" in r["error"]

    def test_invalid_importance_returns_structured_error(self, project: Path) -> None:
        r = working.working_add("content", importance=11)
        assert r["recorded"] is False
        assert "importance" in r["error"]

    def test_oversize_content_returns_structured_error(self, project: Path) -> None:
        r = working.working_add("x" * 4000)
        assert r["recorded"] is False
        assert "2048 byte cap" in r["error"]


# ──────────────────────────────────────────────────────────────────────
# working_get / get_working_context
# ──────────────────────────────────────────────────────────────────────


class TestWorkingGet:
    def test_empty_store(self, project: Path) -> None:
        r = working.working_get()
        assert r["entries"] == []
        assert r["count"] == 0

    def test_returns_entries_ranked(self, project: Path) -> None:
        working.working_add("low", importance=2)
        working.working_add("high", importance=9)
        working.working_add("medium", importance=5)
        r = working.working_get(top_k=3)
        assert r["count"] == 3
        assert [e["content"] for e in r["entries"]] == ["high", "medium", "low"]

    def test_filters_by_kind(self, project: Path) -> None:
        working.working_add("obs a", kind="observation")
        working.working_add("goal b", kind="goal")
        r = working.working_get(kind="goal")
        assert r["count"] == 1
        assert r["entries"][0]["kind"] == "goal"

    def test_response_shape(self, project: Path) -> None:
        working.working_add("x")
        r = working.working_get()
        e = r["entries"][0]
        assert set(e.keys()) >= {
            "entry_id",
            "kind",
            "content",
            "importance",
            "confidence",
            "links",
            "ts",
            "session_id",
        }


class TestGetWorkingContext:
    def test_empty_returns_placeholder(self, project: Path) -> None:
        r = working.get_working_context()
        assert r["count"] == 0
        assert "empty" in r["markdown"].lower()

    def test_renders_markdown_with_prefix_per_kind(self, project: Path) -> None:
        working.working_add("looked at retry.py", kind="observation")
        working.working_add("redesign retry", kind="goal")
        r = working.get_working_context(top_k=5)
        # observation uses • bullet; goal uses → arrow.
        assert "•" in r["markdown"]
        assert "→" in r["markdown"]
        assert "Working memory" in r["markdown"]

    def test_long_content_truncated_in_markdown(self, project: Path) -> None:
        long_content = "x" * 500
        working.working_add(long_content)
        r = working.get_working_context()
        # Truncated at 120 chars + ellipsis in the markdown line.
        assert "..." in r["markdown"]
        # The structured `entries` view keeps full content.
        assert r["entries"][0]["content"] == long_content


# ──────────────────────────────────────────────────────────────────────
# working_promote
# ──────────────────────────────────────────────────────────────────────


class TestWorkingPromote:
    def test_invalid_target_rejected(self, project: Path) -> None:
        wid = working_store.add("x")
        r = working.working_promote(wid, to="filesystem")  # type: ignore[arg-type]
        assert r["promoted"] is False
        assert "'to' must be one of" in r["error"]

    def test_missing_entry_rejected(self, project: Path) -> None:
        r = working.working_promote("W999999", to="decision")
        assert r["promoted"] is False
        assert "not found" in r["error"]

    def test_skill_returns_deferred(self, project: Path) -> None:
        wid = working_store.add("Goal: design retry workflow", kind="goal")
        r = working.working_promote(wid, to="skill")
        assert r["promoted"] is False
        assert r["deferred"] is True
        assert r["milestone"] == "M3"

    def test_playbook_returns_deferred(self, project: Path) -> None:
        wid = working_store.add("design a debug recipe", kind="observation")
        r = working.working_promote(wid, to="playbook")
        assert r["promoted"] is False
        assert r["deferred"] is True

    def test_promote_to_decision_full_path(self, project: Path) -> None:
        wid = working_store.add(
            "Use rate limiting on /auth endpoints",
            kind="observation",
            importance=8,
        )
        r = working.working_promote(
            wid,
            to="decision",
            file_path="auth/middleware.py",
            do_not_revert=True,
            tags=["auth", "security"],
        )
        assert r["promoted"] is True
        assert r["target_id"].startswith("D")
        # Source entry is now tombstoned — working_get no longer returns it.
        live = working.working_get()
        assert all(e["entry_id"] != wid for e in live["entries"])

    def test_promote_already_tombstoned_rejected(self, project: Path) -> None:
        wid = working_store.add("x", kind="observation")
        # Manually tombstone via eviction first.
        working_store.mark_evicted(wid)
        r = working.working_promote(wid, to="decision")
        assert r["promoted"] is False
        assert "tombstoned" in r["error"]

    def test_promote_with_conflict_returns_warning(self, project: Path) -> None:
        # Seed a protected decision; promotion of a near-duplicate must
        # surface the conflict instead of silently writing.
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing",
            do_not_revert=True,
        )
        wid = working_store.add("Use bcrypt for password hashing", kind="observation")
        r = working.working_promote(wid, to="decision")
        assert r["promoted"] is False
        assert "_conflict_warning" in r

    def test_promote_with_force_overrides_conflict(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing",
            do_not_revert=True,
        )
        wid = working_store.add("Use bcrypt for password hashing", kind="observation")
        r = working.working_promote(wid, to="decision", force=True)
        assert r["promoted"] is True
        assert r["target_id"].startswith("D")

    def test_goal_promotion_surfaces_intent_note(self, project: Path) -> None:
        wid = working_store.add("Add OAuth flow", kind="goal", importance=7)
        r = working.working_promote(wid, to="decision")
        assert r["promoted"] is True
        assert "_intent_note" in r

    def test_promotion_carries_links_into_context(self, project: Path) -> None:
        wid = working_store.add(
            "Followup on D000001", kind="observation", links=["D000001"]
        )
        r = working.working_promote(wid, to="decision")
        assert r["promoted"] is True
        # The new decision's context should mention the working entry id
        # and its links — useful audit metadata.
        from mcp_server.storage import decisions_store

        new = decisions_store.get(r["target_id"])
        assert new is not None
        assert wid in (new.get("context") or "")
        assert "D000001" in (new.get("context") or "")
