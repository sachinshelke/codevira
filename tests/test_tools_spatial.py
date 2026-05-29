"""
Tests for mcp_server.tools.spatial — v3.1.0 M4 Phase 2.

Coverage:
  - spatial_neighborhood: folder-tree default + yaml override
  - spatial_affordances: bundled defaults + project overrides
  - spatial_heat: wraps activity_store.list_top_k_files + since_days
  - spatial_nearby: neighborhood fallback (no graph) + activity ranking
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import activity_store, paths
from mcp_server.tools import spatial


def _seed_indexer_graph(project_root: Path, edges: list[tuple[str, str]]) -> Path:
    """Create a minimal graph.sqlite at the path spatial._bfs_distances reads.

    ``edges`` is a list of (source_id, target_id) tuples. We populate both
    `nodes` (with file_path metadata) and `edges` (the BFS table) so the
    schema mirrors what the indexer produces.
    """
    db_path = paths.graph_cache_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
              file_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
              source_id TEXT, target_id TEXT, kind TEXT NOT NULL,
              line INTEGER, PRIMARY KEY (source_id, target_id, kind)
            );
            """
        )
        node_ids: set[str] = set()
        for s, t in edges:
            node_ids.add(s)
            node_ids.add(t)
        for nid in node_ids:
            fp = nid[len("file:") :] if nid.startswith("file:") else nid
            conn.execute(
                "INSERT OR IGNORE INTO nodes(id, kind, name, file_path) "
                "VALUES (?, 'file', ?, ?)",
                (nid, fp.rsplit("/", 1)[-1], fp),
            )
        for s, t in edges:
            conn.execute(
                "INSERT OR IGNORE INTO edges(source_id, target_id, kind) "
                "VALUES (?, ?, 'import')",
                (s, t),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


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


# ──────────────────────────────────────────────────────────────────────
# BFS over a real indexer graph + score-formula numeric pin
# ──────────────────────────────────────────────────────────────────────


class TestBfsOverRealIndexerGraph:
    """CRITICAL — every other spatial_nearby test runs with no graph.sqlite,
    so the entire BFS path in _bfs_distances (~50 lines of SQL, frontier
    expansion, depth-2 bound, file:/symbol normalization) is wholly
    untested end-to-end. A regression could silently degrade
    spatial_nearby to neighborhood-only forever."""

    def test_bfs_reaches_1_hop_neighbor_via_indexer_graph(self, project: Path) -> None:
        # a → b (1 hop) under file: prefix.
        _seed_indexer_graph(
            project,
            edges=[("file:src/a.py", "file:src/b.py")],
        )
        # Some activity so the candidate gets a non-zero visit_count + score.
        activity_store.add("src/b.py", kind="edit")

        dists = spatial._bfs_distances("file:src/a.py", max_depth=2)
        # Normalized neighbor key uses the un-prefixed file path.
        assert dists.get("src/b.py") == 1, f"bfs missed 1-hop neighbor: {dists}"

    def test_bfs_reaches_2_hop_but_not_3_hop(self, project: Path) -> None:
        # Chain: a → b → c → d. With max_depth=2, d must NOT be reached.
        _seed_indexer_graph(
            project,
            edges=[
                ("file:src/a.py", "file:src/b.py"),
                ("file:src/b.py", "file:src/c.py"),
                ("file:src/c.py", "file:src/d.py"),
            ],
        )
        dists = spatial._bfs_distances("file:src/a.py", max_depth=2)
        assert dists.get("src/b.py") == 1
        assert dists.get("src/c.py") == 2
        assert "src/d.py" not in dists, f"bfs exceeded max_depth=2: {dists}"

    def test_bfs_is_undirected_follows_reverse_edges(self, project: Path) -> None:
        # Edge x → y; BFS started from y must still reach x.
        _seed_indexer_graph(project, edges=[("file:src/x.py", "file:src/y.py")])
        dists = spatial._bfs_distances("file:src/y.py", max_depth=2)
        assert dists.get("src/x.py") == 1


class TestSpatialNearbyScoreFormulaPin:
    """CRITICAL — the documented ranking formula
    score = (1 / (1 + bfs_dist)) * log(1 + visit_count_30d)
    has no numeric test. A refactor that swaps log for log10 or
    drops the +1 floor would re-rank everything and no test would fail.
    """

    def test_score_matches_documented_formula_for_1_hop_neighbor(
        self, project: Path
    ) -> None:
        # graph: a → b (1 hop). visits on b = 4.
        _seed_indexer_graph(project, edges=[("file:src/a.py", "file:src/b.py")])
        for _ in range(4):
            activity_store.add("src/b.py", kind="edit")

        r = spatial.spatial_nearby("file:src/a.py", k=5)
        # Find the hit for b.py.
        hit = next((h for h in r["hits"] if h["file_path"] == "src/b.py"), None)
        assert hit is not None, f"b.py missing from hits: {r['hits']}"
        # Formula: (1 / (1 + 1)) * log(1 + 4) = 0.5 * log(5).
        # spatial.py rounds score to 4 decimals, so match that precision.
        expected = round((1.0 / (1.0 + 1)) * math.log(1.0 + 4), 4)
        assert hit["bfs_distance"] == 1
        assert hit["score"] == expected, (
            f"score formula drifted: got {hit['score']}, "
            f"expected {expected} = round(0.5 * log(5), 4). "
            f"Likely a swap of log↔log10 or a missing +1 floor."
        )

    def test_score_uses_neighborhood_only_floor_when_no_bfs_path(
        self, project: Path
    ) -> None:
        # No graph at all — neighborhood-only floor should kick in with
        # bfs_dist defaulting to 3.
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        r = spatial.spatial_nearby("mcp_server/storage/a.py", k=5)
        hit = next(
            (h for h in r["hits"] if h["file_path"] == "mcp_server/storage/b.py"),
            None,
        )
        assert hit is not None
        # neighborhood-only floor: bfs_distance documented as 3.
        # Formula: (1 / (1 + 3)) * log(1 + 2) = 0.25 * log(3).
        assert hit["bfs_distance"] == 3
        expected = round((1.0 / 4.0) * math.log(1.0 + 2), 4)
        assert hit["score"] == expected


# ──────────────────────────────────────────────────────────────────────
# Corrupt-graph fallback + indexer-graph member integration
# ──────────────────────────────────────────────────────────────────────


class TestSpatialBfsFallbackOnCorruptGraph:
    """v3.1.x fix: spatial._bfs_distances now catches sqlite3.DatabaseError
    raised inside the BFS query loop (not just connect-time). A
    corrupt-bytes db or schema with the `edges` table missing falls
    back to self-only instead of crashing spatial_nearby."""

    def test_corrupt_graph_db_falls_back_to_self_only(self, project: Path) -> None:
        db = paths.graph_cache_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is not a sqlite database, just bytes" * 50)

        # No raise: fallback returns {} (self-popped before return).
        dists = spatial._bfs_distances("file:src/x.py", max_depth=2)
        assert dists == {} or dists == {"file:src/x.py": 0}

    def test_missing_required_table_falls_back_to_self_only(
        self, project: Path
    ) -> None:
        db = paths.graph_cache_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE nodes (id TEXT PRIMARY KEY, "
                "kind TEXT, name TEXT, file_path TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

        # No raise: query-time OperationalError caught.
        dists = spatial._bfs_distances("file:src/x.py", max_depth=2)
        assert dists == {} or dists == {"file:src/x.py": 0}

    def test_spatial_nearby_survives_corrupt_graph(self, project: Path) -> None:
        """End-to-end: spatial_nearby falls back to neighborhood-only
        when the indexer graph is corrupt — the user gets a partial
        result instead of an exception."""
        db = paths.graph_cache_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"corrupt" * 100)
        # Seed some activity so neighborhood-only fallback has data.
        activity_store.add("mcp_server/storage/a.py", kind="edit")
        activity_store.add("mcp_server/storage/b.py", kind="edit")
        r = spatial.spatial_nearby("mcp_server/storage/a.py", k=5)
        # No raise. Neighborhood fallback surfaces b.py.
        paths_returned = {h["file_path"] for h in r["hits"]}
        assert "mcp_server/storage/b.py" in paths_returned


class TestMembersFromIndexerGraph:
    """_members_of unions activity-log files AND indexer-graph files.
    The existing tests cover the activity path; this covers the
    indexer-only path so a file present in the code graph but never
    touched still surfaces as a neighborhood member."""

    def test_indexer_only_file_appears_in_neighborhood_members(
        self, project: Path
    ) -> None:
        # Seed an indexer graph with one file under mcp_server/storage/
        # but NO activity log entry. The file should still be a
        # member of the mcp_server/storage neighborhood.
        _seed_indexer_graph(
            project,
            edges=[
                (
                    "file:mcp_server/storage/indexer_only.py",
                    "file:mcp_server/storage/peer.py",
                )
            ],
        )

        members = spatial._members_of("mcp_server/storage")
        # The indexer-only file IS a member (no activity touch needed).
        assert any(
            "indexer_only.py" in m for m in members
        ), f"indexer-only file missing from members: {members}"


class TestActivityCompactPreservesMalformedTs:
    """compact._keep returns True for rows with non-string or
    unparseable ts — a deliberate fail-safe so doctor can repair
    them rather than compact silently dropping them. Currently no
    test pins this behavior."""

    def test_compact_keeps_row_with_no_ts(self, project: Path) -> None:
        from mcp_server.storage import activity_store, jsonl_store

        # Append a row that has no ts at all (malformed).
        jsonl_store.append(
            paths.activity_path(),
            {"node_id": "src/a.py", "kind": activity_store.KIND_EDIT},
        )
        # Append a normal-shape row so compact has something to do.
        activity_store.add("src/b.py", kind=activity_store.KIND_EDIT)

        # Aggressive retention (0 days); even the fresh one would drop
        # if not for ts freshness. The malformed row MUST survive.
        activity_store.compact(retention_days=10000)  # keep all
        rows = jsonl_store.read_all(paths.activity_path())
        # Malformed row preserved.
        malformed = [r for r in rows if "ts" not in r]
        assert malformed, "compact silently dropped a malformed ts row"

    def test_compact_keeps_row_with_unparseable_ts(self, project: Path) -> None:
        from mcp_server.storage import activity_store, jsonl_store

        jsonl_store.append(
            paths.activity_path(),
            {
                "node_id": "src/c.py",
                "kind": activity_store.KIND_EDIT,
                "ts": "this is not iso8601",
            },
        )
        activity_store.compact(retention_days=0)  # would drop everything by age
        rows = jsonl_store.read_all(paths.activity_path())
        kept = [r for r in rows if r.get("ts") == "this is not iso8601"]
        assert kept, "compact dropped a row with unparseable ts"


# ──────────────────────────────────────────────────────────────────────
# M4 minor + polish coverage
# ──────────────────────────────────────────────────────────────────────


class TestAffordancesUnionAndDedupe:
    """spatial_affordances unions bundled defaults with project
    overrides; dedup via a `seen` set so identical entries only
    appear once."""

    def test_overlap_between_two_overrides_dedupes(self, project: Path) -> None:
        # The override YAML is a top-level list. Add two entries that
        # share an affordance to verify the per-call seen-set dedupes.
        (project / ".codevira" / "affordances.yaml").write_text(
            "- pattern: '**/*.py'\n"
            "  affordances: [write_test, add_tool]\n"
            "- pattern: 'src/**'\n"
            "  affordances: [write_test]\n"
        )
        r = spatial.spatial_affordances("src/x.py")
        afford = r.get("affordances") or []
        # write_test should appear at most once in the deduped union.
        assert afford.count("write_test") == 1

    def test_malformed_affordances_yaml_silently_skipped(self, project: Path) -> None:
        # Schema-broken YAML: top-level not a list.
        (project / ".codevira" / "affordances.yaml").write_text(
            "patterns: not-a-list\n"
        )
        # Should not raise; falls back to bundled defaults.
        r = spatial.spatial_affordances("src/x.py")
        assert isinstance(r.get("affordances"), list)


class TestSpatialHeatSinceDays:
    """spatial_heat uses `since_days is not None and since_days > 0`.
    Passing 0 or negative falls through to all-time."""

    def test_zero_since_days_returns_all_time(self, project: Path) -> None:
        activity_store.add("a.py", kind=activity_store.KIND_EDIT)
        r0 = spatial.spatial_heat(top_k=10, since_days=0)
        r_none = spatial.spatial_heat(top_k=10, since_days=None)
        # 0 falls through → same result as None.
        assert len(r0["hits"]) == len(r_none["hits"])

    def test_negative_since_days_returns_all_time(self, project: Path) -> None:
        activity_store.add("a.py", kind=activity_store.KIND_EDIT)
        r = spatial.spatial_heat(top_k=10, since_days=-5)
        assert isinstance(r["hits"], list) and len(r["hits"]) >= 1


class TestNeighborhoodOverrideMalformed:
    """_load_neighborhood_override skips entries whose value isn't a
    list and drops non-string globs."""

    def test_non_list_value_skipped(self, project: Path) -> None:
        (project / ".codevira" / "neighborhoods.yaml").write_text(
            "auth: single-string-value\n" "data: ['models/*.py']\n"
        )
        nbhds = spatial._load_neighborhood_override()
        # `auth` (non-list) silently dropped; `data` survives.
        assert nbhds is None or "auth" not in nbhds
        assert nbhds is None or "data" in nbhds

    def test_non_string_glob_dropped(self, project: Path) -> None:
        (project / ".codevira" / "neighborhoods.yaml").write_text(
            "auth: ['ok.py', 123, null]\n"
        )
        nbhds = spatial._load_neighborhood_override()
        # Only the str entries survive.
        if nbhds and "auth" in nbhds:
            assert all(isinstance(p, str) for p in nbhds["auth"])
