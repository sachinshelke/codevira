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


class TestOriginTagging:
    """v3.1.0 M1: every decision write carries origin: {ide,
    agent_model, host_hash, ts}. Reads tolerate absence on legacy
    v3.0.x records (treated as ide="unknown").
    """

    def test_record_stamps_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(decision="Use rate limiting")

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 1
        origin_field = bases[0].get("origin")
        assert origin_field is not None
        assert origin_field["ide"] == "claude_code"
        assert "host_hash" in origin_field and len(origin_field["host_hash"]) == 12
        assert "ts" in origin_field

    def test_record_many_stamps_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record_many(
            [{"decision": "A"}, {"decision": "B"}, {"decision": "C"}]
        )

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        for r in rows:
            if r.get("_amendment_to_id"):
                continue
            assert r["origin"]["ide"] == "cursor"

    def test_ide_unknown_when_env_unset(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEVIRA_IDE", raising=False)
        decisions_store.record(decision="Anonymous write")

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert bases[0]["origin"]["ide"] == "unknown"

    def test_backcompat_record_without_origin(self, project: Path) -> None:
        """Hand-craft a legacy v3.0.x record (no ``origin`` field) and
        verify every read path tolerates absence. This is the
        regression test for the M1 promise that legacy records read
        as ide="unknown" without crashing.
        """
        from mcp_server.storage import jsonl_store, paths

        legacy = {
            "id": "D000001",
            "ts": "2026-05-01T00:00:00Z",
            "session_id": "ad-hoc",  # the OLD literal default
            "file_path": None,
            "decision": "Legacy decision pre-3.1",
            "context": None,
            "do_not_revert": False,
            "tags": [],
            "supersedes": None,
            "superseded_by": None,
            "outcome": None,
            # NOTE: no "origin" field — legacy 3.0.x shape
        }
        jsonl_store.append(paths.decisions_path(), legacy)

        # Reads via the merged view: legacy record surfaces, origin missing.
        merged = decisions_store._read_merged()
        assert len(merged) == 1
        assert "origin" not in merged[0] or merged[0].get("origin") is None

        # Now write a NEW decision via the dev path — the new one carries origin,
        # legacy doesn't. Both must coexist in subsequent reads.
        decisions_store.record(decision="New decision under 3.1.0")
        merged = decisions_store._read_merged()
        assert len(merged) == 2
        new_rec = next(r for r in merged if "New decision" in r["decision"])
        assert new_rec["origin"]["host_hash"]
        legacy_rec = next(r for r in merged if "Legacy decision" in r["decision"])
        assert legacy_rec.get("origin") is None  # untouched

    def test_search_surfaces_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """decisions_store.search() includes origin per candidate so
        check_conflict can surface provenance."""
        monkeypatch.setenv("CODEVIRA_IDE", "windsurf")
        decisions_store.record(
            decision="Migrate database to PostgreSQL", tags=["db", "migration"]
        )

        hits = decisions_store.search("PostgreSQL migration", limit=5)
        assert len(hits) >= 1
        assert hits[0]["origin"]["ide"] == "windsurf"
