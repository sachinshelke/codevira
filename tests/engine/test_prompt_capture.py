"""
test_prompt_capture.py — v3.3.0 Phase 4: prompt capture + distill nudge.

Real filesystem, no mocks on the capture store (same posture as
test_session_log_enforcer.py).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.prompt_capture import (
    _DEFAULT_NUDGE_THRESHOLD,
    _MAX_PROMPT_CHARS,
    _PROMPTS_MAX_BYTES,
    PromptCapture,
    clear_pending,
    count_pending,
    prompts_path,
    read_pending,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEVIRA_PROMPT_CAPTURE_MODE", raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".codevira-cache").mkdir()
    return root


def _prompt_event(root: Path, text: str | None, *, session_id="s1") -> HookEvent:
    return HookEvent(
        event_type=EventType.USER_PROMPT_SUBMIT,
        project_root=root,
        session_id=session_id,
        timestamp=time.time(),
        prompt_text=text,
    )


def _stop_event(root: Path) -> HookEvent:
    return HookEvent(
        event_type=EventType.STOP,
        project_root=root,
        session_id="s1",
        timestamp=time.time(),
    )


class TestCapture:
    def test_records_prompt(self, project_root: Path) -> None:
        policy = PromptCapture()
        verdict = policy.evaluate(
            _prompt_event(project_root, "keep answers short"), None
        )
        assert verdict.is_allowing()
        rows = read_pending(project_root)
        assert len(rows) == 1
        assert rows[0]["prompt"] == "keep answers short"
        assert rows[0]["session_id"] == "s1"

    def test_empty_prompt_not_recorded(self, project_root: Path) -> None:
        policy = PromptCapture()
        policy.evaluate(_prompt_event(project_root, "   "), None)
        policy.evaluate(_prompt_event(project_root, None), None)
        assert count_pending(project_root) == 0

    def test_long_prompt_truncated(self, project_root: Path) -> None:
        policy = PromptCapture()
        policy.evaluate(_prompt_event(project_root, "x" * 10_000), None)
        rows = read_pending(project_root)
        assert len(rows[0]["prompt"]) <= _MAX_PROMPT_CHARS

    def test_secrets_scrubbed(self, project_root: Path) -> None:
        policy = PromptCapture()
        policy.evaluate(
            _prompt_event(project_root, "use key AKIAIOSFODNN7EXAMPLE please"),
            None,
        )
        rows = read_pending(project_root)
        assert "AKIAIOSFODNN7EXAMPLE" not in rows[0]["prompt"]
        assert "<redacted:aws-akia>" in rows[0]["prompt"]

    def test_rotation_at_cap(self, project_root: Path) -> None:
        path = prompts_path(project_root)
        filler = json.dumps({"prompt": "filler"})
        path.write_text(
            (filler + "\n") * ((_PROMPTS_MAX_BYTES // len(filler)) + 1),
            encoding="utf-8",
        )
        PromptCapture().evaluate(_prompt_event(project_root, "fresh"), None)
        assert path.with_suffix(path.suffix + ".1").exists()
        assert count_pending(project_root) == 1

    def test_off_mode_captures_nothing(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_PROMPT_CAPTURE_MODE", "off")
        PromptCapture().evaluate(_prompt_event(project_root, "hello"), None)
        assert count_pending(project_root) == 0


class TestStopNudge:
    def _fill(self, root: Path, n: int) -> None:
        policy = PromptCapture()
        for i in range(n):
            policy.evaluate(_prompt_event(root, f"instruction number {i}"), None)

    def test_below_threshold_silent(self, project_root: Path) -> None:
        self._fill(project_root, _DEFAULT_NUDGE_THRESHOLD - 1)
        verdict = PromptCapture().evaluate(_stop_event(project_root), None)
        assert verdict.is_allowing()

    def test_at_threshold_nudges(self, project_root: Path) -> None:
        self._fill(project_root, _DEFAULT_NUDGE_THRESHOLD)
        verdict = PromptCapture().evaluate(_stop_event(project_root), None)
        assert verdict.action == "warn"
        assert "distill_preferences" in (verdict.message or "")

    def test_cooldown_suppresses_second_nudge(self, project_root: Path) -> None:
        self._fill(project_root, _DEFAULT_NUDGE_THRESHOLD)
        policy = PromptCapture()
        first = policy.evaluate(_stop_event(project_root), None)
        second = policy.evaluate(_stop_event(project_root), None)
        assert first.action == "warn"
        assert second.is_allowing()
        assert second.metadata.get("reason") == "cooldown"


class TestHelpers:
    def test_clear_pending(self, project_root: Path) -> None:
        PromptCapture().evaluate(_prompt_event(project_root, "hi there"), None)
        assert count_pending(project_root) == 1
        clear_pending(project_root)
        assert count_pending(project_root) == 0
        assert prompts_path(project_root).exists()  # emptied, not deleted

    def test_registered_by_default(self) -> None:
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        assert "prompt_capture" in {p.name for p in registered_policies()}
        reset_policies()
        register_default_policies()
