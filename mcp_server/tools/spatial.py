"""
spatial.py — v3.1.0 M4 Phase 2 MCP tools for spatial memory.

Four tools cover the agent-facing surface:

  - spatial_nearby       — files topologically close to a given file
                           (import/call edges + same-neighborhood),
                           ranked by recent activity.
  - spatial_heat         — top-K most-touched files in a time window.
  - spatial_neighborhood — return the neighborhood of a file and its
                           members.
  - spatial_affordances  — list the affordance keys (task_types) that
                           apply to a file, based on the bundled +
                           project affordances.yaml.

# Neighborhoods — hybrid (folder-tree default + yaml override)

In v3.1.0 a neighborhood = first two path components by default
(e.g., ``mcp_server/storage``, ``indexer``). A project can override
the mapping by committing ``.codevira/neighborhoods.yaml``:

::

    # neighborhoods.yaml
    storage:
      - mcp_server/storage/**/*.py
      - mcp_server/storage/jsonl_store.py
    tools:
      - mcp_server/tools/**/*.py
    engine:
      - mcp_server/engine/**/*.py

When the override exists, ``spatial_neighborhood(file_path)``
matches the file against each neighborhood's glob list (fnmatch);
the first matching neighborhood wins. Files that match nothing fall
through to the folder-tree default — so an override never *hides* a
file, only re-labels matched ones.

# Affordances — bundled + project override

The bundled defaults live at ``mcp_server/data/affordances.yaml``;
a project may override at ``.codevira/affordances.yaml``. Both files
are lists of ``{pattern, affordances}`` entries. The loader
concatenates bundled+project, then returns the union of affordances
across patterns that match the input ``file_path``.

# spatial_nearby ranking

Per the plan:

::

    score = (1 / (1 + bfs_dist)) × log(1 + visit_count_30d)

Candidates = BFS distance ≤ 2 over the indexer graph's edges ∪
same-neighborhood files. Ties broken by activity edit_count then
alphabetical.

If the indexer graph (``.codevira-cache/graph.sqlite``) doesn't
exist, BFS falls back to neighborhood-only — the tool still returns
useful results without requiring ``codevira index`` to have run.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import sqlite3
import yaml
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp_server.storage import activity_store, paths


logger = logging.getLogger(__name__)


# Reasonable BFS bound for nearby — too wide a net floods the output
# without adding signal.
_BFS_MAX_DEPTH = 2


# ──────────────────────────────────────────────────────────────────────
# spatial_nearby
# ──────────────────────────────────────────────────────────────────────


def spatial_nearby(file_path: str, *, k: int = 5) -> dict[str, Any]:
    """Files near ``file_path`` by topology + activity.

    Candidate set = BFS over graph edges (≤ 2 hops) ∪ same-neighborhood.
    Ranking = ``(1 / (1 + bfs_dist)) × log(1 + visit_count_30d)``,
    tied broken by edit_count and alphabetical. The originating
    file itself is excluded.

    Falls back to neighborhood-only if the indexer graph isn't built.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return {"file_path": file_path, "hits": [], "count": 0}

    # Step 1: BFS over the graph (if available).
    bfs_distances = _bfs_distances(file_path, max_depth=_BFS_MAX_DEPTH)

    # Step 2: neighborhood union.
    neighborhood_id = _neighborhood_for(file_path)
    neighborhood_members = _members_of(neighborhood_id)

    # Build candidate set.
    candidates: set[str] = set(bfs_distances.keys()) | set(neighborhood_members)
    candidates.discard(file_path)
    if not candidates:
        return {
            "file_path": file_path,
            "hits": [],
            "count": 0,
            "neighborhood": neighborhood_id,
        }

    # Step 3: rank.
    now = datetime.now(timezone.utc)
    scored: list[dict[str, Any]] = []
    for cand in candidates:
        bfs_dist = bfs_distances.get(cand, 3)  # 3 = "neighborhood-only" floor
        visit_count = activity_store.visit_count_30d(cand, now=now)
        bfs_term = 1.0 / (1.0 + bfs_dist)
        activity_term = math.log(1.0 + visit_count)
        score = bfs_term * activity_term
        scored.append(
            {
                "file_path": cand,
                "bfs_distance": bfs_dist,
                "visit_count_30d": visit_count,
                "score": round(score, 4),
            }
        )

    scored.sort(
        key=lambda r: (
            r["score"],
            r["visit_count_30d"],
            -ord(r["file_path"][0:1] or "z"),
        ),
        reverse=True,
    )
    return {
        "file_path": file_path,
        "neighborhood": neighborhood_id,
        "hits": scored[:k],
        "count": min(k, len(scored)),
    }


# ──────────────────────────────────────────────────────────────────────
# spatial_heat
# ──────────────────────────────────────────────────────────────────────


def spatial_heat(
    *,
    top_k: int = 20,
    since_days: int | None = None,
) -> dict[str, Any]:
    """Top-K most-touched files by weighted activity.

    ``since_days`` (optional): only count rows within the trailing
    N-day window. Falls back to all-time when None.
    """
    since: datetime | None = None
    if since_days is not None and since_days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=int(since_days))

    rows = activity_store.list_top_k_files(top_k=top_k, since=since)
    return {
        "hits": rows,
        "count": len(rows),
        "since_days": since_days,
    }


# ──────────────────────────────────────────────────────────────────────
# spatial_neighborhood
# ──────────────────────────────────────────────────────────────────────


def spatial_neighborhood(file_path: str) -> dict[str, Any]:
    """Return the neighborhood id + members for a file.

    Members are derived from the activity log + any indexer-known
    files in the same neighborhood — i.e., we surface every file the
    spatial layer has 'seen' that shares the neighborhood, not just
    the directory listing on disk.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return {"neighborhood_id": None, "members": [], "count": 0}
    nid = _neighborhood_for(file_path)
    members = _members_of(nid)
    return {
        "neighborhood_id": nid,
        "members": sorted(members),
        "count": len(members),
    }


# ──────────────────────────────────────────────────────────────────────
# spatial_affordances
# ──────────────────────────────────────────────────────────────────────


def spatial_affordances(file_path: str) -> dict[str, Any]:
    """Return the affordance keys (task_types) applicable to a file.

    Loads bundled + project affordances.yaml, evaluates each pattern
    via fnmatch, and returns the union of matching affordance lists.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return {"file_path": file_path, "affordances": []}

    affordances = _load_affordances()
    matched: list[str] = []
    seen: set[str] = set()
    matched_patterns: list[str] = []

    for entry in affordances:
        pattern = entry.get("pattern", "")
        if not pattern:
            continue
        if fnmatch.fnmatch(file_path, pattern):
            matched_patterns.append(pattern)
            for a in entry.get("affordances", []) or []:
                if a not in seen:
                    matched.append(a)
                    seen.add(a)

    return {
        "file_path": file_path,
        "affordances": matched,
        "matched_patterns": matched_patterns,
        "count": len(matched),
    }


# ──────────────────────────────────────────────────────────────────────
# Internals: neighborhoods
# ──────────────────────────────────────────────────────────────────────


def _neighborhood_for(file_path: str) -> str:
    """Resolve the neighborhood for a file.

    First consults the project override (.codevira/neighborhoods.yaml)
    if present; on no match (or no override file), falls back to the
    deterministic folder-tree rule (top-2 path components).
    """
    # Project override.
    override = _load_neighborhood_override()
    if override:
        for name, patterns in override.items():
            for p in patterns:
                if fnmatch.fnmatch(file_path, p):
                    return str(name)
    # Folder-tree default.
    return _folder_tree_neighborhood(file_path)


def _folder_tree_neighborhood(file_path: str) -> str:
    """Directory containing the file, capped at depth 2.

    Examples (from the plan):
      - mcp_server/storage/foo.py → ``mcp_server/storage``
      - mcp_server/tools/working.py → ``mcp_server/tools``
      - indexer/index_codebase.py → ``indexer``
      - README.md → ``<root>``

    The intent is to match how developers actually cluster code — by
    package directory, not by individual file. We strip the filename
    (last component) so files in the same dir share a neighborhood
    even when only one of them has 'top-2' coverage in the raw path.
    """
    parts = [p for p in file_path.split("/") if p]
    if not parts:
        return "<root>"
    dir_parts = parts[:-1]  # drop the filename
    if not dir_parts:
        return "<root>"
    return "/".join(dir_parts[:2])


def _members_of(neighborhood_id: str | None) -> list[str]:
    """All files known to either the activity log or the indexer
    graph that belong to ``neighborhood_id``.

    If ``neighborhood_id`` is None or '<root>', returns activity-log
    files only (no recursive walk of the filesystem).
    """
    if not neighborhood_id:
        return []

    candidates: set[str] = set()

    # Files seen in activity log.
    for rec in _iter_activity_node_ids():
        if _neighborhood_for(rec) == neighborhood_id:
            candidates.add(rec)

    # Files seen in indexer graph (best-effort).
    for nid in _iter_indexer_files():
        if _neighborhood_for(nid) == neighborhood_id:
            candidates.add(nid)

    return list(candidates)


def _load_neighborhood_override() -> dict[str, list[str]] | None:
    """Read .codevira/neighborhoods.yaml; return ``{name: [globs]}``
    or None if the file is missing / malformed.
    """
    override_path = paths.codevira_dir() / "neighborhoods.yaml"
    if not override_path.is_file():
        return None
    try:
        data = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "spatial: failed to parse neighborhoods.yaml; "
            "falling back to folder-tree: %s",
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for k, v in data.items():
        if not isinstance(v, list):
            continue
        out[str(k)] = [str(p) for p in v if isinstance(p, str)]
    return out or None


# ──────────────────────────────────────────────────────────────────────
# Internals: BFS over graph
# ──────────────────────────────────────────────────────────────────────


def _bfs_distances(start: str, *, max_depth: int) -> dict[str, int]:
    """Return ``{file_path: dist}`` for files reachable from ``start``
    within ``max_depth`` hops over import/call edges.

    Falls back to ``{start: 0}`` if the indexer graph doesn't exist.
    The BFS direction is undirected (both source→target and the
    reverse are followed) — for "what's near me?" the agent doesn't
    care about edge direction.
    """
    graph_db = paths.graph_cache_path()
    if not graph_db.is_file():
        return {start: 0}

    try:
        conn = sqlite3.connect(str(graph_db))
        conn.row_factory = sqlite3.Row
    except Exception as exc:  # noqa: BLE001
        logger.warning("spatial: cannot open graph.sqlite: %s", exc)
        return {start: 0}

    distances: dict[str, int] = {start: 0}
    frontier: set[str] = {start}
    try:
        try:
            for depth in range(1, max_depth + 1):
                if not frontier:
                    break
                # Pull neighbors for the current frontier in one query.
                placeholders = ",".join(["?"] * len(frontier))
                cursor = conn.execute(
                    f"""
                    SELECT DISTINCT target_id AS neighbor FROM edges
                    WHERE source_id IN ({placeholders})
                    UNION
                    SELECT DISTINCT source_id AS neighbor FROM edges
                    WHERE target_id IN ({placeholders})
                    """,
                    list(frontier) + list(frontier),
                )
                next_frontier: set[str] = set()
                for row in cursor.fetchall():
                    neighbor = row["neighbor"]
                    if not neighbor or neighbor in distances:
                        continue
                    # Edges include both file nodes ("file:path") and
                    # symbol nodes ("file:path::sym"). Normalize to the
                    # file component so the result list matches
                    # activity_store node_ids (per-file paths).
                    file_neighbor = _node_id_to_file_path(neighbor)
                    if file_neighbor and file_neighbor not in distances:
                        distances[file_neighbor] = depth
                        next_frontier.add(neighbor)
                frontier = next_frontier
        except sqlite3.DatabaseError as exc:
            # v3.1.x bug fix: a junk-bytes db or a schema with the
            # `edges` table missing makes the query raise. The connect-
            # time guard above only catches connection setup failures;
            # widen the safety net so spatial_nearby degrades to the
            # neighborhood-only fallback instead of crashing.
            logger.warning(
                "spatial: BFS query failed (%s); falling back to self-only",
                exc,
            )
            distances = {start: 0}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    distances.pop(start, None)  # exclude self from neighbor list
    return distances


def _node_id_to_file_path(node_id: str) -> str | None:
    """Extract the file path from an indexer node id.

    The indexer schema (per the exploration) prefixes file paths with
    ``file:`` for file nodes and ``file:path::symbol`` for symbols.
    Both should map to the file path string activity_store uses.
    """
    if not isinstance(node_id, str):
        return None
    if node_id.startswith("file:"):
        node_id = node_id[len("file:") :]
    sep = node_id.find("::")
    if sep >= 0:
        node_id = node_id[:sep]
    return node_id.strip() or None


def _iter_indexer_files() -> Iterable[str]:
    """Yield file paths from the indexer's nodes table. Empty if no
    graph DB exists.
    """
    graph_db = paths.graph_cache_path()
    if not graph_db.is_file():
        return
    try:
        conn = sqlite3.connect(str(graph_db))
        try:
            cursor = conn.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL"
            )
            for row in cursor.fetchall():
                fp = row[0]
                if isinstance(fp, str) and fp:
                    yield fp
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("spatial: indexer file scan failed: %s", exc)


def _iter_activity_node_ids() -> Iterable[str]:
    """Distinct node_ids seen in activity.jsonl."""
    seen: set[str] = set()
    from mcp_server.storage import jsonl_store

    for rec in jsonl_store.read_all(paths.activity_path()):
        nid = rec.get("node_id")
        if isinstance(nid, str) and nid not in seen:
            seen.add(nid)
            yield nid


# ──────────────────────────────────────────────────────────────────────
# Internals: affordances
# ──────────────────────────────────────────────────────────────────────


def _load_affordances() -> list[dict[str, Any]]:
    """Concat bundled defaults + project override.

    Order: bundled first, project second. ``spatial_affordances``
    deduplicates the affordance values per match, so duplicate
    patterns between bundled+project unions cleanly.
    """
    out: list[dict[str, Any]] = []
    for path in (_bundled_affordances_path(), _project_affordances_path()):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("spatial: failed to parse %s; skipping: %s", path, exc)
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            pattern = entry.get("pattern")
            affordances = entry.get("affordances")
            if not isinstance(pattern, str) or not isinstance(affordances, list):
                continue
            out.append(
                {
                    "pattern": pattern,
                    "affordances": [str(a) for a in affordances if isinstance(a, str)],
                }
            )
    return out


def _bundled_affordances_path() -> Path:
    """Resolve the bundled affordances.yaml that ships with the package."""
    # Located at mcp_server/data/affordances.yaml (sibling of the
    # tools/ package's parent).
    return Path(__file__).resolve().parent.parent / "data" / "affordances.yaml"


def _project_affordances_path() -> Path:
    return paths.codevira_dir() / "affordances.yaml"
