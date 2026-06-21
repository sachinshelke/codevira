"""Cross-project decision search (v3.6.0).

`decisions_store.search_all_projects` (and the `all_projects=True` path of the
`search_decisions` tool) searches every registered project's decision store and
merges the BM25-ranked results, tagged with the project each came from.

Strategy: build two real temp projects with their own `.codevira/decisions.jsonl`,
then mock `enumerate_projects` to point at them — exercising the real per-project
search + merge/rank/tag without depending on global.db internals.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server.paths as core_paths
from mcp_server.storage import decisions_store


def _make_project(tmp_path: Path, name: str, decisions: list[tuple[str, str]]) -> Path:
    """Create a project dir with a .codevira/ and record `decisions`
    (each a (text, file_path) tuple) into its store."""
    root = tmp_path / name
    (root / ".codevira").mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    core_paths.set_project_dir(root)
    core_paths.invalidate_data_dir_cache()
    for text, fp in decisions:
        decisions_store.record(decision=text, file_path=fp)
    return root


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Two registered projects, each with a 'retry'-themed decision."""
    p1 = _make_project(
        tmp_path,
        "alpha",
        [("retry uses exponential backoff with jitter", "net.py")],
    )
    p2 = _make_project(
        tmp_path,
        "beta",
        [
            ("retry policy is three attempts then fail fast", "api.py"),
            ("cache uses redis with a 60s ttl", "cache.py"),
        ],
    )

    entries = [
        SimpleNamespace(canonical_path=str(p1), name="alpha-svc"),
        SimpleNamespace(canonical_path=str(p2), name="beta-svc"),
    ]
    monkeypatch.setattr(
        "mcp_server._project_inventory.enumerate_projects", lambda: entries
    )
    return p1, p2


class TestSearchAllProjects:
    def test_merges_and_tags_by_project(self, two_projects):
        p1, p2 = two_projects
        results = decisions_store.search_all_projects("retry", limit=10)

        # The 'retry' decision from EACH project surfaces.
        projects = {r["project"] for r in results}
        assert projects == {"alpha-svc", "beta-svc"}
        # Every row is tagged with its on-disk path.
        by_project = {r["project"]: r for r in results}
        assert by_project["alpha-svc"]["project_path"] == str(p1)
        assert by_project["beta-svc"]["project_path"] == str(p2)
        # The non-matching 'cache' decision in beta is NOT returned.
        assert all("cache" not in (r.get("decision") or "") for r in results)

    def test_ranked_by_score_across_projects(self, two_projects):
        results = decisions_store.search_all_projects("retry", limit=10)
        # BM25 score is a distance — ascending (best first) across the merge.
        scores = [r["score"] for r in results]
        assert scores == sorted(scores)

    def test_limit_caps_global_results(self, two_projects):
        results = decisions_store.search_all_projects("retry", limit=1)
        assert len(results) == 1

    def test_empty_query_returns_empty(self, two_projects):
        assert decisions_store.search_all_projects("   ", limit=5) == []


class TestSkipsBadProjects:
    def test_missing_decisions_file_and_none_path_skipped(self, tmp_path, monkeypatch):
        good = _make_project(tmp_path, "good", [("retry uses backoff", "x.py")])
        ghost = tmp_path / "ghost"  # never created on disk
        entries = [
            SimpleNamespace(canonical_path=str(good), name="good"),
            SimpleNamespace(canonical_path=str(ghost), name="ghost"),  # no .codevira
            SimpleNamespace(canonical_path=None, name="slug-only"),  # unresolvable
        ]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        results = decisions_store.search_all_projects("retry", limit=10)
        # Only the good project contributes; the bad entries are skipped cleanly.
        assert {r["project"] for r in results} == {"good"}

    def test_deduplicates_repeated_root(self, tmp_path, monkeypatch):
        root = _make_project(tmp_path, "solo", [("retry uses backoff", "x.py")])
        # Same canonical_path twice (slug + db row) → counted once.
        entries = [
            SimpleNamespace(canonical_path=str(root), name="solo"),
            SimpleNamespace(canonical_path=str(root), name="solo"),
        ]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        results = decisions_store.search_all_projects("retry", limit=10)
        assert len(results) == 1


class TestToolSurface:
    def test_tool_all_projects_includes_project_field(self, two_projects):
        from mcp_server.tools.search import search_decisions

        out = search_decisions("retry", all_projects=True, limit=10)
        assert out["count"] >= 2
        # Compact rows carry the project tag when all_projects=True.
        assert all("project" in r for r in out["results"])

    def test_tool_default_single_project_has_no_project_field(self, two_projects):
        from mcp_server.tools.search import search_decisions

        # Default (current project = beta, the last one set up). No project tag.
        out = search_decisions("retry", limit=10)
        assert out["count"] >= 1
        assert all("project" not in r for r in out["results"])
