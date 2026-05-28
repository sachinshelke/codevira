"""
Tests for mcp_server.storage.decisions_store.

Scope: behaviors and contracts owned directly by decisions_store
(amendment overlay delegation, session_id default generation).
Higher-level surfaces — record_decision MCP tool, conflict checks,
session-context aggregation — are exercised in
``tests/test_tools_learning.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import decisions_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh temp project rooted at ``tmp_path`` so decisions land in an
    isolated ``.codevira/decisions.jsonl``."""
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


class TestDefaultSessionId:
    """v3.0.1 fix: prior to this, the unattributed default was the
    literal string ``"ad-hoc"``. Every concurrent IDE that didn't pass
    a slug collided into one bucket — masking session boundaries and
    breaking the v3.1.0 working-memory design which keys by session_id.
    """

    _PATTERN = re.compile(r"^ad-hoc-[0-9a-f]{6}$")

    def test_helper_returns_unique_slug_each_call(self) -> None:
        """Each call generates a fresh random suffix (per-call
        uniqueness — chosen so that two unattributed writes can be
        distinguished post-hoc even within one process).
        """
        slug1 = decisions_store.default_session_id()
        slug2 = decisions_store.default_session_id()
        assert slug1 != slug2
        assert self._PATTERN.match(slug1), slug1
        assert self._PATTERN.match(slug2), slug2

    def test_helper_never_returns_literal_ad_hoc(self) -> None:
        """Catches a regression where someone short-circuits the helper
        back to the old literal.
        """
        for _ in range(20):
            assert decisions_store.default_session_id() != "ad-hoc"

    def test_record_without_session_id_uses_new_default(self, project: Path) -> None:
        """End-to-end: two record() calls with no session_id MUST yield
        distinct on-disk session_id values. This is the
        cross-IDE-collision fix in its simplest form.
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record(decision="First decision under no slug")
        decisions_store.record(decision="Second decision under no slug")

        raw = jsonl_store.read_all(paths.decisions_path())
        # Only count base records (amendments share a session_id with the
        # base they amend; v3.0.0 records have no amendments at this
        # point in the test).
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert len(sessions) == 2
        assert sessions[0] != sessions[1], (
            f"two unattributed record() calls produced the same "
            f"session_id ({sessions[0]!r}); v3.0.1 regression"
        )
        assert all(self._PATTERN.match(s) for s in sessions), sessions

    def test_record_with_explicit_session_id_preserved(self, project: Path) -> None:
        """Explicit session_id from the caller wins over the default
        generator. (No silent overwrite — agents that DO group their
        work keep their grouping.)
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record(decision="Grouped decision A", session_id="morning-auth")
        decisions_store.record(decision="Grouped decision B", session_id="morning-auth")

        raw = jsonl_store.read_all(paths.decisions_path())
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert sessions == ["morning-auth", "morning-auth"]

    def test_record_many_unique_slug_per_record(self, project: Path) -> None:
        """``record_many`` with mixed explicit + missing session_ids:
        each missing gets its own unique slug; explicit ones preserved.
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record_many(
            [
                {"decision": "Explicit slug A", "session_id": "explicit-1"},
                {"decision": "No slug 1"},
                {"decision": "No slug 2"},
                {"decision": "Explicit slug B", "session_id": "explicit-2"},
            ]
        )

        raw = jsonl_store.read_all(paths.decisions_path())
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert sessions[0] == "explicit-1"
        assert self._PATTERN.match(sessions[1])
        assert self._PATTERN.match(sessions[2])
        assert sessions[1] != sessions[2], "two unattributed siblings collided"
        assert sessions[3] == "explicit-2"
