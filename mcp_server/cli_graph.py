"""
cli_graph.py — `codevira graph` : self-contained interactive memory viewer.

v3.1.x: full multi-lens interactive viewer over the project's memory.
Renders decisions, files, skills, and reflections as one graph; the
embedded JS lets you switch lenses (color-by), layouts, filters, and
time-window without leaving the page. Self-contained: no CDN, no
runtime deps, works offline.

Nodes:
  - decision  — id, decision text, tags, file_path, origin.ide, ts
  - file      — id "file:<path>", referenced by decisions
  - skill     — id "K…", procedure summary, triggers.tags, origin.ide
  - reflection — id "R…", abstraction, tags, period, origin.ide

Edges:
  - supersedes (decision→decision; also skill→skill)
  - touches    (decision→file)
  - depends    (file→file, from code graph if available)
  - induced    (skill→decision, via shared session_ids)
  - covers     (reflection→decision, via source_decision_ids)
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Hard cap so a pathological store can never produce an unbounded O(n^2)
# layout in the browser (P5). Far above any realistic memory size.
_MAX_NODES_PER_TYPE = 2000


def _load_decisions() -> list[dict[str, Any]]:
    """Read every decision (including superseded, for lineage edges)."""
    from mcp_server.storage import decisions_store

    result = decisions_store.list_all(
        limit=_MAX_NODES_PER_TYPE,
        include_superseded=True,
        full=True,
    )
    return result.get("decisions", [])


def _load_skills() -> list[dict[str, Any]]:
    """Read all skills (any status). Best-effort: return [] on any error."""
    try:
        from mcp_server.storage import skills_store

        return skills_store.list_all(status=None, limit=_MAX_NODES_PER_TYPE)
    except Exception:  # noqa: BLE001
        return []


def _load_reflections() -> list[dict[str, Any]]:
    """Read recent reflections. Best-effort: return [] on any error."""
    try:
        from mcp_server.storage import reflections_store

        return reflections_store.list_filtered(limit=_MAX_NODES_PER_TYPE)
    except Exception:  # noqa: BLE001
        return []


def _load_code_graph_edges(file_paths: set[str]) -> list[tuple[str, str]]:
    """Best-effort file→file dependency edges from the code graph."""
    if not file_paths:
        return []
    try:
        import sqlite3

        from mcp_server.paths import get_data_dir

        db = get_data_dir() / "graph" / "graph.db"
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            id_to_file = {
                row[0]: row[1]
                for row in conn.execute("SELECT id, file_path FROM nodes")
                if row[1]
            }
            out: set[tuple[str, str]] = set()
            for src, tgt in conn.execute("SELECT source_id, target_id FROM edges"):
                sf, tf = id_to_file.get(src), id_to_file.get(tgt)
                if sf and tf and sf != tf and sf in file_paths and tf in file_paths:
                    out.add((sf, tf))
            return sorted(out)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _origin_ide(record: dict[str, Any]) -> str:
    """Pull ``origin.ide`` safely. Returns 'unknown' when absent."""
    o = record.get("origin") or {}
    if isinstance(o, dict):
        return str(o.get("ide") or "unknown")
    return "unknown"


def _build_graph(
    decisions: list[dict[str, Any]],
    *,
    with_files: bool = True,
    skills: list[dict[str, Any]] | None = None,
    reflections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shape raw memory records into ``{nodes, edges, meta}`` for the viewer."""
    skills = skills or []
    reflections = reflections or []

    decision_ids = {str(d.get("id")) for d in decisions if d.get("id")}
    skill_ids = {str(s.get("id")) for s in skills if s.get("id")}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    file_set: set[str] = set()

    session_to_decisions: dict[str, list[str]] = {}
    for d in decisions:
        sid = d.get("session_id")
        did = str(d.get("id") or "")
        if sid and did:
            session_to_decisions.setdefault(str(sid), []).append(did)

    all_tags: set[str] = set()
    all_ides: set[str] = set()
    timestamps: list[str] = []

    for d in decisions:
        did = str(d.get("id") or "")
        if not did:
            continue
        text = (d.get("decision") or "").strip()
        fp = d.get("file_path") or ""
        ts = d.get("ts") or ""
        ide = _origin_ide(d)
        tags = list(d.get("tags") or [])
        all_tags.update(t.lower() for t in tags if t)
        all_ides.add(ide)
        if ts:
            timestamps.append(ts)
        nodes.append(
            {
                "id": did,
                "type": "decision",
                "decision": text,
                "file_path": fp,
                "tags": tags,
                "do_not_revert": bool(d.get("do_not_revert", False)),
                "is_superseded": bool(d.get("is_superseded") or d.get("superseded_by")),
                "ts": ts,
                "session_id": d.get("session_id") or "",
                "ide": ide,
            }
        )
        sup_by = d.get("superseded_by")
        if sup_by and str(sup_by) in decision_ids:
            edges.append({"source": did, "target": str(sup_by), "kind": "supersedes"})
        if with_files and fp:
            file_set.add(fp)
            edges.append({"source": did, "target": f"file:{fp}", "kind": "touches"})

    if with_files:
        for fp in sorted(file_set):
            nodes.append(
                {
                    "id": f"file:{fp}",
                    "type": "file",
                    "file_path": fp,
                    "label": fp.rsplit("/", 1)[-1],
                    "tags": [],
                    "ts": "",
                    "ide": "",
                    "do_not_revert": False,
                    "is_superseded": False,
                }
            )
        for sf, tf in _load_code_graph_edges(file_set):
            edges.append(
                {"source": f"file:{sf}", "target": f"file:{tf}", "kind": "depends"}
            )

    for s in skills:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        triggers = s.get("triggers") or {}
        tags = list(triggers.get("tags") or [])
        all_tags.update(t.lower() for t in tags if t)
        ide = _origin_ide(s)
        all_ides.add(ide)
        ts = s.get("ts") or ""
        if ts:
            timestamps.append(ts)
        nodes.append(
            {
                "id": sid,
                "type": "skill",
                "name": str(s.get("name") or ""),
                "summary": str(s.get("summary") or ""),
                "procedure": str(s.get("procedure") or ""),
                "tags": tags,
                "status": str(s.get("status") or "active"),
                "source": str(s.get("source") or "explicit"),
                "success_count": int(s.get("success_count") or 0),
                "failure_count": int(s.get("failure_count") or 0),
                "do_not_revert": bool(s.get("do_not_revert", False)),
                "is_superseded": str(s.get("status") or "") == "superseded",
                "ts": ts,
                "ide": ide,
            }
        )
        sup_by = s.get("superseded_by")
        if sup_by and str(sup_by) in skill_ids:
            edges.append({"source": sid, "target": str(sup_by), "kind": "supersedes"})
        for src_sess in s.get("source_session_ids") or []:
            for did in session_to_decisions.get(str(src_sess), []):
                edges.append({"source": sid, "target": did, "kind": "induced"})

    for r in reflections:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        tags = list(r.get("tags") or [])
        all_tags.update(t.lower() for t in tags if t)
        ide = _origin_ide(r)
        all_ides.add(ide)
        ts = r.get("ts") or ""
        if ts:
            timestamps.append(ts)
        nodes.append(
            {
                "id": rid,
                "type": "reflection",
                "abstraction": str(r.get("abstraction") or ""),
                "tags": tags,
                "period_start": str(r.get("period_start") or ""),
                "period_end": str(r.get("period_end") or ""),
                "confidence": float(r.get("confidence") or 0.0),
                "model_used": str(r.get("model_used") or ""),
                "do_not_revert": False,
                "is_superseded": False,
                "ts": ts,
                "ide": ide,
            }
        )
        for did in r.get("source_decision_ids") or []:
            if str(did) in decision_ids:
                edges.append({"source": rid, "target": str(did), "kind": "covers"})

    meta = {
        "tags": sorted(all_tags),
        "ides": sorted(all_ides),
        "ts_min": min(timestamps) if timestamps else "",
        "ts_max": max(timestamps) if timestamps else "",
        "counts": {
            "decisions": sum(1 for n in nodes if n["type"] == "decision"),
            "files": sum(1 for n in nodes if n["type"] == "file"),
            "skills": sum(1 for n in nodes if n["type"] == "skill"),
            "reflections": sum(1 for n in nodes if n["type"] == "reflection"),
        },
    }

    return {"nodes": nodes, "edges": edges, "meta": meta}


# The HTML template uses ``@@PLACEHOLDER@@`` markers rather than an
# f-string so the inlined JS (full of ``{}``) needs no brace-escaping.
# v3.1.x: template extracted to mcp_server/graph/template.html so the
# HTML/CSS/JS body is testable + editable on its own + diffable in
# reviews without scrolling past a 1700-line embedded string.
def _load_template() -> str:
    """Read the inlined HTML template ONCE per process, cache it."""
    global _CACHED_TEMPLATE
    if _CACHED_TEMPLATE is not None:
        return _CACHED_TEMPLATE
    from importlib import resources

    _CACHED_TEMPLATE = (
        resources.files("mcp_server.graph")
        .joinpath("template.html")
        .read_text(encoding="utf-8")
    )
    return _CACHED_TEMPLATE


_CACHED_TEMPLATE: str | None = None


def render_graph_html(
    decisions: list[dict[str, Any]],
    *,
    with_files: bool = True,
    skills: list[dict[str, Any]] | None = None,
    reflections: list[dict[str, Any]] | None = None,
) -> str:
    """Render the self-contained viewer HTML.

    ``with_files`` overlays code-file nodes (and best-effort file→file
    code-dependency edges). ``skills`` / ``reflections`` overlay
    procedural + abstraction memory respectively (pass ``None`` to omit
    each). Pure function (no I/O) so it is directly unit-testable.
    """
    graph = _build_graph(
        decisions,
        with_files=with_files,
        skills=skills,
        reflections=reflections,
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Escape ``<`` as ``<`` in the inlined JSON so any decision/skill/
    # reflection text containing a literal ``</script>`` (or ``<!--``)
    # cannot break out of the <script> data island (P4).
    data_json = json.dumps(graph, default=str).replace("<", "\\u003c")
    return (
        _load_template()
        .replace("@@TITLE@@", "codevira memory")
        .replace("@@GENERATED@@", html.escape(f"generated {generated}"))
        .replace("@@DATA@@", data_json)
    )


def cmd_graph(
    out: str | None = None,
    *,
    dry_run: bool = False,
    with_files: bool = True,
    with_skills: bool = True,
    with_reflections: bool = True,
) -> int:
    """``codevira graph`` — write the interactive memory viewer to an HTML file.

    Returns a POSIX exit code: 0 success, 1 error, 2 nothing to show.
    """
    try:
        from mcp_server.storage import paths as store_paths

        if not store_paths.is_initialized():
            print(
                "Error: this project has no codevira memory yet "
                f"(no {store_paths.codevira_dir()}).\n"
                "  Fix: run `codevira init`, then record a decision.",
                file=sys.stderr,
            )
            return 1
        decisions = _load_decisions()
        skills = _load_skills() if with_skills else []
        reflections = _load_reflections() if with_reflections else []
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not read decisions: {exc}", file=sys.stderr)
        return 1

    if not decisions and not skills and not reflections:
        print(
            "No decisions, skills, or reflections recorded yet — nothing to visualize.\n"
            "  Fix: record one with the record_decision MCP tool, then re-run "
            "`codevira graph`.",
            file=sys.stderr,
        )
        return 2

    from mcp_server.storage import paths as store_paths

    if out is None:
        out_path = store_paths.codevira_cache_dir() / "memory-graph.html"
    else:
        out_path = Path(out).expanduser().resolve()

    if dry_run:
        print(
            f"  [dry-run] Would render {len(decisions)} decisions, "
            f"{len(skills)} skills, {len(reflections)} reflections → {out_path}"
        )
        return 0

    try:
        from mcp_server.storage.atomic import atomic_write_text

        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            out_path,
            render_graph_html(
                decisions,
                with_files=with_files,
                skills=skills,
                reflections=reflections,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to write viewer: {exc}", file=sys.stderr)
        return 1

    n_protected = sum(1 for d in decisions if d.get("do_not_revert"))
    print(
        f"  ✓ codevira memory viewer "
        f"({len(decisions)} decisions, {len(skills)} skills, "
        f"{len(reflections)} reflections, {n_protected} protected)"
    )
    print(f"  Path: {out_path}")
    print(f"  Open: file://{out_path}")
    return 0
