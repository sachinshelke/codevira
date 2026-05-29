"""
Tests for mcp_server.tools.spatial — v3.1.0 M4 Phase 2.

Coverage:
  - spatial_neighborhood: folder-tree default + yaml override
  - spatial_affordances: bundled defaults + project overrides
  - spatial_heat: wraps activity_store.list_top_k_files + since_days
  - spatial_nearby: neighborhood fallback (no graph) + activity ranking
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import activity_store, paths
from mcp_server.tools import spatial


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# Folder-tree + neighborhood
# ──────────────────────────────────────────────────────────────────────


class TestFolderTreeNeighborhood:
    def test_two_component_path(self) -> None:
        assert spatial._folder_tree_neighborhood("mcp_server/storage/foo.py") == (
            "mcp_server/storage"
        )

    def test_long_path_only_top_two(self) -> None:
        assert spatial._folder_tree_neighborhood("a/b/c/d/e.py") == "a/b"

    def test_single_component_is_root(self) -> None:
        assert spatial._folder_tree_neighborhood("README.md") == "<root>"

    def test_empty_path_is_root(self) -> None:
        assert spatial._folder_tree_neighborhood("") == "<root>"


class TestSpatialNeighborhood:
    def test_default_folder_tree(self, project: Path) -> None:
        r = spatial.spatial_neighborhood("mcp_server/storage/foo.py")
        assert r["neighborhood_id"] == "mcp_server/storage"

    def test_yaml_override_wins(self, project: Path) -> None:
        # Override: place anything matching mcp_server/storage/**.py
        # into the 'persistence' neighborhood.
        override = project / ".codevira" / "neighborhoods.yaml"
        override.write_text(
            "persistence:\n"
            "  - mcp_server/storage/*.py\n"
            "engine:\n"
            "  - mcp_server/engine/*.py\n"
        )
        r = spatial.spatial_neighborhood("mcp_server/storage/foo.py")
        assert r["neighborhood_id"] == "persistence"
        r2 = spatial.spatial_neighborhood("mcp_server/engine/x.py")
        assert r2["neighborhood_id"] == "engine"

    def test_override_fallthrough_to_folder_tree(self, project: Path) -> None:
        """A file matching no override pattern still gets a folder-tree
        neighborhood — the override re-labels, it doesn't gate."""
        override = project / ".codevira" / "neighborhoods.yaml"
        override.write_text("persistence:\n  - mcp_server/storage/*.py\n")
        r = spatial.spatial_neighborhood("indexer/foo.py")
        assert r["neighborhood_id"] == "indexer"

    def test_malformed_override_falls_through(self, project: Path) -> None:
        override = project / ".codevira" / "neighborhoods.yaml"
        override.write_text("[ this is not valid yaml :")
        # Should not raise; falls back to folder-tree.
        r = spatial.spatial_neighborhood("mcp_server/storage/foo.py")
        assert r["neighborhood_id"] == "mcp_server/storage"

    def test_members_from_activity_log(self, project: Path) -> None:
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        activity_store.add("indexer/x.py", kind="edit")
        r = spatial.spatial_neighborhood("mcp_server/storage/a.py")
        assert r["neighborhood_id"] == "mcp_server/storage"
        members = set(r["members"])
        assert "mcp_server/storage/a.py" in members
        assert "mcp_server/storage/b.py" in members
        assert "indexer/x.py" not in members


# ──────────────────────────────────────────────────────────────────────
# Affordances
# ──────────────────────────────────────────────────────────────────────


class TestSpatialAffordances:
    def test_bundled_tools_pattern(self, project: Path) -> None:
        r = spatial.spatial_affordances("mcp_server/tools/foo.py")
        assert "add_tool" in r["affordances"]
        assert "write_test" in r["affordances"]

    def test_bundled_storage_pattern(self, project: Path) -> None:
        r = spatial.spatial_affordances("mcp_server/storage/foo.py")
        assert "add_store" in r["affordances"]

    def test_test_files_get_write_test_affordance(self, project: Path) -> None:
        r = spatial.spatial_affordances("tests/test_something.py")
        assert "write_test" in r["affordances"]

    def test_no_match_returns_empty(self, project: Path) -> None:
        r = spatial.spatial_affordances("random/file.xyz")
        assert r["affordances"] == []
        assert r["count"] == 0

    def test_project_override_unions_with_bundled(self, project: Path) -> None:
        override = project / ".codevira" / "affordances.yaml"
        override.write_text(
            "- pattern: 'mcp_server/storage/*.py'\n"
            "  affordances: ['custom_affordance']\n"
        )
        r = spatial.spatial_affordances("mcp_server/storage/foo.py")
        # Both bundled affordances (add_store, write_test) AND the project
        # affordance (custom_affordance) surface.
        assert "add_store" in r["affordances"]
        assert "custom_affordance" in r["affordances"]

    def test_empty_path_returns_empty(self, project: Path) -> None:
        r = spatial.spatial_affordances("")
        assert r["affordances"] == []


# ──────────────────────────────────────────────────────────────────────
# spatial_heat
# ──────────────────────────────────────────────────────────────────────


class TestSpatialHeat:
    def test_returns_ranked_files(self, project: Path) -> None:
        for _ in range(3):
            activity_store.add("hot.py", kind="edit")
        activity_store.add("cool.py", kind="edit")
        r = spatial.spatial_heat(top_k=5)
        names = [h["node_id"] for h in r["hits"]]
        assert names[0] == "hot.py"
        assert "cool.py" in names

    def test_top_k_caps(self, project: Path) -> None:
        for i in range(10):
            activity_store.add(f"f{i}.py", kind="edit")
        r = spatial.spatial_heat(top_k=3)
        assert r["count"] == 3

    def test_empty_returns_empty(self, project: Path) -> None:
        r = spatial.spatial_heat()
        assert r["count"] == 0

    def test_since_days_filter(self, project: Path) -> None:
        from datetime import datetime, timedelta, timezone

        from mcp_server.storage import jsonl_store

        # Stale row.
        old = datetime.now(timezone.utc) - timedelta(days=45)
        jsonl_store.append(
            paths.activity_path(),
            {
                "id": "A000001",
                "ts": old.isoformat(),
                "node_id": "stale.py",
                "kind": "edit",
                "_schema_v": 1,
            },
        )
        # Fresh row.
        activity_store.add("fresh.py", kind="edit")
        r = spatial.spatial_heat(since_days=30)
        names = {h["node_id"] for h in r["hits"]}
        assert "fresh.py" in names
        assert "stale.py" not in names


# ──────────────────────────────────────────────────────────────────────
# spatial_nearby (neighborhood-only fallback when graph missing)
# ──────────────────────────────────────────────────────────────────────


class TestSpatialNearby:
    def test_no_graph_uses_neighborhood_only(self, project: Path) -> None:
        # No graph.sqlite exists; BFS falls back. Same-neighborhood
        # files still surface via activity log.
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        activity_store.add("indexer/x.py", kind="edit")

        r = spatial.spatial_nearby("mcp_server/storage/a.py", k=10)
        nearby_paths = {h["file_path"] for h in r["hits"]}
        # b.py is in the same neighborhood; x.py isn't.
        assert "mcp_server/storage/b.py" in nearby_paths
        assert "indexer/x.py" not in nearby_paths

    def test_originating_file_excluded(self, project: Path) -> None:
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        r = spatial.spatial_nearby("mcp_server/storage/a.py")
        paths_returned = {h["file_path"] for h in r["hits"]}
        assert "mcp_server/storage/a.py" not in paths_returned

    def test_ranks_by_activity_count(self, project: Path) -> None:
        # b.py has 3 edits, c.py has 1 edit; both same neighborhood as a.py.
        for _ in range(3):
            activity_store.add("mcp_server/storage/b.py", kind="edit")
        activity_store.add("mcp_server/storage/c.py", kind="edit")
        # Originating file is a.py.
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        r = spatial.spatial_nearby("mcp_server/storage/a.py", k=5)
        # b.py should outrank c.py via higher visit_count_30d.
        names = [h["file_path"] for h in r["hits"]]
        assert names.index("mcp_server/storage/b.py") < names.index(
            "mcp_server/storage/c.py"
        )

    def test_empty_query_returns_empty(self, project: Path) -> None:
        r = spatial.spatial_nearby("", k=5)
        assert r["hits"] == []

    def test_isolated_file_returns_empty(self, project: Path) -> None:
        # File with no neighborhood-mates and no graph entries.
        r = spatial.spatial_nearby("isolated/lonely.py")
        assert r["hits"] == []


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────


class TestNodeIdToFilePath:
    def test_strips_file_prefix(self) -> None:
        assert (
            spatial._node_id_to_file_path("file:mcp_server/storage/foo.py")
            == "mcp_server/storage/foo.py"
        )

    def test_strips_symbol_suffix(self) -> None:
        assert (
            spatial._node_id_to_file_path("file:mcp_server/storage/foo.py::bar")
            == "mcp_server/storage/foo.py"
        )

    def test_plain_path_unchanged(self) -> None:
        assert spatial._node_id_to_file_path("foo.py") == "foo.py"

    def test_empty_returns_none(self) -> None:
        assert spatial._node_id_to_file_path("") is None
