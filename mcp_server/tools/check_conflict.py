"""
check_conflict.py — v2.1.2 Item 20: proactive conflict + duplicate detector.

Reports 3 + 4 both flagged silent duplicate / conflict accumulation as
a trust gap:
- Report 3 §"What's genuinely bad" #4: recording a decision that
  contradicts a `do_not_revert=True` decision succeeds silently.
- Report 4 #3: same decision recorded twice creates two rows with
  different IDs, no warning. Decision history becomes contradictory
  over time.

Design:
- A *duplicate* is a decision whose semantic distance to ANY existing
  decision is below the search threshold.
- A *conflict* is a decision whose semantic distance to a
  ``do_not_revert=True`` decision is below the search threshold.
- The check is best-effort: if semantic infra is unavailable (chromadb
  missing / corrupted), we return empty arrays — never block.

Hooks:
- Standalone MCP tool: ``check_conflict(decision_text, file_path=None)``
- Implicit: ``record_decision`` invokes this and surfaces
  ``_conflict_warning`` in its response unless ``force=True``.
"""

from __future__ import annotations

from typing import Any


def check_conflict(
    decision_text: str,
    file_path: str | None = None,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Check whether ``decision_text`` is a near-duplicate of, or
    contradicts, any existing decision in this project.

    Returns:
        {
            "status": "novel" | "duplicate" | "conflict",
            "conflicts": [{decision_id, similarity, do_not_revert,
                           summary, file_path, decision}, ...],
            "duplicates": [{decision_id, similarity, do_not_revert,
                            summary, file_path, decision}, ...],
            "threshold_used": float,
        }

    "Conflict" overrides "duplicate" — if any hit is do_not_revert=True
    the overall status is "conflict" regardless of how many merely-
    duplicate hits exist.
    """
    if not decision_text or not isinstance(decision_text, str):
        return {
            "status": "error",
            "error": "decision_text must be a non-empty string",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": None,
        }

    try:
        from mcp_server.tools._decision_embeddings import (
            semantic_search_decisions_scored,
            load_threshold,
        )
    except Exception:
        return {
            "status": "novel",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": None,
            "note": "semantic search unavailable — cannot detect conflicts",
        }

    threshold = load_threshold(target="search")

    try:
        scored = semantic_search_decisions_scored(
            decision_text.strip(),
            limit=max(limit, 5),
        )
    except Exception:
        scored = []

    hits_above_threshold = [(did, dist) for did, dist in scored if dist <= threshold]
    if not hits_above_threshold:
        return {
            "status": "novel",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": threshold,
        }

    # Resolve to full rows + do_not_revert flag from SQLite.
    try:
        from mcp_server.paths import get_data_dir
        from indexer.sqlite_graph import SQLiteGraph

        db_path = get_data_dir() / "graph" / "graph.db"
        if not db_path.is_file():
            return {
                "status": "novel",
                "conflicts": [],
                "duplicates": [],
                "threshold_used": threshold,
                "note": "no graph.db — cannot resolve decisions",
            }
        db = SQLiteGraph(db_path)
    except Exception as exc:
        return {
            "status": "novel",
            "conflicts": [],
            "duplicates": [],
            "threshold_used": threshold,
            "note": f"cannot open graph.db: {exc}",
        }

    try:
        ids = [did for did, _ in hits_above_threshold]
        score_by_id = {did: dist for did, dist in hits_above_threshold}
        placeholders = ",".join("?" * len(ids))
        try:
            cur = db.conn.execute(
                f"SELECT d.id, d.decision, d.context, d.file_path, "
                f"d.do_not_revert, s.summary FROM decisions d "
                f"JOIN sessions s ON d.session_id = s.session_id "
                f"WHERE d.id IN ({placeholders})",
                ids,
            )
            rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            rows = []

        conflicts = []
        duplicates = []
        for row in rows:
            rid = row.get("id")
            entry = {
                "decision_id": rid,
                "similarity_distance": score_by_id.get(int(rid))
                if rid is not None
                else None,
                "do_not_revert": bool(row.get("do_not_revert")),
                "summary": row.get("summary"),
                "file_path": row.get("file_path"),
                "decision": (row.get("decision") or "")[:200],
            }
            if entry["do_not_revert"]:
                conflicts.append(entry)
            else:
                duplicates.append(entry)

        # If a file_path was supplied, prefer hits in the same file.
        if file_path:

            def _file_score(e: dict[str, Any]) -> int:
                return 0 if e.get("file_path") == file_path else 1

            conflicts.sort(key=_file_score)
            duplicates.sort(key=_file_score)

        status = "conflict" if conflicts else ("duplicate" if duplicates else "novel")
        return {
            "status": status,
            "conflicts": conflicts,
            "duplicates": duplicates,
            "threshold_used": threshold,
        }
    finally:
        try:
            db.close()
        except Exception:
            pass
