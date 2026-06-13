"""
Tests for mcp_server/tools/learning.py — v3.0.0 adaptive memory.

v3.0.0 surface (2026-05-22 surface-cut audit):
  - get_decision_confidence: scope by file_path or pattern
  - get_session_context:     "catch me up" aggregator
  - record_decision:         capture a new decision (+ tags / do_not_revert)
  - supersede_decision:      retire an old decision, link to its replacement
  - _interpret_confidence:   internal interpretation helper

v2.x tools deleted in the audit (test classes removed from this file):
  - get_preferences, get_learned_rules, get_project_maturity
  - _compute_maturity_score, _maturity_level, _maturity_hint

The maturity scoring + preferences + rules helpers all relied on
preference_signals and learned_rules counts that are always zero in
v3.0.0 (the underlying record paths were removed in the same audit).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import mcp_server.paths as paths
from indexer.sqlite_graph import SQLiteGraph
from mcp_server.tools import learning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_project(tmp_path, monkeypatch) -> tuple[Path, Path, SQLiteGraph]:
    """Create a temp project with a graph database and monkeypatched paths."""
    project_root = tmp_path / "test-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text("project:\n  name: test-learning\n")
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project_root.resolve())

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    return project_root, data_dir, db


def _seed_outcomes(db: SQLiteGraph, outcomes: list[tuple[str, str, str]]) -> None:
    """Seed outcomes. Each tuple is (session_id, file_path, outcome_type)."""
    sessions_seen = set()
    for sess_id, fp, ot in outcomes:
        if sess_id not in sessions_seen:
            db.log_session(
                sess_id,
                f"Session {sess_id}",
                "1",
                [
                    {
                        "file_path": fp,
                        "decision": f"decision for {fp}",
                        "context": "test",
                    }
                ],
            )
            sessions_seen.add(sess_id)
        db.record_outcome(sess_id, fp, ot)


def _seed_full_project(db: SQLiteGraph) -> None:
    """Create a project with sessions, outcomes, and files.

    v3.0.0 round-3 (2026-05-23): the 3 ``log_session`` calls used to
    seed the legacy SQLite ``decisions`` table — invisible to
    get_session_context after the v3.0.0 wire-up to JSONL. Replaced
    with ``decisions_store.record`` so the seed reaches the canonical
    store the read path actually queries. Nodes + outcomes still go
    through SQLiteGraph since those subsystems haven't moved.
    """
    # Files (SQLite graph — still the storage layer for the code graph).
    db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
    db.add_node("file:src/core.py", "file", "core.py", "src/core.py", layer="core")

    # Decisions — write through the v3.0.0 JSONL canonical store.
    from mcp_server.storage import decisions_store as _decisions_store

    _decisions_store.record(
        decision="Use REST",
        file_path="src/api.py",
        context="api design",
        session_id="s1",
    )
    _decisions_store.record(
        decision="Add caching",
        file_path="src/core.py",
        context="perf",
        session_id="s2",
    )
    _decisions_store.record(
        decision="Add validation",
        file_path="src/api.py",
        context="security",
        session_id="s3",
    )

    # Outcomes (SQLite — outcome subsystem hasn't moved to JSONL yet).
    db.record_outcome("s1", "src/api.py", "kept")
    db.record_outcome("s2", "src/core.py", "kept")
    db.record_outcome("s3", "src/api.py", "modified")


# =====================================================================
# _interpret_confidence
# =====================================================================


class TestInterpretConfidence:
    def test_no_data(self):
        result = learning._interpret_confidence(0.0)
        assert "No data" in result

    def test_low_confidence(self):
        result = learning._interpret_confidence(0.3)
        assert "Low confidence" in result

    def test_moderate_confidence(self):
        result = learning._interpret_confidence(0.6)
        assert "Moderate confidence" in result

    def test_high_confidence(self):
        result = learning._interpret_confidence(0.9)
        assert "High confidence" in result

    def test_boundary_zero_point_five(self):
        result = learning._interpret_confidence(0.5)
        assert "Moderate confidence" in result

    def test_boundary_zero_point_eight(self):
        result = learning._interpret_confidence(0.8)
        assert "High confidence" in result

    def test_just_above_zero(self):
        result = learning._interpret_confidence(0.01)
        assert "Low confidence" in result


# =====================================================================
# _compute_maturity_score
# =====================================================================


# v3.0.0 audit cleanup (2026-05-22): TestComputeMaturityScore,
# TestMaturityLevel, TestMaturityHint, TestGetProjectMaturity all
# deleted. The underlying functions (_compute_maturity_score,
# _maturity_level, _maturity_hint, get_project_maturity) were
# removed in the surface-cut audit because two of their three
# inputs (learned_rules count + preference signal count) are
# always zero in v3.0.0 — the MCP tools that wrote those counts
# were deleted in the same audit.


# =====================================================================
# _maturity_level
# =====================================================================


# =====================================================================
# get_decision_confidence (tool-level)
# =====================================================================


class TestGetDecisionConfidence:
    def test_empty_db_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        result = learning.get_decision_confidence()
        assert result["confidence"] == 0.0
        assert "No data" in result["interpretation"]

    def test_file_specific_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(
            db,
            [
                ("s1", "src/api.py", "kept"),
                ("s2", "src/api.py", "kept"),
                ("s3", "src/api.py", "kept"),
            ],
        )
        db.close()
        result = learning.get_decision_confidence(file_path="src/api.py")
        assert result["scope"] == "src/api.py"
        assert result["confidence"] == 1.0
        assert "High confidence" in result["interpretation"]

    def test_pattern_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(
            db,
            [
                ("s1", "src/api.py", "kept"),
                ("s2", "src/core.py", "reverted"),
            ],
        )
        db.close()
        result = learning.get_decision_confidence(pattern="src/")
        assert result["scope"] == "src/"
        assert result["total_decisions"] == 2

    def test_project_wide_confidence(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_outcomes(
            db,
            [
                ("s1", "a.py", "kept"),
                ("s2", "b.py", "modified"),
            ],
        )
        db.close()
        result = learning.get_decision_confidence()
        assert result["scope"] == "project-wide"
        assert result["total_decisions"] == 2


# =====================================================================
# get_preferences (tool-level)
# =====================================================================


# v2.2.0+: TestGetPreferences + TestGetLearnedRules removed.
# The corresponding tools (get_preferences, get_learned_rules, retire_rule)
# were deleted per the 2026-05-22 surface-cut audit — the auto-extracted
# signals produced noise rather than value; nobody read them in real
# sessions. SQLiteGraph still records preferences/rules for back-compat
# but they're not surfaced as MCP tools or via get_session_context.


# =====================================================================
# get_project_maturity (tool-level)
# =====================================================================


# =====================================================================
# get_session_context (tool-level)
# =====================================================================


class TestGetSessionContext:
    def test_session_context_basic(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        # Mock the external imports that session_context pulls in
        mock_roadmap = {
            "current_phase": {
                "name": "Phase 5",
                "next_action": "Do stuff",
                "status": "in_progress",
            },
        }
        mock_changesets = {"open_changesets": [], "count": 0, "warning": None}

        with patch(
            "mcp_server.tools.learning.get_roadmap",
            return_value=mock_roadmap,
            create=True,
        ):
            with patch(
                "mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap
            ):
                with patch(
                    "mcp_server.tools.changesets.list_open_changesets",
                    return_value=mock_changesets,
                ):
                    result = learning.get_session_context()

        assert "recent_sessions" in result
        assert "recent_decisions" in result
        # 2026-05-18 v2.1.2 Item 8: confidence may be replaced by
        # confidence_note when there's no outcome data yet.
        assert "confidence" in result or "confidence_note" in result
        # v2.2.0+: top_signals (preferences + rules) removed per the
        # 2026-05-22 surface-cut audit. No longer asserted.

    def test_session_context_with_roadmap(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        mock_roadmap = {
            "current_phase": {
                "name": "API Refactor",
                "next_action": "Fix routes",
                "status": "in_progress",
            },
        }
        with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
            with patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ):
                result = learning.get_session_context()

        # New shape: current_phase at top level (no more nested `roadmap` key)
        assert result["current_phase"]["name"] == "API Refactor"
        assert result["current_phase"]["next_action"] == "Fix routes"

    def test_session_context_roadmap_failure_graceful(self, tmp_path, monkeypatch):
        """If roadmap import fails, session_context should still work."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch(
            "mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("broken")
        ):
            with patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ):
                result = learning.get_session_context()

        # On failure current_phase stays empty dict
        assert result["current_phase"] == {}

    # v2.2.0+: test_session_context_changesets_failure_graceful removed
    # (the changesets feature was deleted; this test exercised the
    # graceful-fallback for an import path that no longer exists).

    def test_session_context_working_panel_empty(self, tmp_path, monkeypatch):
        """v3.1.0 M2 Phase 3: empty working memory surfaces as
        {entries: [], count: 0} — never crashes the catch-me-up call."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch(
            "mcp_server.tools.roadmap.get_roadmap",
            return_value={"current_phase": {}},
        ):
            result = learning.get_session_context()

        assert "working" in result
        assert result["working"]["entries"] == []
        assert result["working"]["count"] == 0

    def test_session_context_working_panel_populated(self, tmp_path, monkeypatch):
        """v3.1.0 M2 Phase 3: top-3 live entries surface, capped, with
        truncated content (120 chars per plan's token budget)."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        from mcp_server.storage import working_store

        # Seed 5 entries — panel should show top 3 by decay/importance.
        working_store.add("low signal", importance=2)
        working_store.add("medium signal", importance=5)
        working_store.add("high signal", importance=9)
        working_store.add("goal: ship M2", kind="goal", importance=8)
        working_store.add("x" * 200, importance=6)  # truncation check

        with patch(
            "mcp_server.tools.roadmap.get_roadmap",
            return_value={"current_phase": {}},
        ):
            result = learning.get_session_context()

        panel = result["working"]
        assert panel["count"] == 3, panel
        # Top entry must be the highest-importance one.
        assert panel["entries"][0]["importance"] == 9
        # Truncation: any 120+ char content shows the ellipsis marker.
        long_entry = next((e for e in panel["entries"] if e["importance"] == 6), None)
        if long_entry is not None:
            assert len(long_entry["content"]) <= 124  # 120 + "..."

    def test_session_context_working_panel_failure_graceful(
        self, tmp_path, monkeypatch
    ):
        """If working_store.list_top_k raises, the panel surfaces as
        empty rather than breaking get_session_context."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch(
            "mcp_server.storage.working_store.list_top_k",
            side_effect=Exception("synthetic"),
        ):
            with patch(
                "mcp_server.tools.roadmap.get_roadmap",
                return_value={"current_phase": {}},
            ):
                result = learning.get_session_context()
        assert result["working"] == {"entries": [], "count": 0}

    def test_session_context_empty_db(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch(
            "mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("no roadmap")
        ):
            result = learning.get_session_context()

        assert result["recent_sessions"] == []
        assert result["recent_decisions"] == []
        # v2.2.0+: top_signals (preferences + rules) removed.

    def test_session_context_surfaces_phase_key_decisions(self, tmp_path, monkeypatch):
        """Bug 5 regression: complete_phase(key_decisions=[...]) writes to
        the roadmap store, NOT the decisions table. Without surfacing in
        session_context, a fresh session has no way to learn what was
        just decided when the previous phase completed.

        Fix: query the roadmap's recently-completed phases and include
        their key_decisions tagged with source='phase_completion'.
        """
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        # Simulate two completed phases with key_decisions
        mock_roadmap_for_get = {
            "current_phase": {
                "name": "Phase 5",
                "next_action": "Do",
                "status": "in_progress",
            },
        }
        mock_roadmap_data = {
            "completed_phases": [
                {
                    "number": 1,
                    "name": "Stub closure",
                    "key_decisions": [
                        "Plan 1 Week 1 multi-host Go client foundation shipped (commit ce24961).",
                        "Hardening pass commit 3a4bc05 closes Week 1.",
                    ],
                },
                {
                    "number": 2,
                    "name": "Plan 1 Week 2 — Core commands ported",
                    "key_decisions": [
                        "12 Python CLI commands ported to Go (operator/cmd/uadp/*.go).",
                    ],
                },
            ],
        }

        with patch(
            "mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap_for_get
        ):
            with patch(
                "mcp_server.tools.roadmap._load_roadmap", return_value=mock_roadmap_data
            ):
                with patch(
                    "mcp_server.tools.changesets.list_open_changesets",
                    return_value={"open_changesets": [], "count": 0, "warning": None},
                ):
                    result = learning.get_session_context()

        assert "recent_phase_decisions" in result, (
            "Bug 5 regression: get_session_context must include "
            "`recent_phase_decisions` field"
        )
        decisions = result["recent_phase_decisions"]
        assert len(decisions) >= 2
        # Most recent completed phase first (phase 2)
        assert decisions[0]["phase_number"] == 2
        assert decisions[0]["source"] == "phase_completion"
        assert "Go" in decisions[0]["decision"]
        # Phase 1 decisions present too
        phase_1_decisions = [d for d in decisions if d["phase_number"] == 1]
        assert len(phase_1_decisions) >= 1

    def test_session_context_phase_decisions_capped_at_5(self, tmp_path, monkeypatch):
        """Don't blow the ~500-token budget — cap phase decisions at 5."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        many_decisions = [f"Decision {i}" for i in range(20)]
        mock_roadmap_data = {
            "completed_phases": [
                {"number": 1, "name": "Phase 1", "key_decisions": many_decisions},
            ],
        }
        with patch(
            "mcp_server.tools.roadmap._load_roadmap", return_value=mock_roadmap_data
        ):
            with patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ):
                result = learning.get_session_context()

        assert len(result["recent_phase_decisions"]) <= 5

    def test_session_context_no_completed_phases(self, tmp_path, monkeypatch):
        """Empty completed_phases → recent_phase_decisions is empty list,
        not missing or None."""
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()

        with patch(
            "mcp_server.tools.roadmap._load_roadmap",
            return_value={"completed_phases": []},
        ):
            with patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ):
                result = learning.get_session_context()

        assert result["recent_phase_decisions"] == []

    def test_session_context_recent_decisions_tagged_with_source(
        self, tmp_path, monkeypatch
    ):
        """Bug 5 — the existing recent_decisions list (from sessions table)
        should now be tagged source='session' so AIs can distinguish it
        from the new recent_phase_decisions list."""
        project, data_dir, db = _setup_project(tmp_path, monkeypatch)
        # Seed a real decision so recent_decisions isn't empty
        db.log_session(
            "s-test",
            "test session",
            "1",
            [
                {"file_path": "src/api.py", "decision": "Use REST", "context": "ctx"},
            ],
        )
        db.close()

        with patch(
            "mcp_server.tools.changesets.list_open_changesets",
            return_value={"open_changesets": [], "count": 0, "warning": None},
        ):
            result = learning.get_session_context()

        if result["recent_decisions"]:
            for d in result["recent_decisions"]:
                assert (
                    d.get("source") == "session"
                ), f"recent_decisions entries must be tagged source='session'; got {d}"

    def test_session_context_recent_decisions_preserve_file_path(
        self, tmp_path, monkeypatch
    ):
        """2026-05-18 v2.1.2 Item 19: a decision recorded WITH file_path
        must round-trip through get_session_context with file_path intact
        (not silently coerced to None). Field-test Report 4 #8 flagged
        the serialization quirk; this test guards against regression.

        v3.0.0 round-3 (2026-05-23): rewrote to use the v3.0.0 canonical
        store (decisions_store.record → .codevira/decisions.jsonl) since
        get_session_context now reads from JSONL, not the legacy SQLiteGraph
        decisions table. The AgentStore system test in
        scripts/system_test_agentstore.py::A8 caught that recent_decisions
        was always empty in v3.0.0 — this test was passing because it set
        up data via db.log_session (legacy SQL path) which is invisible to
        the v3.0.0 read code.
        """
        project, data_dir, db = _setup_project(tmp_path, monkeypatch)
        db.close()  # we don't need the SQLite handle in v3.0.0

        from mcp_server.storage import decisions_store as _decisions_store

        _decisions_store.record(
            decision="Use vue3 composables",
            file_path="src/widgets.py",
            session_id="s-fp",
        )
        _decisions_store.record(
            decision="REST not GraphQL",
            file_path="src/api.py",
            session_id="s-fp",
        )

        result = learning.get_session_context()

        recent = result["recent_decisions"]
        assert recent, "expected recent_decisions to be non-empty after log_session"
        paths_returned = {d.get("file_path") for d in recent}
        assert "src/widgets.py" in paths_returned or "src/api.py" in paths_returned, (
            f"Item 19 regression: file_path lost in serialization. "
            f"recent_decisions = {recent}"
        )
        # No entry should silently drop file_path → None when the underlying
        # decision had one.
        for d in recent:
            if d.get("decision") in ("Use vue3 composables", "REST not GraphQL"):
                assert d.get("file_path") is not None, (
                    f"Item 19 regression: file_path is None for decision "
                    f"{d.get('decision')!r} that was recorded with a path."
                )


# =====================================================================
# get_session_context exception branches (lines 171-173, 180-182)
# =====================================================================


class TestGetSessionContextExceptions:
    def test_graceful_on_dependent_failures(self, tmp_path, monkeypatch):
        """get_session_context continues when sub-calls raise.

        v1.7.0 dropped global_intelligence and indexing_progress from the
        response (they belong in admin/status tools, not session context).
        Verify the function still returns a valid response when sub-calls fail.
        """
        _setup_project(tmp_path, monkeypatch)

        with patch(
            "mcp_server.tools.roadmap.get_roadmap", side_effect=Exception("no roadmap")
        ):
            result = learning.get_session_context()

        assert result is not None
        assert "recent_sessions" in result
        assert result["current_phase"] == {}


# =====================================================================
# _maturity_hint boundary coverage (lines 233, 237)
# =====================================================================


# =====================================================================
# v1.8: Open-changesets key bug (Change 0) + focus inference (Change 1)
# =====================================================================


def _changeset(
    id: str, files: list[str], created: str = "2026-04-22", description: str = "desc"
) -> dict:
    """Helper producing the raw list_open_changesets() item shape."""
    return {
        "id": id,
        "description": description,
        "created": created,
        "files_pending": files,
        "blocker": None,
    }


class TestOpenChangesetsKeyFixed:
    """Change 0: get_session_context() must read the real key."""

    # v2.2.0+: test_open_changesets_key_fixed removed — the feature it
    # tested (the open_changesets field of get_session_context) was
    # deleted along with the rest of the changesets surface.


class TestInferFocus:
    """v2.2.0+: _infer_focus uses only current_phase.next_action.

    Changeset-based focus inference (priority 1 in v2.1.x) removed
    along with the changesets feature per 2026-05-22 surface-cut audit.
    """

    def test_focus_from_next_action(self):
        cp = {"next_action": "Refactor authentication middleware pipeline"}
        focus, source = learning._infer_focus(cp)
        assert source == "next_action"
        # All tokens >= 4 chars
        assert "refactor" in focus
        assert "authentication" in focus
        assert "middleware" in focus
        assert "pipeline" in focus

    def test_focus_weak_signal_ignored_short(self):
        cp = {"next_action": "continue work"}
        focus, source = learning._infer_focus(cp)
        assert focus is None
        assert source is None

    def test_focus_weak_signal_ignored_stop_list_only(self):
        cp = {"next_action": "continue work fix todo"}
        focus, source = learning._infer_focus(cp)
        assert focus is None
        assert source is None

    def test_focus_none_when_no_signals(self):
        focus, source = learning._infer_focus({})
        assert focus is None
        assert source is None


class TestSessionContextFocus:
    """Change 1: focus inference wired into get_session_context()."""

    def test_focus_source_field_always_present(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        db.close()
        with (
            patch(
                "mcp_server.tools.roadmap.get_roadmap",
                return_value={"current_phase": {}},
            ),
            patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ),
        ):
            result = learning.get_session_context()
        assert "focus_source" in result
        assert result["focus_source"] is None

    # v2.2.0+: test_focus_source_reflects_changeset removed
    # (changeset-based focus inference is gone; only next_action is used).
    # test_focus_pads_with_recent_when_few_matches: same reason.

    def test_focus_from_next_action_sets_source(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        roadmap = {
            "current_phase": {
                "name": "API Hardening",
                "status": "in_progress",
                "next_action": "Add validation layer to api endpoints",
            }
        }
        with (
            patch("mcp_server.tools.roadmap.get_roadmap", return_value=roadmap),
            patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ),
        ):
            result = learning.get_session_context()

        assert result["focus_source"] == "next_action"

    def test_no_focus_uses_chronological_fallback(self, tmp_path, monkeypatch):
        _, _, db = _setup_project(tmp_path, monkeypatch)
        _seed_full_project(db)
        db.close()

        with (
            patch(
                "mcp_server.tools.roadmap.get_roadmap",
                return_value={"current_phase": {}},
            ),
            patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ),
        ):
            result = learning.get_session_context()

        assert result["focus_source"] is None
        # 3 decisions seeded → should get all 3, newest first
        assert len(result["recent_decisions"]) == 3


# =====================================================================
# get_session_context — communication `style` panel
#
# v3.3.0 (D0000LU) wired LLM-distilled communication preferences into
# get_session_context as a budgeted one-line `style` panel, but it shipped
# WITHOUT end-to-end coverage. These tests pin that contract (v3.4.0):
# present when communication prefs exist, omitted when not, truncated to
# the 160-char budget, and never able to break the brief.
#
# Per D0000NJ, per-prompt injection into relevance_inject stays deferred —
# the session-start panel is the shipped read surface.
# =====================================================================


def _seed_communication_prefs(db_path, signals: list[str]) -> None:
    """Create an isolated global.db and seed communication preferences.

    Passing an empty list still creates the schema (so the file exists with
    no communication rows) — used for the omitted-panel cases.
    """
    from indexer.global_db import GlobalDB

    db = GlobalDB(db_path)
    try:
        for sig in signals:
            db.upsert_preference("communication", sig, None, "proj-a")
    finally:
        db.close()


class TestSessionContextStylePanel:
    def _run(self, monkeypatch, db_path):
        """Call get_session_context with roadmap + changesets mocked and the
        global DB pointed at db_path."""
        monkeypatch.setattr(paths, "get_global_db_path", lambda: db_path)
        mock_roadmap = {
            "current_phase": {
                "name": "Phase 5",
                "next_action": "Do stuff",
                "status": "in_progress",
            },
        }
        with patch("mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap):
            with patch(
                "mcp_server.tools.changesets.list_open_changesets",
                return_value={"open_changesets": [], "count": 0, "warning": None},
            ):
                return learning.get_session_context()

    def test_style_present_when_communication_prefs_exist(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        db_path = tmp_path / "global.db"
        _seed_communication_prefs(db_path, ["keep answers short", "tests first"])

        result = self._run(monkeypatch, db_path)

        assert "style" in result
        assert "keep answers short" in result["style"]
        assert "tests first" in result["style"]

    def test_style_omitted_when_no_communication_prefs(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        db_path = tmp_path / "global.db"
        _seed_communication_prefs(db_path, [])  # schema only, no rows

        result = self._run(monkeypatch, db_path)
        assert "style" not in result

    def test_style_omitted_when_global_db_missing(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        db_path = tmp_path / "never-created.db"

        result = self._run(monkeypatch, db_path)
        assert "style" not in result

    def test_style_truncated_to_budget(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        db_path = tmp_path / "global.db"
        _seed_communication_prefs(db_path, ["x" * 300])

        result = self._run(monkeypatch, db_path)
        assert "style" in result
        assert len(result["style"]) <= 160

    def test_style_survives_preferences_error(self, tmp_path, monkeypatch):
        """If preference lookup raises, the brief still returns — minus the
        style key. The panel must never break get_session_context."""
        _setup_project(tmp_path, monkeypatch)
        db_path = tmp_path / "global.db"
        _seed_communication_prefs(db_path, ["keep answers short"])
        monkeypatch.setattr(paths, "get_global_db_path", lambda: db_path)

        def boom(*a, **k):
            raise RuntimeError("preferences exploded")

        mock_roadmap = {
            "current_phase": {
                "name": "P",
                "next_action": "x",
                "status": "in_progress",
            },
        }
        with patch("mcp_server.tools.preferences.search_preferences", side_effect=boom):
            with patch(
                "mcp_server.tools.roadmap.get_roadmap", return_value=mock_roadmap
            ):
                with patch(
                    "mcp_server.tools.changesets.list_open_changesets",
                    return_value={
                        "open_changesets": [],
                        "count": 0,
                        "warning": None,
                    },
                ):
                    result = learning.get_session_context()

        assert "style" not in result
        assert "current_phase" in result  # brief still returned
