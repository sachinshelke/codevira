"""
Tests for mcp_server.storage.working_store — v3.1.0 M2 Phase 1.

Coverage:
  - add() input validation (kind, content size, importance, confidence)
  - schema fields (W-id, origin, _schema_v: 1)
  - list_top_k decay scoring + tie-breaker
  - mark_evicted / mark_promoted tombstone via amendment overlay
  - compact() drops tombstoned rows AND their amendment rows
  - commit_session copies live entries to working_archived
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import jsonl_store, paths, working_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


class TestAdd:
    _ID_PATTERN = re.compile(r"^W\d{6}$")

    def test_basic_add_returns_w_id(self, project: Path) -> None:
        wid = working_store.add("Touched mcp_server/storage/paths.py")
        assert self._ID_PATTERN.match(wid), wid

    def test_record_has_schema_v_and_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        working_store.add("Goal: implement working memory", kind="goal")

        rows = jsonl_store.read_all(paths.working_path())
        assert len(rows) == 1
        rec = rows[0]
        assert rec["_schema_v"] == 1
        assert rec["origin"]["ide"] == "cursor"
        assert rec["kind"] == "goal"

    def test_default_session_id_unique(self, project: Path) -> None:
        wid1 = working_store.add("a")
        wid2 = working_store.add("b")
        assert wid1 != wid2
        rows = jsonl_store.read_all(paths.working_path())
        sids = {r["session_id"] for r in rows}
        # Per the v3.0.1 session-id helper: each unattributed call gets its own slug.
        assert len(sids) == 2

    def test_invalid_kind_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="kind"):
            working_store.add("content", kind="hypothesis")

    def test_empty_content_rejected(self, project: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            working_store.add("")

    def test_content_size_cap_2kb(self, project: Path) -> None:
        # Exactly 2 KB ASCII → 2048 bytes → OK
        working_store.add("x" * 2048)
        # One byte over → reject
        with pytest.raises(ValueError, match="2048 byte cap"):
            working_store.add("x" * 2049)

    def test_importance_range(self, project: Path) -> None:
        working_store.add("ok", importance=1)
        working_store.add("ok", importance=10)
        for bad in (0, 11, -1, 5.5):
            with pytest.raises(ValueError, match="importance"):
                working_store.add("ok", importance=bad)  # type: ignore[arg-type]

    def test_confidence_range(self, project: Path) -> None:
        working_store.add("ok", confidence=0.0)
        working_store.add("ok", confidence=1.0)
        for bad in (-0.01, 1.01, 2.0):
            with pytest.raises(ValueError, match="confidence"):
                working_store.add("ok", confidence=bad)

    def test_links_preserved(self, project: Path) -> None:
        wid = working_store.add("touching D000007", links=["D000007", "D000008"])
        rec = working_store.get(wid)
        assert rec is not None
        assert rec["links"] == ["D000007", "D000008"]


class TestDecayScoring:
    """The lazy-on-read scoring contract per the plan:

    score = importance × exp(-Δt_hours / τ) + 0.5 × access_count, τ=6h
    """

    def test_formula_matches_plan(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        # importance 8, 3h old, access_count 2 → 8 * exp(-0.5) + 1
        rec = {
            "ts": (now - timedelta(hours=3)).isoformat(),
            "importance": 8,
            "access_count": 2,
        }
        expected = 8 * math.exp(-0.5) + 1.0
        actual = working_store._compute_score(rec, now=now)
        assert abs(actual - expected) < 1e-6, (actual, expected)

    def test_fresh_entry_close_to_importance(self) -> None:
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        rec = {"ts": now.isoformat(), "importance": 7, "access_count": 0}
        # Δt=0 → exp(0)=1 → score = importance
        assert abs(working_store._compute_score(rec, now=now) - 7.0) < 1e-6

    def test_malformed_ts_treated_as_now(self) -> None:
        """No penalty for bad metadata — return importance + access term."""
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        rec = {"ts": "not-an-iso-string", "importance": 5, "access_count": 4}
        score = working_store._compute_score(rec, now=now)
        assert abs(score - (5 + 2.0)) < 1e-6


class TestListTopK:
    def test_returns_highest_scoring_entries_first(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin clock so the test is deterministic.
        fixed = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)

        # Older but more important should rank above newer-but-less-important
        # within 6-hour decay window.
        working_store.add("low importance", importance=2)
        working_store.add("high importance", importance=9)
        working_store.add("medium importance", importance=5)

        top = working_store.list_top_k(top_k=3, now=fixed)
        contents = [r["content"] for r in top]
        # All three written ~ "now"; ranking by importance desc.
        assert contents == [
            "high importance",
            "medium importance",
            "low importance",
        ]

    def test_filters_by_kind(self, project: Path) -> None:
        working_store.add("obs A", kind="observation")
        working_store.add("goal B", kind="goal")
        working_store.add("obs C", kind="observation")
        only_goals = working_store.list_top_k(kind="goal")
        assert [r["content"] for r in only_goals] == ["goal B"]

    def test_filters_by_session_id(self, project: Path) -> None:
        working_store.add("alpha", session_id="s1")
        working_store.add("beta", session_id="s2")
        working_store.add("gamma", session_id="s1")
        s1 = working_store.list_top_k(session_id="s1")
        assert {r["content"] for r in s1} == {"alpha", "gamma"}

    def test_evicted_entries_excluded(self, project: Path) -> None:
        wid_drop = working_store.add("drop me", importance=10)
        working_store.add("keep me", importance=3)
        working_store.mark_evicted(wid_drop)
        top = working_store.list_top_k()
        assert [r["content"] for r in top] == ["keep me"]

    def test_promoted_entries_excluded(self, project: Path) -> None:
        wid = working_store.add("goal: design retry", kind="goal", importance=9)
        working_store.add("obs: looked at retry.py", importance=4)
        working_store.mark_promoted(wid, target_id="D000099")
        top = working_store.list_top_k()
        assert [r["content"] for r in top] == ["obs: looked at retry.py"]

    def test_top_k_caps_output(self, project: Path) -> None:
        for i in range(10):
            working_store.add(f"entry {i}", importance=(i % 10) + 1)
        assert len(working_store.list_top_k(top_k=3)) == 3

    def test_empty_store_returns_empty(self, project: Path) -> None:
        assert working_store.list_top_k() == []


class TestTombstoneMerging:
    """The amendment-merge contract: mark_evicted / mark_promoted
    append amendment rows; read_merged folds them into the base.
    """

    def test_evicted_amendment_sets_flag_on_merged_record(self, project: Path) -> None:
        wid = working_store.add("temp")
        working_store.mark_evicted(wid, reason="superseded by W000007")
        merged = jsonl_store.read_merged(paths.working_path())
        assert len(merged) == 1
        # _evicted is an underscored field — NOT overlaid onto the base
        # by jsonl_store.read_merged. The presence/absence of _evicted
        # MUST be checked via raw read_all, not merged.
        assert "_evicted" not in merged[0]

        # Raw rows include the amendment with _evicted: True
        raw = jsonl_store.read_all(paths.working_path())
        ams = [r for r in raw if r.get("_amendment_to_id") == wid]
        assert len(ams) == 1
        assert ams[0]["_evicted"] is True
        assert ams[0].get("_evict_reason") == "superseded by W000007"

    def test_list_top_k_skips_via_raw_amendment_scan(self, project: Path) -> None:
        """list_top_k checks the raw merge result for `_evicted` /
        `_promoted_to`. Since those keys are underscored and don't
        overlay onto the base, list_top_k must instead detect
        tombstones via a separate pre-scan of amendment rows.
        """
        # This documents the contract the implementation honors.
        wid = working_store.add("doomed", importance=10)
        working_store.mark_evicted(wid)
        # If list_top_k returned the tombstoned entry, this would fail.
        assert working_store.list_top_k() == []


class TestCompact:
    def test_drops_evicted_and_their_amendments(self, project: Path) -> None:
        keep = working_store.add("keep me")
        drop = working_store.add("drop me")
        working_store.mark_evicted(drop, reason="not useful")

        # Before: 3 rows (2 base + 1 amendment).
        assert len(jsonl_store.read_all(paths.working_path())) == 3

        dropped = working_store.compact()
        # 1 base + 1 amendment removed = 2 dropped rows.
        assert dropped == 2

        remaining = jsonl_store.read_all(paths.working_path())
        assert [r["id"] for r in remaining] == [keep]

    def test_drops_promoted_and_their_amendments(self, project: Path) -> None:
        wid = working_store.add("goal: ship v3.1", kind="goal")
        working_store.mark_promoted(wid, target_id="D000123")
        dropped = working_store.compact()
        assert dropped == 2

    def test_keeps_live_entries(self, project: Path) -> None:
        for i in range(5):
            working_store.add(f"e{i}")
        before = len(jsonl_store.read_all(paths.working_path()))
        dropped = working_store.compact()
        after = len(jsonl_store.read_all(paths.working_path()))
        assert dropped == 0
        assert before == after == 5

    def test_compact_on_missing_file_is_noop(self, tmp_path: Path) -> None:
        # Without project fixture — working_path() will resolve, but
        # the file doesn't exist; compact returns 0.
        assert (
            jsonl_store.compact(
                tmp_path / "missing.jsonl", keep_predicate=lambda r: True
            )
            == 0
        )


class TestCommitSession:
    def test_copies_live_entries_to_archive(self, project: Path) -> None:
        working_store.add("alpha", session_id="my-session")
        working_store.add("beta", session_id="my-session")
        working_store.add("zeta", session_id="other-session")

        res = working_store.commit_session("my-session")
        assert res["committed_count"] == 2
        assert "my-session.jsonl" in res["destination"]

        dest_path = paths.working_archived_path("my-session")
        assert dest_path.is_file()
        archived = jsonl_store.read_all(dest_path)
        assert {r["content"] for r in archived} == {"alpha", "beta"}

    def test_excludes_evicted_entries(self, project: Path) -> None:
        wid_keep = working_store.add("keep", session_id="s")
        wid_drop = working_store.add("drop", session_id="s")
        working_store.mark_evicted(wid_drop)
        res = working_store.commit_session("s")
        assert res["committed_count"] == 1
        archived = jsonl_store.read_all(paths.working_archived_path("s"))
        assert archived[0]["id"] == wid_keep

    def test_no_live_entries_returns_zero(self, project: Path) -> None:
        res = working_store.commit_session("nonexistent")
        assert res["committed_count"] == 0
        assert res["destination"] is None

    def test_idempotent_appends(self, project: Path) -> None:
        working_store.add("x", session_id="s")
        working_store.commit_session("s")
        working_store.commit_session("s")
        archived = jsonl_store.read_all(paths.working_archived_path("s"))
        assert len(archived) == 2  # appended twice; this is the documented behavior
