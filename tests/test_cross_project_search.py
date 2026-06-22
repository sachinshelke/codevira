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

    def test_interleaves_by_rank_for_fair_representation(self, tmp_path, monkeypatch):
        # alpha has THREE 'retry' matches, beta only one. Raw-score sorting could
        # let alpha's matches monopolize the top; rank-interleave must give each
        # project's #1 a top slot (BM25 scores aren't comparable across corpora).
        a = _make_project(
            tmp_path,
            "alpha",
            [
                ("retry alpha one", "a.py"),
                ("retry alpha two", "b.py"),
                ("retry alpha three", "c.py"),
            ],
        )
        b = _make_project(tmp_path, "beta", [("retry beta one", "d.py")])
        entries = [
            SimpleNamespace(canonical_path=str(a), name="alpha"),
            SimpleNamespace(canonical_path=str(b), name="beta"),
        ]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        results = decisions_store.search_all_projects("retry", limit=2)
        # Top-2 spans BOTH projects (each project's #1), not alpha's top-2.
        assert {r["project"] for r in results} == {"alpha", "beta"}
        # The internal _rank key must not leak to callers.
        assert all("_rank" not in r for r in results)

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


class TestPathHardening:
    def test_dedup_normalizes_symlinked_paths(self, tmp_path, monkeypatch):
        # Same dir reached two ways: the registry stores a SYMLINK path while
        # the cwd fallback uses the resolved real path. Must dedup to one result.
        real = _make_project(tmp_path, "real", [("retry uses backoff", "x.py")])
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlinks unsupported on this platform")
        entries = [SimpleNamespace(canonical_path=str(link), name="via-link")]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        # Current project (set by _make_project) is the REAL path.
        results = decisions_store.search_all_projects("retry", limit=10)
        assert len(results) == 1  # not double-counted via the symlink alias

    def test_invalid_project_root_skipped(self, tmp_path, monkeypatch):
        good = _make_project(tmp_path, "good", [("retry uses backoff", "x.py")])
        bad = _make_project(tmp_path, "bad", [("retry also here", "y.py")])
        entries = [
            SimpleNamespace(canonical_path=str(good), name="good"),
            SimpleNamespace(canonical_path=str(bad), name="bad"),
        ]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        # Flag `bad` as an unsafe root (e.g. $HOME / system dir).
        import mcp_server.paths as P

        real_guard = P.is_invalid_project_root
        monkeypatch.setattr(
            P,
            "is_invalid_project_root",
            lambda root: "unsafe" if str(root).endswith("bad") else real_guard(root),
        )
        results = decisions_store.search_all_projects("retry", limit=10)
        assert {r["project"] for r in results} == {"good"}


class TestCurrentProjectAlwaysIncluded:
    def test_unregistered_current_project_is_searched(self, tmp_path, monkeypatch):
        # Current project has decisions but is NOT in the registry at all.
        _make_project(tmp_path, "current", [("retry uses backoff", "x.py")])
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: []
        )
        results = decisions_store.search_all_projects("retry", limit=10)
        # The current project is still searched via the cwd fallback.
        assert len(results) == 1
        assert "retry" in results[0]["decision"]

    def test_registered_current_project_not_double_counted(self, tmp_path, monkeypatch):
        root = _make_project(tmp_path, "current", [("retry uses backoff", "x.py")])
        entries = [SimpleNamespace(canonical_path=str(root), name="current-svc")]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        results = decisions_store.search_all_projects("retry", limit=10)
        assert len(results) == 1  # searched once, not twice
        # Registered name wins (enumerated before the cwd fallback runs).
        assert results[0]["project"] == "current-svc"


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
