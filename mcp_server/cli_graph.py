"""
cli_graph.py — `codevira graph` : self-contained interactive memory viewer.

v3.0.0 (D000016): render the project's decision memory as a single
self-contained HTML file — zero runtime dependencies, no server, works
offline. Data is read through the canonical JSONL store
(``decisions_store.list_all`` — honors D000002) and inlined as JSON; the
page ships an inlined vanilla-JS force layout plus a client-side query
box and a details panel.

Nodes are decisions; edges are the ``supersedes`` lineage
(old → replacement). Querying/filtering (text, tag, file_path,
protected-only) happens entirely client-side, so the artifact is a
portable snapshot you can open anywhere or attach to a review.

Design rationale (see D000016): a self-contained HTML beats a local
Flask/FastAPI server (extra dep + running process) and pyvis (extra
deps) — it reuses the data layer that already exists, works offline, and
ships as one file. The interactive code-graph overlay
(``.codevira-cache/graph.sqlite``) is a deliberate follow-up; v1 covers
decision memory.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Hard cap so a pathological store can never produce an unbounded O(n^2)
# layout in the browser (P5). Far above any realistic decision count.
_MAX_NODES = 2000


def _load_decisions() -> list[dict[str, Any]]:
    """Read every decision (including superseded, for lineage edges).

    Goes through the canonical JSONL store — never graph.db (D000002).
    """
    from mcp_server.storage import decisions_store

    result = decisions_store.list_all(
        limit=_MAX_NODES,
        include_superseded=True,
        full=True,
    )
    return result.get("decisions", [])


def _load_code_graph_edges(file_paths: set[str]) -> list[tuple[str, str]]:
    """Best-effort file→file dependency edges from the code graph.

    Reads the tree-sitter code graph (``<data_dir>/graph/graph.db``) and
    returns ``(src_file, tgt_file)`` pairs where BOTH endpoints are in
    ``file_paths`` — so the overlay only links files that already carry
    decisions, keeping it focused. Degrades to ``[]`` if the graph store
    is missing or unreadable (P9: the viewer must still render from the
    canonical decision data even when the rebuildable graph cache is
    absent or its location has drifted).
    """
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
    except Exception:  # noqa: BLE001 — overlay is best-effort, never fatal
        return []


def _build_graph(
    decisions: list[dict[str, Any]], *, with_files: bool = True
) -> dict[str, Any]:
    """Shape raw decision records into ``{nodes, edges}`` for the viewer.

    Decision edges encode supersession (retired → replacement). When
    ``with_files`` is set, the graph also overlays code structure: a
    ``file`` node per distinct ``file_path``, a ``touches`` edge from each
    decision to the file it pertains to, and best-effort ``depends``
    edges between those files pulled from the code graph. Dangling
    references are dropped defensively.
    """
    ids = {str(d.get("id")) for d in decisions if d.get("id")}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    file_set: set[str] = set()

    for d in decisions:
        did = str(d.get("id") or "")
        if not did:
            continue
        text = (d.get("decision") or "").strip()
        fp = d.get("file_path") or ""
        nodes.append(
            {
                "id": did,
                "type": "decision",
                "decision": text,
                "file_path": fp,
                "tags": d.get("tags") or [],
                "do_not_revert": bool(d.get("do_not_revert", False)),
                "is_superseded": bool(d.get("is_superseded") or d.get("superseded_by")),
                "ts": d.get("ts") or "",
                "session_id": d.get("session_id") or "",
            }
        )
        sup_by = d.get("superseded_by")
        if sup_by and str(sup_by) in ids:
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
                }
            )
        for sf, tf in _load_code_graph_edges(file_set):
            edges.append(
                {"source": f"file:{sf}", "target": f"file:{tf}", "kind": "depends"}
            )

    return {"nodes": nodes, "edges": edges}


# The HTML template uses ``@@PLACEHOLDER@@`` markers rather than an
# f-string so the inlined JS (full of ``{}``) needs no brace-escaping.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.5 ui-sans-serif,system-ui,sans-serif;
         display:flex; height:100vh; overflow:hidden;
         background:#0f1115; color:#e6e6e6; }
  #side { width:340px; flex:0 0 340px; padding:14px; overflow:auto;
          border-right:1px solid #2a2d35; background:#15171d; }
  #side h1 { font-size:15px; margin:0 0 4px; }
  #meta { color:#8b91a0; font-size:11px; margin-bottom:10px; }
  #q { width:100%; padding:7px 9px; border-radius:6px; border:1px solid #333;
       background:#0f1115; color:#e6e6e6; margin-bottom:8px; }
  .row { display:flex; gap:8px; align-items:center; margin-bottom:10px;
         color:#a9afbd; font-size:12px; }
  #stats { font-size:11px; color:#8b91a0; margin-bottom:10px; }
  .legend span { display:inline-flex; align-items:center; gap:5px; margin-right:10px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  #detail { margin-top:12px; padding:10px; border:1px solid #2a2d35;
            border-radius:8px; background:#0f1115; display:none; }
  #detail h2 { font-size:13px; margin:0 0 6px; }
  #detail .k { color:#8b91a0; }
  #detail .tag { display:inline-block; background:#23262f; border-radius:4px;
                 padding:1px 6px; margin:2px 3px 0 0; font-size:11px; }
  #detail .txt { white-space:pre-wrap; margin-top:6px; }
  #canvasWrap { flex:1; position:relative; }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  .node circle { stroke:#0f1115; stroke-width:1.5px; cursor:pointer; }
  .node text { fill:#cdd2dd; font-size:10px; pointer-events:none; }
  .edge { stroke:#4a4f5c; stroke-width:1.2px; }
  .edge-supersedes { marker-end:url(#arrow); }
  .edge-touches { stroke:#3a3f4b; stroke-dasharray:3 3; }
  .edge-depends { stroke:#2f6f4f; }
  .dim { opacity:0.1; }
</style>
</head>
<body>
<div id="side">
  <h1>@@TITLE@@</h1>
  <div id="meta">@@GENERATED@@</div>
  <input id="q" placeholder="filter: id / text / tag / file…" autocomplete="off">
  <label class="row"><input type="checkbox" id="protOnly"> protected (do_not_revert) only</label>
  <div id="stats"></div>
  <div class="legend">
    <span><i class="dot" style="background:#e5534b"></i>protected</span>
    <span><i class="dot" style="background:#539bf5"></i>active</span>
    <span><i class="dot" style="background:#6b7280"></i>superseded</span>
    <span><i class="dot" style="background:#d29922"></i>file</span>
  </div>
  <div id="detail"></div>
</div>
<div id="canvasWrap">
  <svg id="svg"><defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="7"
            markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#6b7280"/>
    </marker>
  </defs></svg>
</div>
<script>
const DATA = @@DATA@@;
const svg = document.getElementById('svg');
const NS = 'http://www.w3.org/2000/svg';
const W = svg.clientWidth || 900, H = svg.clientHeight || 700;
const byId = {};
DATA.nodes.forEach((n,i) => {
  n.x = W/2 + Math.cos(i)*Math.min(W,H)*0.3 + (Math.random()-0.5)*40;
  n.y = H/2 + Math.sin(i)*Math.min(W,H)*0.3 + (Math.random()-0.5)*40;
  n.vx = 0; n.vy = 0; byId[n.id] = n;
});

// Bounded force layout (P5: fixed iteration count, no live animation loop).
function layout() {
  const ITER = 220, k = Math.sqrt((W*H) / Math.max(1, DATA.nodes.length));
  for (let it=0; it<ITER; it++) {
    for (let a=0; a<DATA.nodes.length; a++) {
      const na = DATA.nodes[a];
      for (let b=a+1; b<DATA.nodes.length; b++) {
        const nb = DATA.nodes[b];
        let dx = na.x-nb.x, dy = na.y-nb.y;
        let d = Math.sqrt(dx*dx+dy*dy) || 0.01;
        const rep = (k*k) / d / 8;
        dx/=d; dy/=d;
        na.vx += dx*rep; na.vy += dy*rep;
        nb.vx -= dx*rep; nb.vy -= dy*rep;
      }
    }
    DATA.edges.forEach(e => {
      const s = byId[e.source], t = byId[e.target];
      if (!s || !t) return;
      let dx = t.x-s.x, dy = t.y-s.y;
      let d = Math.sqrt(dx*dx+dy*dy) || 0.01;
      const att = (d*d) / k / 90;
      dx/=d; dy/=d;
      s.vx += dx*att; s.vy += dy*att;
      t.vx -= dx*att; t.vy -= dy*att;
    });
    DATA.nodes.forEach(n => {
      n.vx += (W/2 - n.x)*0.002; n.vy += (H/2 - n.y)*0.002;
      n.x += Math.max(-30, Math.min(30, n.vx)); n.y += Math.max(-30, Math.min(30, n.vy));
      n.vx *= 0.85; n.vy *= 0.85;
      n.x = Math.max(30, Math.min(W-30, n.x)); n.y = Math.max(20, Math.min(H-20, n.y));
    });
  }
}

function color(n) {
  if (n.type === 'file') return '#d29922';
  if (n.is_superseded) return '#6b7280';
  if (n.do_not_revert) return '#e5534b';
  return '#539bf5';
}

function render() {
  while (svg.lastChild && svg.lastChild.tagName !== 'defs') svg.removeChild(svg.lastChild);
  DATA.edges.forEach(e => {
    const s = byId[e.source], t = byId[e.target];
    if (!s || !t) return;
    const l = document.createElementNS(NS,'line');
    l.setAttribute('class','edge edge-' + (e.kind || 'supersedes'));
    l.setAttribute('x1',s.x); l.setAttribute('y1',s.y);
    l.setAttribute('x2',t.x); l.setAttribute('y2',t.y);
    l.dataset.s = e.source; l.dataset.t = e.target;
    svg.appendChild(l);
  });
  DATA.nodes.forEach(n => {
    const g = document.createElementNS(NS,'g');
    g.setAttribute('class','node'); g.dataset.id = n.id;
    g.setAttribute('transform', `translate(${n.x},${n.y})`);
    const c = document.createElementNS(NS,'circle');
    c.setAttribute('r', n.type==='file' ? 6 : (n.do_not_revert ? 9 : 7));
    c.setAttribute('fill', color(n));
    const tx = document.createElementNS(NS,'text');
    tx.setAttribute('x', 11); tx.setAttribute('y', 3);
    tx.textContent = n.type === 'file' ? n.label : n.id;
    g.appendChild(c); g.appendChild(tx);
    g.addEventListener('click', () => showDetail(n));
    svg.appendChild(g);
  });
}

function esc(s){ const d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }

function showDetail(n) {
  const d = document.getElementById('detail');
  d.style.display = 'block';
  if (n.type === 'file') {
    d.innerHTML =
      `<h2>📄 ${esc(n.label)}</h2>` +
      `<div class="k">${esc(n.file_path)}</div>` +
      `<div class="txt">Code file referenced by one or more decisions. Dashed edges link decisions that touch it; green edges are code dependencies between files.</div>`;
    return;
  }
  const tags = (n.tags||[]).map(t => `<span class="tag">${esc(t)}</span>`).join('');
  d.innerHTML =
    `<h2>${esc(n.id)} ${n.do_not_revert?'🔒':''}</h2>` +
    `<div><span class="k">file:</span> ${esc(n.file_path||'—')}</div>` +
    `<div><span class="k">when:</span> ${esc((n.ts||'').slice(0,19))}` +
      ` &nbsp;<span class="k">session:</span> ${esc(n.session_id||'—')}</div>` +
    (n.is_superseded?`<div class="k">(superseded)</div>`:``) +
    `<div>${tags}</div>` +
    `<div class="txt">${esc(n.decision)}</div>`;
}

const q = document.getElementById('q');
const protOnly = document.getElementById('protOnly');
function applyFilter() {
  const term = q.value.trim().toLowerCase();
  const matchIds = new Set();
  DATA.nodes.forEach(n => {
    let ok = true;
    if (protOnly.checked && !(n.type !== 'file' && n.do_not_revert)) ok = false;
    if (ok && term) {
      const hay = (n.id+' '+(n.decision||'')+' '+(n.tags||[]).join(' ')+' '
                   +(n.file_path||'')+' '+(n.label||'')).toLowerCase();
      ok = hay.includes(term);
    }
    if (ok) matchIds.add(n.id);
  });
  document.querySelectorAll('.node').forEach(g =>
    g.classList.toggle('dim', !matchIds.has(g.dataset.id)));
  document.querySelectorAll('.edge').forEach(l =>
    l.classList.toggle('dim', !(matchIds.has(l.dataset.s) && matchIds.has(l.dataset.t))));
  const nFiles = DATA.nodes.filter(n => n.type === 'file').length;
  const nDec = DATA.nodes.length - nFiles;
  document.getElementById('stats').textContent =
    `${matchIds.size} / ${DATA.nodes.length} shown · ${nDec} decisions · ${nFiles} files · ${DATA.edges.length} links`;
}
q.addEventListener('input', applyFilter);
protOnly.addEventListener('change', applyFilter);

layout(); render(); applyFilter();
</script>
</body>
</html>
"""


def render_graph_html(
    decisions: list[dict[str, Any]], *, with_files: bool = True
) -> str:
    """Render the self-contained viewer HTML for ``decisions``.

    ``with_files`` overlays code-file nodes (and best-effort file→file
    code-dependency edges). Pure function (no I/O) so it is directly
    unit-testable.
    """
    graph = _build_graph(decisions, with_files=with_files)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Escape ``<`` as ``<`` in the inlined JSON so decision text
    # containing a literal ``</script>`` (or ``<!--``) can't break out of
    # the <script> data island and inject HTML (P4). ``<`` is a valid
    # JSON/JS escape that decodes back to ``<`` at parse time.
    data_json = json.dumps(graph, default=str).replace("<", "\\u003c")
    return (
        _TEMPLATE.replace("@@TITLE@@", "codevira memory")
        .replace("@@GENERATED@@", html.escape(f"generated {generated}"))
        .replace("@@DATA@@", data_json)
    )


def cmd_graph(
    out: str | None = None, *, dry_run: bool = False, with_files: bool = True
) -> int:
    """``codevira graph`` — write the interactive memory viewer to an HTML file.

    ``with_files`` overlays code-file nodes (default on; ``--no-files``
    turns it off for a decisions-only view).

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
    except ValueError as e:
        # get_project_root / store refuses invalid roots ($HOME, system dirs).
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not read decisions: {exc}", file=sys.stderr)
        return 1

    if not decisions:
        print(
            "No decisions recorded yet — nothing to visualize.\n"
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
        print(f"  [dry-run] Would render {len(decisions)} decisions → {out_path}")
        return 0

    try:
        from mcp_server.storage.atomic import atomic_write_text

        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out_path, render_graph_html(decisions, with_files=with_files))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to write viewer: {exc}", file=sys.stderr)
        return 1

    n_protected = sum(1 for d in decisions if d.get("do_not_revert"))
    print(
        f"  ✓ codevira memory viewer ({len(decisions)} decisions, {n_protected} protected)"
    )
    print(f"  Path: {out_path}")
    print(f"  Open: file://{out_path}")
    return 0
