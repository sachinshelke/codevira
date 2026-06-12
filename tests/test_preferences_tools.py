"""
test_preferences_tools.py — v3.3.0 Phase 4: distill_preferences + search_preferences.

FakeSession mirrors the sampling test harness in test_reflections.py.
Global.db is real sqlite in tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from mcp_server.engine.policies.prompt_capture import count_pending, prompts_path
from mcp_server.tools.preferences import (
    _parse_preferences,
    distill_preferences_async,
    search_preferences,
)


class _FakeResult:
    def __init__(self, text: str) -> None:
        class _Content:
            pass

        self.content = _Content()
        self.content.text = text
        self.model = "test-llm"


class _FakeSession:
    def __init__(self, *, advertise=True, canned="[]") -> None:
        if advertise:

            class _Caps:
                sampling = object()

            class _Params:
                capabilities = _Caps()

            self.client_params = _Params()
        else:
            self.client_params = None
        self._canned = canned
        self.create_message_called = 0

    async def create_message(self, *, messages, max_tokens, **kw):
        self.create_message_called += 1
        return _FakeResult(self._canned)


@pytest.fixture(autouse=True)
def _stub_sampling_message():
    import sys

    mt = sys.modules.get("mcp.types")
    if mt is None or hasattr(mt, "SamplingMessage"):
        yield
        return

    class _Stub:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mt.SamplingMessage = _Stub
    try:
        yield
    finally:
        delattr(mt, "SamplingMessage")


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".codevira-cache").mkdir()
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
    monkeypatch.setattr(
        "mcp_server.paths.get_global_db_path",
        lambda: tmp_path / "home" / "global.db",
    )
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(root)
    return root


def _capture(root: Path, prompts: list[str]) -> None:
    path = prompts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for p in prompts:
            fh.write(json.dumps({"ts": 1.0, "session_id": "s1", "prompt": p}) + "\n")


_LLM_JSON = json.dumps(
    [
        {
            "category": "communication",
            "signal": "keep answers short",
            "example": "keep answers always short",
        }
    ]
)


def _run(coro):
    return asyncio.run(coro)


class TestDistill:
    def test_no_prompts_is_noop(self, project) -> None:
        result = _run(distill_preferences_async(server_session=None))
        assert result["pending_prompts"] == 0
        assert result["preferences_extracted"] == 0

    def test_no_session_returns_stub_with_prompt(self, project) -> None:
        _capture(project, ["keep answers short"])
        result = _run(distill_preferences_async(server_session=None))
        assert result["sampling_supported"] is False
        assert result["sampling_error"] == "no_server_session"
        assert "keep answers short" in result["rendered_prompt"]
        assert count_pending(project) == 1  # untouched on failure

    def test_dry_run_extracts_but_does_not_persist(self, project, tmp_path) -> None:
        _capture(project, ["keep answers short", "do not overexplain"])
        sess = _FakeSession(canned=_LLM_JSON)
        result = _run(distill_preferences_async(server_session=sess, dry_run=True))
        assert result["sampling_supported"] is True
        assert result["preferences_extracted"] == 1
        assert result["preferences_persisted"] == 0
        assert not (tmp_path / "home" / "global.db").exists()
        assert count_pending(project) == 2  # capture kept until persisted

    def test_persists_and_clears_capture(self, project, tmp_path) -> None:
        _capture(project, ["keep answers short"] * 3)
        sess = _FakeSession(canned=_LLM_JSON)
        result = _run(distill_preferences_async(server_session=sess, dry_run=False))
        assert result["preferences_persisted"] == 1
        assert result["capture_file_cleared"] is True
        assert count_pending(project) == 0

        conn = sqlite3.connect(tmp_path / "home" / "global.db")
        rows = conn.execute(
            "SELECT category, signal FROM global_preferences"
        ).fetchall()
        conn.close()
        assert rows == [("communication", "keep answers short")]

    def test_garbage_llm_output_persists_nothing(self, project, tmp_path) -> None:
        _capture(project, ["keep answers short"])
        sess = _FakeSession(canned="I think the user likes brevity!")
        result = _run(distill_preferences_async(server_session=sess, dry_run=False))
        assert result["preferences_extracted"] == 0
        assert result["preferences_persisted"] == 0
        assert count_pending(project) == 1  # capture kept


class TestParse:
    def test_code_fenced_json(self) -> None:
        fenced = f"```json\n{_LLM_JSON}\n```"
        assert _parse_preferences(fenced)[0]["signal"] == "keep answers short"

    def test_non_list_rejected(self) -> None:
        assert _parse_preferences('{"signal": "x"}') == []

    def test_missing_signal_skipped(self) -> None:
        assert _parse_preferences('[{"category": "communication"}]') == []


class TestSearchPreferences:
    def test_no_db_hint(self, project) -> None:
        result = search_preferences(category="communication")
        assert result["count"] == 0
        assert "hint" in result

    def test_category_filter(self, project, tmp_path) -> None:
        from indexer.global_db import GlobalDB

        db = GlobalDB(tmp_path / "home" / "global.db")
        db.upsert_preference(
            category="communication",
            signal="short answers",
            example=None,
            source_project="proj",
            frequency=5,
        )
        db.upsert_preference(
            category="workflow",
            signal="tests first",
            example=None,
            source_project="proj",
            frequency=2,
        )
        db.close()

        comm = search_preferences(category="communication")
        assert comm["count"] == 1
        assert comm["preferences"][0]["signal"] == "short answers"

        all_prefs = search_preferences()
        assert all_prefs["count"] == 2
        assert all_prefs["preferences"][0]["frequency"] == 5  # freq-desc
