"""test_diff_envelope.py — Phase 9 false-positive fix.

Two layers of coverage:

1. ``TestSynthesizeProposedDiff`` — unit tests for the shared diff
   synthesizer. The load-bearing new behavior is that ``Write`` reads the
   current on-disk content as the ``before`` block (empty for a new
   file), so a full-file Write carries an honest ``--- before/--- after``
   envelope instead of raw content.

2. ``TestWriteAdditiveFix*`` — end-to-end regression: a ``Write`` payload
   run through the synthesizer and then the real policies. Proves the
   false positive is gone (a purely-additive Write to a locked /
   high-fan-in file no longer hard-blocks) AND that the moat still holds
   (a destructive Write still blocks). These are the cases that, before
   the fix, forced the user to toggle enforcement off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
from mcp_server.engine.policies.decision_lock import DecisionLock
from mcp_server.engine.policies._signature_detect import parse_diff
from mcp_server.engine.wiring._diff_envelope import synthesize_proposed_diff


# ---------------------------------------------------------------------
# Layer 1 — synthesizer units
# ---------------------------------------------------------------------


class TestSynthesizeProposedDiff:
    def test_edit_builds_envelope(self) -> None:
        diff = synthesize_proposed_diff(
            "Edit", {"old_string": "a", "new_string": "a\nb"}, None
        )
        assert diff == "--- before\na\n--- after\na\nb\n"

    def test_edit_empty_returns_none(self) -> None:
        assert synthesize_proposed_diff("Edit", {}, None) is None

    def test_multiedit_concatenates(self) -> None:
        diff = synthesize_proposed_diff(
            "MultiEdit",
            {
                "edits": [
                    {"old_string": "a", "new_string": "A"},
                    {"old_string": "b", "new_string": "B"},
                ]
            },
            None,
        )
        assert diff == "--- before\na\nb\n--- after\nA\nB\n"

    def test_multiedit_empty_returns_none(self) -> None:
        assert synthesize_proposed_diff("MultiEdit", {"edits": []}, None) is None

    def test_write_new_file_has_empty_before(self, tmp_path: Path) -> None:
        """A Write to a path that doesn't exist yet → before is empty."""
        target = tmp_path / "brand_new.py"
        diff = synthesize_proposed_diff(
            "Write", {"content": "def f():\n    return 1\n"}, target
        )
        before, after = parse_diff(diff or "")
        assert before == ""
        assert "def f()" in (after or "")

    def test_write_overwrite_reads_disk_as_before(self, tmp_path: Path) -> None:
        """A Write to an existing file → before is the current on-disk text."""
        target = tmp_path / "exists.py"
        target.write_text("def original():\n    return 0\n")
        diff = synthesize_proposed_diff(
            "Write",
            {
                "content": "def original():\n    return 0\n\ndef added():\n    return 1\n"
            },
            target,
        )
        before, after = parse_diff(diff or "")
        assert "def original()" in (before or "")
        assert "def added()" in (after or "")

    def test_write_additive_is_pure_insertion_parseable(self, tmp_path: Path) -> None:
        """The synthesized envelope makes an additive Write detectable as
        a subsequence (every before line present in after)."""
        target = tmp_path / "f.py"
        target.write_text("line_a\nline_b\n")
        diff = synthesize_proposed_diff(
            "Write", {"content": "line_a\nline_b\nline_c\n"}, target
        )
        before, after = parse_diff(diff or "")
        before_lines = [ln for ln in (before or "").splitlines() if ln.strip()]
        after_lines = [ln for ln in (after or "").splitlines() if ln.strip()]
        # every before line still present, order preserved → pure insertion
        assert before_lines == ["line_a", "line_b"]
        assert after_lines == ["line_a", "line_b", "line_c"]

    def test_write_non_string_content_returns_none(self, tmp_path: Path) -> None:
        assert (
            synthesize_proposed_diff("Write", {"content": None}, tmp_path / "x") is None
        )
        assert synthesize_proposed_diff("Write", {}, tmp_path / "x") is None

    def test_write_oversized_falls_back_to_raw(self, tmp_path: Path) -> None:
        """An envelope over the cap degrades to raw content (no markers)."""
        target = tmp_path / "big.py"
        huge = "x" * 1_000_001
        diff = synthesize_proposed_diff("Write", {"content": huge}, target)
        assert diff == huge  # raw, not wrapped
        assert "--- before" not in (diff or "")

    def test_write_target_none_has_empty_before(self) -> None:
        """No target (path rejected) → can't read disk → empty before."""
        diff = synthesize_proposed_diff("Write", {"content": "hello\n"}, None)
        before, after = parse_diff(diff or "")
        assert before == ""
        assert "hello" in (after or "")

    def test_notebookedit_passes_raw_source(self, tmp_path: Path) -> None:
        diff = synthesize_proposed_diff(
            "NotebookEdit", {"new_source": "print(1)"}, tmp_path / "n.ipynb"
        )
        assert diff == "print(1)"

    def test_notebookedit_no_content_returns_none(self, tmp_path: Path) -> None:
        assert (
            synthesize_proposed_diff("NotebookEdit", {}, tmp_path / "n.ipynb") is None
        )

    def test_unknown_tool_returns_none(self) -> None:
        assert synthesize_proposed_diff("Read", {"file_path": "x"}, None) is None

    def test_unreadable_target_degrades_to_empty_before(self, tmp_path: Path) -> None:
        """Pointing at a directory (is_file False) must not raise — before
        degrades to empty, treated as a create."""
        target = tmp_path / "a_dir"
        target.mkdir()
        diff = synthesize_proposed_diff("Write", {"content": "data\n"}, target)
        before, _ = parse_diff(diff or "")
        assert before == ""


# ---------------------------------------------------------------------
# Layer 2 — end-to-end regression through the real policies
# ---------------------------------------------------------------------


class _LockedSignals:
    """SignalContext stand-in: every file has one locked decision."""

    graph = None

    def decisions(
        self, *, file: str | None = None, locked_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        return [{"id": "D1", "decision": "Locked thing", "timestamp": None}]


class _ImpactSignals:
    """SignalContext stand-in: the target file has a fixed blast radius."""

    def __init__(self, radius: int) -> None:
        self._radius = radius

    def impact(self, path: Path) -> dict[str, Any]:
        return {"found": True, "blast_radius": self._radius, "affected": []}


def _write_event(target: Path, project_root: Path, new_content: str) -> HookEvent:
    """Build a PRE_TOOL_USE Write event whose proposed_diff is produced by
    the real synthesizer (reading ``target`` off disk for the before block)."""
    diff = synthesize_proposed_diff("Write", {"content": new_content}, target)
    return HookEvent(
        event_type=EventType.PRE_TOOL_USE,
        project_root=project_root,
        tool_name="Write",
        target_file=target,
        proposed_diff=diff,
    )


@pytest.fixture(autouse=True)
def _clear_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "CODEVIRA_DECISION_LOCK_MODE",
        "CODEVIRA_BLAST_RADIUS_MODE",
        "CODEVIRA_BLAST_RADIUS_THRESHOLD",
        "CODEVIRA_BLAST_RADIUS_WARN_THRESHOLD",
    ):
        monkeypatch.delenv(k, raising=False)


class TestWriteAdditiveFixDecisionLock:
    """The headline fix: an additive full-file Write to a locked file used
    to hard-block (raw content un-parseable). Now it warns."""

    def test_additive_write_to_locked_file_warns_not_blocks(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "auth.py"
        target.write_text("def login(user):\n    return user\n")
        # Additive: keeps login verbatim, appends logout.
        new = (
            "def login(user):\n    return user\n\ndef logout(user):\n    return None\n"
        )
        verdict = DecisionLock().evaluate(
            _write_event(target, tmp_path, new), _LockedSignals()
        )
        assert verdict.action == "warn", "additive Write must NOT hard-block"
        assert verdict.metadata["pure_insertion"] is True
        assert "D1" in (verdict.message or "")  # decision still surfaced

    def test_destructive_write_touching_decision_blocks(self, tmp_path: Path) -> None:
        """Moat preserved: a Write that CHANGES a line the locked decision is
        ABOUT still hard-blocks (v3.5.0 content-aware)."""
        target = tmp_path / "auth.py"
        target.write_text("def login(user):\n    return user\n")
        # Destructive: the `return user` line is gone (changed).
        new = "def login(user):\n    return user + '!'\n"

        class _Sig:
            graph = None

            def decisions(self, **kw):
                return [
                    {
                        "id": "D1",
                        "decision": "login(user) must return the user unchanged",
                        "timestamp": None,
                    }
                ]

        verdict = DecisionLock().evaluate(_write_event(target, tmp_path, new), _Sig())
        assert (
            verdict.is_blocking()
        ), "destructive Write touching the decision must block"
        assert verdict.metadata["pure_insertion"] is False
        assert verdict.metadata["content_orthogonal"] is False

    def test_orthogonal_destructive_write_warns(self, tmp_path: Path) -> None:
        """v3.5.0: a destructive Write that does NOT touch the locked
        decision's subject downgrades to warn (content-aware). The generic
        'Locked thing' decision is orthogonal to a login/user change."""
        target = tmp_path / "auth.py"
        target.write_text("def login(user):\n    return user\n")
        new = "def login(user):\n    return user + '!'\n"
        verdict = DecisionLock().evaluate(
            _write_event(target, tmp_path, new), _LockedSignals()
        )
        assert verdict.action == "warn"
        assert verdict.metadata["content_orthogonal"] is True

    def test_new_file_write_is_pure_insertion(self, tmp_path: Path) -> None:
        """A brand-new file (empty before) is a pure insertion."""
        target = tmp_path / "fresh.py"  # does not exist
        verdict = DecisionLock().evaluate(
            _write_event(target, tmp_path, "def f():\n    return 1\n"),
            _LockedSignals(),
        )
        assert verdict.action == "warn"
        assert verdict.metadata["pure_insertion"] is True


class TestWriteAdditiveFixBlastRadius:
    """blast_radius used to hard-block any high-fan-in Write (None/raw diff
    → 'full Write' → block). An additive Write is now allowed."""

    def test_additive_write_to_high_fanin_file_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "service.py"
        target.write_text("def public_api(x):\n    return x\n")
        # Additive: keeps public_api, adds a private helper.
        new = "def public_api(x):\n    return x\n\ndef _helper(y):\n    return y\n"
        verdict = BlastRadiusVeto().evaluate(
            _write_event(target, tmp_path, new), _ImpactSignals(radius=20)
        )
        assert verdict.action == "allow", "adding a helper to a hot file must not block"

    def test_destructive_write_to_high_fanin_file_blocks(self, tmp_path: Path) -> None:
        """Veto preserved: removing a public function from a hot file blocks."""
        target = tmp_path / "service.py"
        target.write_text("def public_api(x):\n    return x\n")
        # Destructive: public_api removed entirely.
        new = "def other(z):\n    return z\n"
        verdict = BlastRadiusVeto().evaluate(
            _write_event(target, tmp_path, new), _ImpactSignals(radius=20)
        )
        assert (
            verdict.is_blocking()
        ), "removing a public signature from a hot file must block"


class TestEndToEndHookWiring:
    """Prove the FULL Claude Code hook path builds the envelope — not just
    the synthesizer in isolation. Closes the gap where synthesizer + policy
    were each tested but never the real _build_event glue."""

    def test_build_event_write_reads_disk_for_before(self, tmp_path: Path) -> None:
        from mcp_server.engine.wiring import claude_code_hooks

        project = tmp_path / "proj"
        project.mkdir()
        target = project / "auth.py"
        target.write_text("def login():\n    return 1\n")

        raw = {
            "cwd": str(project),
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": (
                    "def login():\n    return 1\n\ndef logout():\n    return 0\n"
                ),
            },
        }
        event = claude_code_hooks._build_event(EventType.PRE_TOOL_USE, raw)
        before, after = parse_diff(event.proposed_diff or "")
        assert "def login()" in (before or ""), "disk content must become `before`"
        assert "def logout()" in (after or ""), "new content must become `after`"

    def test_build_event_new_file_write_has_empty_before(self, tmp_path: Path) -> None:
        from mcp_server.engine.wiring import claude_code_hooks

        project = tmp_path / "proj"
        project.mkdir()
        target = project / "brand_new.py"  # not on disk
        raw = {
            "cwd": str(project),
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x = 1\n"},
        }
        event = claude_code_hooks._build_event(EventType.PRE_TOOL_USE, raw)
        before, after = parse_diff(event.proposed_diff or "")
        assert before == ""
        assert "x = 1" in (after or "")


class _FixSignals:
    """SignalContext stand-in: the target file has one recorded bug-fix."""

    def fixes(self, target):
        return [
            {
                "description": "fix null user race condition",
                "line_start": 1,
                "line_end": 5,
                "commit_sha": "abc12345",
            }
        ]


class TestAntiRegressionWriteGuard:
    """v3.4.0 regression guard: giving Write an envelope must NOT activate
    anti_regression's whole-file keyword heuristic (block mode) and start
    false-blocking additive overwrites that merely mention a fix's
    keywords. Pre-v3.4.0 Write no-op'd here; that's preserved."""

    # before mentions no fix keywords; after ADDS a comment that mentions
    # several → after_hits > before_hits → the heuristic WOULD flag a revert.
    _ENVELOPE = (
        "--- before\n"
        "def f():\n    pass\n"
        "--- after\n"
        "def f():\n    pass\n# handle null user race condition cleanly\n"
    )

    def _event(self, tool_name: str):
        return HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name=tool_name,
            target_file=Path("/p/svc.py"),
            proposed_diff=self._ENVELOPE,
        )

    def test_full_file_write_is_not_falsely_blocked(self, monkeypatch) -> None:
        from mcp_server.engine.policies.anti_regression import AntiRegression

        monkeypatch.delenv("CODEVIRA_ANTI_REGRESSION_MODE", raising=False)  # block
        verdict = AntiRegression().evaluate(self._event("Write"), _FixSignals())
        assert verdict.action == "allow", "additive Write must not be revert-blocked"

    def test_edit_still_detects_revert(self, monkeypatch) -> None:
        """Proof the guard is Write-specific, not a blanket disable: the
        same envelope as an Edit still runs the revert heuristic."""
        from mcp_server.engine.policies.anti_regression import AntiRegression

        monkeypatch.delenv("CODEVIRA_ANTI_REGRESSION_MODE", raising=False)
        verdict = AntiRegression().evaluate(self._event("Edit"), _FixSignals())
        assert verdict.action in ("block", "warn"), "Edit revert detection intact"
