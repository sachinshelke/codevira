"""
test_mcp_roundtrip.py — v2.1.2 hardening (Test A).

Promotes the post-release smoke harness into a permanent pytest suite.
Exercises EVERY v2.1.2 MCP tool through the real ``call_tool`` dispatcher
against a freshly-bootstrapped project. Asserts response shapes,
filter semantics, idempotency contracts, and the cross-tool interactions
unit tests can't easily catch.

History: caught the bulk_import_phases-skips-placeholder bug
(v2.1.2 38447fe) the FIRST time we ran the harness, after dozens of
unit tests had passed without noticing. Lesson: unit tests verify each
tool in isolation; this suite verifies them as a system.

Marked ``integration`` so it can be skipped in fast unit runs:
    pytest tests/                                  # everything
    pytest tests/ -m "not integration"             # fast unit-only
    pytest tests/integration -m integration -v     # integration only
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def isolated_codevira(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap an empty codevira project under ``tmp_path``.

    Sets HOME so global.db lands under the temp tree, points
    mcp_server.paths at the temp project, and lazily ensures the data
    dir + graph.db exist. Returns the project path.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'roundtrip-smoke'\nversion = '0.0.1'\n"
    )

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    # Set fake home for the paths module too — get_global_home() reads
    # ``~/.codevira`` which we want to stay in the temp tree.
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home / ".codevira")

    # Best-effort bootstrap. Many MCP tools lazy-init their own state,
    # so a missing data dir at this point is non-fatal.
    try:
        from mcp_server.auto_init import ensure_project_initialized

        ensure_project_initialized()
    except Exception:
        pass

    return project


def _get_call_tool():
    """Locate the @server.call_tool() decorated handler in server.py.

    The MCP Server class registers handlers internally; the easiest path
    is to call the module-level decorated function directly.
    """
    import mcp_server.server as srv_mod

    for name in dir(srv_mod):
        v = getattr(srv_mod, name)
        if callable(v) and getattr(v, "__name__", "") == "call_tool":
            return v
    raise RuntimeError("could not locate call_tool in server module")


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | str:
    """Invoke an MCP tool via the dispatcher; unpack the JSON result."""
    fn = _get_call_tool()
    res = asyncio.run(fn(name, arguments))
    if not res:
        return {}
    txt = res[0].text
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


# ---------------------------------------------------------------------
# Tests — one logical test per MCP tool / cross-tool interaction
# ---------------------------------------------------------------------


class TestV212MCPRoundTrip:
    """End-to-end verification of v2.1.2 MCP tools via call_tool dispatch."""

    def test_record_decisions_batch(self, isolated_codevira):
        """Item 23: batch record returns count + ids + no errors."""
        r = call_tool(
            "record_decisions",
            {
                "decisions": [
                    {
                        "decision": "Use bcrypt over argon2 for password hashing",
                        "file_path": "auth.py",
                        "do_not_revert": True,
                        "tags": ["security", "auth"],
                    },
                    {
                        "decision": "Prefer named exports over default exports in TS",
                        "file_path": "core.ts",
                        "tags": ["typescript", "style"],
                    },
                    {
                        "decision": "Always use context.Context as first arg in Go funcs",
                        "file_path": "main.go",
                        "tags": ["go"],
                    },
                ],
            },
        )
        assert r.get("count") == 3, r
        assert len(r.get("recorded", [])) == 3
        assert not r.get("errors")

    def test_list_decisions_filters_protected_only(self, isolated_codevira):
        """Item 11: protected_only filter narrows the result set."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "Locked decision A", "do_not_revert": True},
                    {"decision": "Not-locked decision B", "do_not_revert": False},
                    {"decision": "Locked decision C", "do_not_revert": True},
                ]
            },
        )
        r = call_tool("list_decisions", {"limit": 50, "protected_only": True})
        assert r.get("count") == 2, r
        for d in r.get("decisions", []):
            assert d.get("do_not_revert") is True

    def test_list_decisions_filters_tags_intersection(self, isolated_codevira):
        """Item 27: tag filter is AND-intersection."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "X", "tags": ["a", "b"]},
                    {"decision": "Y", "tags": ["a", "c"]},
                    {"decision": "Z", "tags": ["b", "c"]},
                ]
            },
        )
        r = call_tool("list_decisions", {"limit": 50, "tags": ["a", "b"]})
        # Only "X" has BOTH a and b
        assert r.get("count") == 1, r
        assert r["decisions"][0]["decision"] == "X"

    def test_list_tags_returns_counts(self, isolated_codevira):
        """Item 27: list_tags enumerates with counts."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "A", "tags": ["security"]},
                    {"decision": "B", "tags": ["security", "auth"]},
                    {"decision": "C", "tags": ["go"]},
                ]
            },
        )
        r = call_tool("list_tags", {})
        tags = {t["tag"]: t["count"] for t in r.get("tags", [])}
        assert tags.get("security") == 2
        assert tags.get("auth") == 1
        assert tags.get("go") == 1

    def test_check_conflict_detects_conflict_with_protected(self, isolated_codevira):
        """Item 20: check_conflict flags conflicts vs do_not_revert decisions.

        Requires semantic infra. Skip cleanly if chromadb/torch can't
        load in the test env (Tier-2 graceful degradation).
        """
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {
                        "decision": "Use bcrypt over argon2 for password hashing",
                        "file_path": "auth.py",
                        "do_not_revert": True,
                    },
                ]
            },
        )
        r = call_tool(
            "check_conflict",
            {
                "decision_text": "Switch from bcrypt to scrypt for hashing",
                "file_path": "auth.py",
            },
        )
        if r.get("note", "").startswith("semantic search unavailable"):
            pytest.skip("semantic infra unavailable in this env")
        assert "status" in r
        # When semantic works, this should be a conflict (similarity below threshold
        # to a do_not_revert decision)
        if r["status"] == "conflict":
            assert len(r.get("conflicts", [])) >= 1
            assert all(c.get("do_not_revert") for c in r["conflicts"])

    def test_supersede_decision_hides_old_surfaces_new(self, isolated_codevira):
        """Item 26: supersede flips is_superseded; list_decisions filters."""
        r = call_tool(
            "record_decisions",
            {
                "decisions": [
                    {
                        "decision": "Use bcrypt",
                        "file_path": "auth.py",
                        "do_not_revert": True,
                        "tags": ["sec"],
                    },
                ]
            },
        )
        old_id = r["recorded"][0]
        r = call_tool(
            "supersede_decision",
            {
                "old_id": old_id,
                "new_decision": "Use argon2id for password hashing",
                "reason": "OWASP recommendation changed",
                "file_path": "auth.py",
            },
        )
        assert r.get("success") is True, r
        new_id = r["new_id"]
        assert new_id != old_id

        # Default list hides superseded
        r = call_tool("list_decisions", {"limit": 50, "tags": ["sec"]})
        ids_default = {d["id"] for d in r.get("decisions", [])}
        assert (
            old_id not in ids_default
        ), "superseded decision should be hidden by default"

        # include_superseded surfaces both
        r = call_tool("list_decisions", {"limit": 50, "include_superseded": True})
        ids_all = {d["id"] for d in r.get("decisions", [])}
        assert old_id in ids_all
        assert new_id in ids_all

    def test_bulk_import_phases_replaces_pristine_placeholder(self, isolated_codevira):
        """Item 29 + Item 18 interaction (caught by THIS suite first time
        the smoke harness ran). Importing phase=1 on a fresh project
        with the bootstrap placeholder must NOT silently skip.
        """
        r = call_tool(
            "bulk_import_phases",
            {
                "phases": [
                    {
                        "number": 1,
                        "name": "Foundations",
                        "status": "done",
                        "completed_at": "2026-01-15",
                    },
                    {
                        "number": 2,
                        "name": "Auth",
                        "status": "done",
                        "completed_at": "2026-02-20",
                        "git_ref": "abc1234",
                    },
                    {"number": 3, "name": "Search", "status": "done"},
                    {"number": 4, "name": "Polish", "status": "upcoming"},
                ],
            },
        )
        assert r.get("imported") == 4, (
            f"bulk_import skipping phase=1 indicates the placeholder-replacement "
            f"logic regressed: {r}"
        )
        assert not r.get("errors")

    def test_bulk_import_phases_idempotent(self, isolated_codevira):
        """Item 29: re-importing the same phase is a no-op."""
        call_tool(
            "bulk_import_phases",
            {
                "phases": [
                    {"number": 5, "name": "Already imported", "status": "done"},
                ]
            },
        )
        r = call_tool(
            "bulk_import_phases",
            {
                "phases": [
                    {"number": 5, "name": "Already imported", "status": "done"},
                ]
            },
        )
        assert r.get("imported") == 0
        assert r.get("skipped_existing") == 1

    def test_search_decisions_exposes_threshold_used(self, isolated_codevira):
        """Item 1: search response carries threshold_used regardless of mode."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "Use bcrypt", "file_path": "auth.py"},
                ]
            },
        )
        r = call_tool("search_decisions", {"query": "bcrypt"})
        assert "threshold_used" in r, r
        assert "retrieval" in r

    def test_search_decisions_since_filter(self, isolated_codevira):
        """Item 25: since= in the future returns 0 results."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "Old decision"},
                ]
            },
        )
        r = call_tool(
            "search_decisions",
            {
                "query": "decision",
                "since": "2099-01-01",
            },
        )
        assert r.get("count") == 0, r

    def test_summary_only_returns_slim_payload(self, isolated_codevira):
        """Item 28: summary_only=True drops everything except id+summary+score+do_not_revert."""
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {
                        "decision": "Some decision",
                        "file_path": "a.py",
                        "context": "some context",
                        "do_not_revert": True,
                    },
                ]
            },
        )
        r = call_tool(
            "search_decisions",
            {
                "query": "decision",
                "summary_only": True,
            },
        )
        if r.get("count", 0) > 0:
            first = r["results"][0]
            allowed_keys = {"id", "summary", "score", "do_not_revert"}
            assert set(first.keys()) <= allowed_keys, (
                f"summary_only response leaked extra keys: "
                f"{set(first.keys()) - allowed_keys}"
            )
        assert r.get("mode") == "summary_only"

    def test_write_session_logs_batch(self, isolated_codevira):
        """Item 24: batch session log returns count + session_ids."""
        r = call_tool(
            "write_session_logs",
            {
                "logs": [
                    {
                        "session_id": "rt-s1",
                        "task": "task A",
                        "phase": "1",
                        "decisions": [
                            {"file_path": "a.py", "decision": "A", "context": ""}
                        ],
                    },
                    {
                        "session_id": "rt-s2",
                        "task": "task B",
                        "phase": "2",
                        "decisions": [],
                    },
                ]
            },
        )
        assert r.get("count") == 2, r
        assert len(r.get("session_ids", [])) == 2

    def test_record_decision_accepts_non_bool_do_not_revert(self, isolated_codevira):
        """Item 30: int 1 / int 0 for do_not_revert coerces cleanly (no crash)."""
        r = call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "with int 1", "do_not_revert": 1},
                    {"decision": "with int 0", "do_not_revert": 0},
                ]
            },
        )
        assert r.get("count") == 2, r

    def test_search_decisions_carries_semantic_warning_when_chroma_unavailable(
        self,
        isolated_codevira,
    ):
        """Issue #10 / Tier 2: when chromadb / torch fails to load, the
        response carries a clear ``_semantic_warning`` explaining why.

        Hard to force-fail chromadb in a clean test env; we skip if
        semantic IS available (the warning won't surface). When it IS
        unavailable, the field must be present.
        """
        call_tool(
            "record_decisions",
            {
                "decisions": [
                    {"decision": "Some decision"},
                ]
            },
        )
        r = call_tool("search_decisions", {"query": "decision"})
        # If chromadb loaded fine, there's nothing to assert here.
        if "_semantic_warning" not in r:
            pytest.skip("semantic infra loaded cleanly; no warning to verify")
        assert isinstance(r["_semantic_warning"], str)
        assert len(r["_semantic_warning"]) > 0
