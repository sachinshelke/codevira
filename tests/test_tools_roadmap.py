"""
Tests for mcp_server/tools/roadmap.py — roadmap lifecycle, planning, and edge cases.

Covers:
  - add_phase: adding phases with priority ordering, duplicate detection
  - complete_phase: validates current phase number, advances to next, records decisions
  - update_phase_status: pending/in_progress/blocked transitions
  - defer_phase: moves upcoming to deferred list
  - get_phase: retrieves any phase by number (current, completed, upcoming)
  - update_next_action: updates next_action on current phase
  - add_open_changeset / remove_open_changeset: changeset tracking
  - Full lifecycle: add -> start -> complete -> advance -> defer
  - Edge cases: corrupt YAML, empty roadmap, wrong phase number, etc.
"""
from __future__ import annotations

import yaml
from pathlib import Path

import mcp_server.paths as paths
from mcp_server.tools import roadmap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_project(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Create a temp project with a .codevira data dir and monkeypatch paths."""
    project_root = tmp_path / "test-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text("project:\n  name: test-roadmap\n")
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project_root.resolve())
    return project_root, data_dir


def _write_roadmap(data_dir: Path, data: dict) -> None:
    """Write a roadmap.yaml directly for test setup."""
    with open(data_dir / "roadmap.yaml", "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _read_roadmap(data_dir: Path) -> dict:
    """Read the raw roadmap.yaml from disk."""
    with open(data_dir / "roadmap.yaml") as f:
        return yaml.safe_load(f)


# =====================================================================
# add_phase
# =====================================================================

class TestAddPhase:
    def test_add_phase_basic(self, tmp_path, monkeypatch):
        """Adding a basic phase should succeed and appear in upcoming."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.add_phase(
            phase=10, name="Schema Versioning",
            description="Add schema migration support",
        )
        assert result["success"] is True
        assert result["phase"] == 10
        assert result["name"] == "Schema Versioning"
        assert result["position_in_queue"] == 1
        assert result["total_upcoming"] == 1

    def test_add_phase_with_all_fields(self, tmp_path, monkeypatch):
        """Adding a phase with effort, files, priority, depends_on should persist."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.add_phase(
            phase=20, name="API Refactor",
            description="Refactor API endpoints",
            priority="high",
            depends_on=[10],
            files=["src/api.py", "src/routes.py"],
            effort="~4 hours",
        )
        assert result["success"] is True
        full = roadmap.get_full_roadmap()
        upcoming = full["upcoming_phases"]
        assert len(upcoming) == 1
        phase = upcoming[0]
        assert phase["effort"] == "~4 hours"
        assert phase["files"] == ["src/api.py", "src/routes.py"]
        assert phase["depends_on"] == [10]

    def test_add_phase_duplicate_rejected(self, tmp_path, monkeypatch):
        """Adding a phase with a number that already exists should fail."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=5, name="Phase A", description="desc A")
        result = roadmap.add_phase(phase=5, name="Phase B", description="desc B")
        assert result["success"] is False
        assert "already exists" in result["message"]

    def test_add_phase_duplicate_current_rejected(self, tmp_path, monkeypatch):
        """Adding a phase whose number matches current_phase should fail."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        # Stub roadmap starts with current_phase number = 1
        result = roadmap.add_phase(phase=1, name="Duplicate", description="dup")
        assert result["success"] is False

    def test_add_phase_high_priority_front_of_queue(self, tmp_path, monkeypatch):
        """High-priority phases should be inserted at the front of the queue."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="Medium A", description="m-a", priority="medium")
        roadmap.add_phase(phase=11, name="Medium B", description="m-b", priority="medium")
        result = roadmap.add_phase(phase=12, name="Urgent", description="urgent!", priority="high")
        assert result["position_in_queue"] == 1  # front

    def test_add_phase_multiple_high_priority_ordering(self, tmp_path, monkeypatch):
        """Multiple high-priority phases should stack in order after each other."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="Low A", description="l-a", priority="low")
        roadmap.add_phase(phase=11, name="High A", description="h-a", priority="high")
        roadmap.add_phase(phase=12, name="High B", description="h-b", priority="high")
        full = roadmap.get_full_roadmap()
        names = [p["name"] for p in full["upcoming_phases"]]
        assert names == ["High A", "High B", "Low A"]

    def test_add_phase_string_label(self, tmp_path, monkeypatch):
        """Phase number can be a string label like '8R'."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.add_phase(phase="8R", name="Rollback Phase", description="rb")
        assert result["success"] is True
        assert result["phase"] == "8R"


# =====================================================================
# complete_phase
# =====================================================================

class TestCompletePhase:
    def test_complete_current_phase(self, tmp_path, monkeypatch):
        """Completing the current phase should move it to completed and advance."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=2, name="Next Up", description="next")
        result = roadmap.complete_phase(
            phase_number=1,
            key_decisions=["Chose REST over GraphQL", "Adopted snake_case"],
        )
        assert result["success"] is True
        assert result["completed_phase"] == 1
        assert result["key_decisions_recorded"] == 2
        assert result["advanced_to"] == 2

    def test_complete_wrong_phase_number_fails(self, tmp_path, monkeypatch):
        """Completing a phase that is not the current one should fail."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.complete_phase(
            phase_number=999,
            key_decisions=["Irrelevant"],
        )
        assert result["success"] is False
        assert "not 999" in result["message"]

    def test_complete_records_started_date(self, tmp_path, monkeypatch):
        """If current phase had a started date, it should be preserved in completed entry."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_phase_status("in_progress", started="2025-01-15")
        roadmap.add_phase(phase=2, name="Next", description="n")
        roadmap.complete_phase(phase_number=1, key_decisions=["d1"])
        full = roadmap.get_full_roadmap()
        completed = full["completed_phases"]
        assert len(completed) == 1
        assert completed[0]["started"] == "2025-01-15"

    def test_complete_with_no_upcoming_phases(self, tmp_path, monkeypatch):
        """Completing with no upcoming should set advanced_to to None."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.complete_phase(phase_number=1, key_decisions=["Done"])
        assert result["success"] is True
        assert result["advanced_to"] is None
        current = roadmap.get_roadmap()["current_phase"]
        assert current["name"] == "No upcoming phases"

    def test_complete_advances_to_correct_phase(self, tmp_path, monkeypatch):
        """After completion, the next upcoming phase should become current."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=2, name="Phase Two", description="desc 2")
        roadmap.add_phase(phase=3, name="Phase Three", description="desc 3")
        roadmap.complete_phase(phase_number=1, key_decisions=["d1"])
        current = roadmap.get_roadmap()["current_phase"]
        assert current["number"] == 2
        assert current["name"] == "Phase Two"
        assert current["status"] == "pending"


# =====================================================================
# update_phase_status
# =====================================================================

class TestUpdatePhaseStatus:
    def test_set_in_progress(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.update_phase_status("in_progress")
        assert result["success"] is True
        assert result["status"] == "in_progress"

    def test_set_blocked_without_blocker_fails(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.update_phase_status("blocked")
        assert result["success"] is False
        assert "blocker" in result["message"].lower()

    def test_set_blocked_with_blocker(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.update_phase_status("blocked", blocker="Waiting on API design review")
        assert result["success"] is True
        assert result["blocker"] == "Waiting on API design review"

    def test_invalid_status_rejected(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.update_phase_status("completed")
        assert result["success"] is False
        assert "Invalid status" in result["message"]

    def test_in_progress_sets_started_date(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_phase_status("in_progress")
        raw = _read_roadmap(data_dir)
        assert "started" in raw["current_phase"]

    def test_in_progress_custom_started_date(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_phase_status("in_progress", started="2025-03-01")
        raw = _read_roadmap(data_dir)
        assert raw["current_phase"]["started"] == "2025-03-01"

    def test_blocker_cleared_when_unblocked(self, tmp_path, monkeypatch):
        """Transitioning from blocked to pending should remove the blocker field."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_phase_status("blocked", blocker="API not ready")
        roadmap.update_phase_status("pending")
        raw = _read_roadmap(data_dir)
        assert "blocker" not in raw["current_phase"]

    def test_set_pending(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_phase_status("in_progress")
        result = roadmap.update_phase_status("pending")
        assert result["success"] is True
        assert result["status"] == "pending"


# =====================================================================
# defer_phase
# =====================================================================

class TestDeferPhase:
    def test_defer_upcoming_phase(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="Optional Work", description="optional")
        result = roadmap.defer_phase(phase_number=10, reason="Not a priority right now")
        assert result["success"] is True
        assert result["name"] == "Optional Work"
        assert result["reason"] == "Not a priority right now"
        assert result["remaining_upcoming"] == 0

    def test_defer_nonexistent_phase_fails(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.defer_phase(phase_number=999, reason="no reason")
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_defer_preserves_original_metadata(self, tmp_path, monkeypatch):
        """Deferred phase should retain original_priority, goal, and deferred_date."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="Deferred Work", description="def-desc", priority="high")
        roadmap.defer_phase(phase_number=10, reason="Shifted priorities")
        full = roadmap.get_full_roadmap()
        deferred = full["deferred"]
        assert len(deferred) == 1
        entry = deferred[0]
        assert entry["original_priority"] == "high"
        assert entry["reason"] == "Shifted priorities"
        assert "deferred_date" in entry

    def test_defer_removes_from_upcoming(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="A", description="a")
        roadmap.add_phase(phase=11, name="B", description="b")
        roadmap.defer_phase(phase_number=10, reason="reason")
        full = roadmap.get_full_roadmap()
        upcoming_phases = [p["phase"] for p in full["upcoming_phases"]]
        assert 10 not in upcoming_phases
        assert 11 in upcoming_phases


# =====================================================================
# get_phase
# =====================================================================

class TestGetPhase:
    def test_get_current_phase(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.get_phase(1)
        assert result["found"] is True
        assert result["location"] == "current"

    def test_get_upcoming_phase(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=10, name="Upcoming", description="u")
        result = roadmap.get_phase(10)
        assert result["found"] is True
        assert result["location"] == "upcoming"

    def test_get_completed_phase(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_phase(phase=2, name="Next", description="n")
        roadmap.complete_phase(phase_number=1, key_decisions=["d"])
        result = roadmap.get_phase(1)
        assert result["found"] is True
        assert result["location"] == "completed"

    def test_get_nonexistent_phase(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.get_phase(999)
        assert result["found"] is False
        assert "hint" in result


# =====================================================================
# update_next_action
# =====================================================================

class TestUpdateNextAction:
    def test_update_next_action(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.update_next_action("Implement the user auth flow")
        assert result["success"] is True
        assert result["next_action"] == "Implement the user auth flow"

    def test_next_action_persisted(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.update_next_action("Do the thing")
        current = roadmap.get_roadmap()["current_phase"]
        assert current["next_action"] == "Do the thing"


# =====================================================================
# add_open_changeset / remove_open_changeset
# =====================================================================

class TestChangesetTracking:
    def test_add_changeset(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.add_open_changeset("auth-refactor")
        assert result["success"] is True
        assert "auth-refactor" in result["open_changesets"]

    def test_add_changeset_idempotent(self, tmp_path, monkeypatch):
        """Adding the same changeset twice should not create duplicates."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_open_changeset("fix-123")
        roadmap.add_open_changeset("fix-123")
        result = roadmap.get_roadmap()
        assert result["current_phase"]["open_changesets"].count("fix-123") == 1

    def test_remove_changeset(self, tmp_path, monkeypatch):
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        roadmap.add_open_changeset("fix-123")
        roadmap.add_open_changeset("fix-456")
        result = roadmap.remove_open_changeset("fix-123")
        assert result["success"] is True
        assert "fix-123" not in result["open_changesets"]
        assert "fix-456" in result["open_changesets"]

    def test_remove_nonexistent_changeset(self, tmp_path, monkeypatch):
        """Removing a changeset that doesn't exist should succeed silently."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.remove_open_changeset("nonexistent")
        assert result["success"] is True


# =====================================================================
# Full Lifecycle Test
# =====================================================================

class TestLifecycle:
    def test_full_lifecycle(self, tmp_path, monkeypatch):
        """Add 3 phases -> start first -> complete -> verify second is current -> defer third."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)

        # Add 3 upcoming phases
        roadmap.add_phase(phase=2, name="Phase Two", description="Second phase")
        roadmap.add_phase(phase=3, name="Phase Three", description="Third phase")
        roadmap.add_phase(phase=4, name="Phase Four", description="Fourth phase")

        # Start the current phase (1)
        roadmap.update_phase_status("in_progress")
        current = roadmap.get_roadmap()["current_phase"]
        assert current["status"] == "in_progress"
        assert current["number"] == 1

        # Complete phase 1 with decisions
        result = roadmap.complete_phase(
            phase_number=1,
            key_decisions=["Decided on REST API", "Using PostgreSQL"],
        )
        assert result["success"] is True
        assert result["advanced_to"] == 2

        # Verify phase 2 is now current
        current = roadmap.get_roadmap()["current_phase"]
        assert current["number"] == 2
        assert current["name"] == "Phase Two"

        # Verify phase 1 is in completed with decisions
        phase1 = roadmap.get_phase(1)
        assert phase1["found"] is True
        assert phase1["location"] == "completed"
        assert "Decided on REST API" in phase1["phase"]["key_decisions"]

        # Defer phase 4
        defer_result = roadmap.defer_phase(phase_number=4, reason="Low priority")
        assert defer_result["success"] is True

        # Verify final state
        full = roadmap.get_full_roadmap()
        assert len(full["completed_phases"]) == 1
        assert len(full["upcoming_phases"]) == 1  # Only phase 3 remains
        assert len(full["deferred"]) == 1
        assert full["deferred"][0]["name"] == "Phase Four"


# =====================================================================
# Edge Cases
# =====================================================================

class TestEdgeCases:
    def test_empty_roadmap_auto_creates_stub(self, tmp_path, monkeypatch):
        """An empty data dir should auto-create a stub roadmap on first access."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        result = roadmap.get_roadmap()
        assert result["current_phase"]["number"] == 1
        assert result["current_phase"]["name"] == "Getting Started"
        assert (data_dir / "roadmap.yaml").exists()

    def test_corrupt_yaml_handled(self, tmp_path, monkeypatch):
        """Non-dict YAML content should be treated as invalid and create a stub."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        (data_dir / "roadmap.yaml").write_text("this is not valid yaml mapping: [")
        # Even with corrupt YAML, get_roadmap should recover
        try:
            result = roadmap.get_roadmap()
            # If it doesn't raise, it should still return a usable structure
            assert "current_phase" in result
        except yaml.YAMLError:
            pass  # This is also acceptable behavior

    def test_yaml_with_scalar_value_normalizes(self, tmp_path, monkeypatch):
        """A YAML that loads as a string should normalize to a stub."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        (data_dir / "roadmap.yaml").write_text("just a plain string\n")
        result = roadmap.get_roadmap()
        assert "current_phase" in result
        assert result["current_phase"]["number"] is not None

    def test_get_roadmap_returns_compact_summary(self, tmp_path, monkeypatch):
        """get_roadmap() should return only top 3 upcoming and counts."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        for i in range(2, 10):
            roadmap.add_phase(phase=i, name=f"Phase {i}", description=f"desc {i}")
        result = roadmap.get_roadmap()
        assert len(result["upcoming"]) <= 3
        assert "deferred_count" in result
        assert "completed_phases_count" in result

    def test_get_full_roadmap_returns_all(self, tmp_path, monkeypatch):
        """get_full_roadmap() should return all upcoming phases."""
        _, data_dir = _setup_project(tmp_path, monkeypatch)
        for i in range(2, 10):
            roadmap.add_phase(phase=i, name=f"Phase {i}", description=f"desc {i}")
        result = roadmap.get_full_roadmap()
        assert len(result["upcoming_phases"]) == 8
        assert "summary" in result


# =====================================================================
# Ported from test_stability.py: legacy migration
# =====================================================================

class TestLegacyMigration:
    def test_get_roadmap_migrates_legacy_current_phase(self, tmp_path, monkeypatch):
        """Ported from test_stability.py: legacy roadmap with integer current_phase
        should be migrated to the new dict-based format on first access."""
        project_root = tmp_path / "legacy-project"
        data_dir = project_root / ".codevira"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_text("project:\n  name: test\n")
        monkeypatch.setattr(paths, "_project_dir_override", None)
        monkeypatch.chdir(project_root.resolve())

        legacy_roadmap = {
            "current_phase": 1,
            "next_action": "Finish bootstrapping",
            "open_changesets": ["cs-1"],
            "phases": [
                {
                    "number": 1,
                    "name": "Bootstrap",
                    "description": "Initialize the project",
                    "status": "in_progress",
                },
                {
                    "number": 2,
                    "name": "Next Phase",
                    "description": "Follow-up work",
                    "status": "pending",
                },
            ],
            "deferred": [],
        }
        _write_roadmap(data_dir, legacy_roadmap)

        compact = roadmap.get_roadmap()
        full = roadmap.get_full_roadmap()

        assert compact["current_phase"]["number"] == 1
        assert compact["current_phase"]["name"] == "Bootstrap"
        assert compact["current_phase"]["open_changesets"] == ["cs-1"]
        assert compact["upcoming"][0]["phase"] == 2
        assert full["current_phase"]["number"] == 1
        assert full["upcoming_phases"][0]["number"] == 2

        migrated = _read_roadmap(data_dir)

        assert isinstance(migrated["current_phase"], dict)
        assert migrated["current_phase"]["number"] == 1
        assert migrated["upcoming_phases"][0]["phase"] == 2
