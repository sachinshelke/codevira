"""Tests for the per-verdict enforcement audit log (4.0 Step 3.5).

The load-bearing property is NOT that rows get written — it is that a
broken audit path can never change a verdict or raise into the hook
wiring. Those are the tests that matter here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server.engine import audit
from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine import Policy, PolicyVerdict, register_policy, reset_policies
from mcp_server.engine.runner import dispatch


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_policies()
    monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)
    monkeypatch.setenv("CODEVIRA_AUTO_ADOPT", "1")
    yield
    reset_policies()


def _event(project: Path, event_type=EventType.PRE_TOOL_USE) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        project_root=project,
        session_id="s1",
        tool_name="Edit",
        tool_input={"file_path": str(project / "x.py")},
    )


def _rows(project: Path) -> list[dict]:
    p = audit.path(project)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


class _Blocker(Policy):
    name = "blocker"
    handles = (EventType.PRE_TOOL_USE,)

    def evaluate(self, event):
        return PolicyVerdict.block("nope", metadata={"decision_ids": ["D000001"]})


class _Allower(Policy):
    name = "allower"
    handles = (EventType.PRE_TOOL_USE,)

    def evaluate(self, event):
        return PolicyVerdict.allow()


class TestRecording:
    def test_block_is_recorded_with_evidence(self, tmp_path: Path) -> None:
        register_policy(_Blocker())
        verdict = dispatch(_event(tmp_path))
        assert verdict.action == "block"

        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["action"] == "block"
        assert rows[0]["policies"] == ["blocker"]
        assert rows[0]["eligible"] == 1
        assert rows[0]["meta"]["decision_ids"] == ["D000001"]

    def test_allow_is_recorded_so_a_rate_can_be_computed(self, tmp_path: Path) -> None:
        """Without allow rows there is no denominator and no false-block rate."""
        register_policy(_Allower())
        dispatch(_event(tmp_path))

        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["action"] == "allow"
        assert rows[0]["policies"] == []

    def test_identity_is_stamped(self, tmp_path: Path) -> None:
        """The v3.3.0 outcomes file omitted these, which made its own
        'across multiple machines' question unanswerable."""
        register_policy(_Blocker())
        dispatch(_event(tmp_path))

        row = _rows(tmp_path)[0]
        assert "host_hash" in row
        assert "version" in row
        assert "ide" in row
        assert row["project"] == str(tmp_path)

    def test_no_eligible_policy_writes_nothing(self, tmp_path: Path) -> None:
        register_policy(_Blocker())  # handles PRE_TOOL_USE only
        dispatch(_event(tmp_path, EventType.STOP))
        assert _rows(tmp_path) == []


class TestNeverAffectsEnforcement:
    """P9. These are the tests that justify shipping this on the hot path."""

    def test_unwritable_audit_path_does_not_change_the_verdict(
        self, tmp_path: Path
    ) -> None:
        register_policy(_Blocker())
        # open(..., "a") on a directory raises IsADirectoryError (OSError)
        audit.path(tmp_path).mkdir(parents=True, exist_ok=True)

        verdict = dispatch(_event(tmp_path))
        assert verdict.action == "block"
        assert verdict.message == "nope"

    def test_audit_exception_does_not_propagate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_policy(_Blocker())
        monkeypatch.setattr(
            audit, "record", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        verdict = dispatch(_event(tmp_path))
        assert verdict.action == "block"

    def test_canonical_store_is_never_touched(self, tmp_path: Path) -> None:
        """Instrumentation writes to .codevira-cache/, never .codevira/."""
        register_policy(_Blocker())
        dispatch(_event(tmp_path))

        assert (tmp_path / ".codevira-cache" / "enforcement.jsonl").exists()
        assert not (tmp_path / ".codevira").exists()


class TestBounds:
    def test_rotates_at_size_cap(self, tmp_path: Path) -> None:
        p = audit.path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * (audit._MAX_BYTES + 1))

        register_policy(_Blocker())
        dispatch(_event(tmp_path))

        assert p.with_suffix(p.suffix + ".1").exists()
        assert len(_rows(tmp_path)) == 1

    def test_oversized_metadata_is_truncated_not_dropped(self, tmp_path: Path) -> None:
        class Fat(Policy):
            name = "fat"
            handles = (EventType.PRE_TOOL_USE,)

            def evaluate(self, event):
                return PolicyVerdict.block("x", metadata={"blob": "y" * 50_000})

        register_policy(Fat())
        dispatch(_event(tmp_path))

        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["meta"] == {"_truncated": True}
        assert rows[0]["action"] == "block"  # identity + verdict survive
