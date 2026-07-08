"""Tests for `codevira graph` — the self-contained memory viewer (D000016).

Covers the pure render/build helpers (no I/O) plus the cmd_graph entry
point against an isolated JSONL store.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import mcp_server.paths as paths
from mcp_server.cli_graph import _build_graph, render_graph_html, cmd_graph


# ---------------------------------------------------------------------------
# Pure helpers — no store needed
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_supersedes_edge_emitted(self):
        decisions = [
            {
                "id": "D000001",
                "decision": "old",
                "superseded_by": "D000002",
                "is_superseded": True,
            },
            {"id": "D000002", "decision": "new"},
        ]
        g = _build_graph(decisions)
        assert len(g["nodes"]) == 2  # no file_path → no file-overlay nodes
        assert {
            "source": "D000001",
            "target": "D000002",
            "kind": "supersedes",
        } in g["edges"]

    def test_file_overlay_adds_file_nodes_and_touches_edges(self):
        decisions = [
            {"id": "D1", "decision": "a", "file_path": "src/x.py"},
            {"id": "D2", "decision": "b", "file_path": "src/x.py"},
        ]
        g = _build_graph(decisions, with_files=True)
        files = [n for n in g["nodes"] if n.get("type") == "file"]
        assert len(files) == 1  # both decisions share one file node
        assert files[0]["id"] == "file:src/x.py"
        touches = [e for e in g["edges"] if e.get("kind") == "touches"]
        assert len(touches) == 2  # each decision → the shared file

    def test_with_files_false_is_decisions_only(self):
        decisions = [{"id": "D1", "decision": "a", "file_path": "src/x.py"}]
        g = _build_graph(decisions, with_files=False)
        assert all(n.get("type") == "decision" for n in g["nodes"])
        assert all(e.get("kind") != "touches" for e in g["edges"])

    def test_dangling_superseded_by_is_dropped(self):
        """A superseded_by pointing at a missing id must not create an edge."""
        decisions = [
            {"id": "D000001", "decision": "x", "superseded_by": "D999999"},
        ]
        g = _build_graph(decisions)
        assert g["edges"] == []

    def test_flags_carried_through(self):
        decisions = [
            {
                "id": "D1",
                "decision": "d",
                "do_not_revert": True,
                "tags": ["a"],
                "file_path": "x.py",
            },
        ]
        n = _build_graph(decisions)["nodes"][0]
        assert n["do_not_revert"] is True
        assert n["tags"] == ["a"]
        assert n["file_path"] == "x.py"

    def test_origin_ide_surfaced_on_node(self):
        decisions = [
            {
                "id": "D1",
                "decision": "d",
                "origin": {"ide": "cursor", "host_hash": "abc"},
            },
            {"id": "D2", "decision": "e"},  # no origin → 'unknown'
        ]
        nodes = {
            n["id"]: n
            for n in _build_graph(decisions)["nodes"]
            if n["type"] == "decision"
        }
        assert nodes["D1"]["ide"] == "cursor"
        assert nodes["D2"]["ide"] == "unknown"

    def test_meta_block_has_tags_ides_ts_range(self):
        decisions = [
            {
                "id": "D1",
                "decision": "a",
                "tags": ["auth", "fix"],
                "ts": "2026-01-02T00:00:00+00:00",
                "origin": {"ide": "cursor"},
            },
            {
                "id": "D2",
                "decision": "b",
                "tags": ["auth"],
                "ts": "2026-03-04T00:00:00+00:00",
                "origin": {"ide": "claude_code"},
            },
        ]
        meta = _build_graph(decisions, with_files=False)["meta"]
        assert "auth" in meta["tags"] and "fix" in meta["tags"]
        assert set(meta["ides"]) >= {"cursor", "claude_code"}
        assert meta["ts_min"] == "2026-01-02T00:00:00+00:00"
        assert meta["ts_max"] == "2026-03-04T00:00:00+00:00"
        assert meta["counts"]["decisions"] == 2
        assert meta["counts"]["files"] == 0
        assert meta["counts"]["skills"] == 0
        assert meta["counts"]["reflections"] == 0

    def test_skill_node_and_induced_edge(self):
        decisions = [
            {"id": "D1", "decision": "d", "session_id": "s-abc"},
        ]
        skills = [
            {
                "id": "K1",
                "name": "git-rebase",
                "summary": "how we rebase",
                "procedure": "git fetch && git rebase origin/main",
                "triggers": {"tags": ["git"], "file_patterns": []},
                "source": "induced",
                "source_session_ids": ["s-abc"],
                "status": "active",
                "ts": "2026-04-01T00:00:00+00:00",
            },
        ]
        g = _build_graph(decisions, with_files=False, skills=skills)
        types = [n["type"] for n in g["nodes"]]
        assert "skill" in types
        skill_node = next(n for n in g["nodes"] if n["type"] == "skill")
        assert skill_node["id"] == "K1"
        assert skill_node["name"] == "git-rebase"
        assert {"source": "K1", "target": "D1", "kind": "induced"} in g["edges"]
        assert g["meta"]["counts"]["skills"] == 1

    def test_reflection_node_and_covers_edge(self):
        decisions = [
            {"id": "D1", "decision": "d"},
            {"id": "D2", "decision": "e"},
        ]
        reflections = [
            {
                "id": "R1",
                "abstraction": "we kept refactoring auth",
                "source_decision_ids": ["D1", "D2", "D999"],  # D999 dangles
                "tags": ["auth"],
                "ts": "2026-05-01T00:00:00+00:00",
                "confidence": 0.8,
            },
        ]
        g = _build_graph(decisions, with_files=False, reflections=reflections)
        rnode = next(n for n in g["nodes"] if n["type"] == "reflection")
        assert rnode["id"] == "R1"
        assert rnode["confidence"] == 0.8
        covers = [e for e in g["edges"] if e["kind"] == "covers"]
        assert {"source": "R1", "target": "D1", "kind": "covers"} in covers
        assert {"source": "R1", "target": "D2", "kind": "covers"} in covers
        # Dangling target must not produce an edge.
        assert not any(e["target"] == "D999" for e in covers)
        assert g["meta"]["counts"]["reflections"] == 1

    def test_decision_surfaces_outcome_and_counter_fields(self):
        """v3.1.x viewer overhaul: outcome, alternatives_considered,
        would_re_examine_if, context must round-trip onto the node."""
        decisions = [
            {
                "id": "D1",
                "decision": "use bcrypt",
                "outcome": "kept",
                "alternatives_considered": ["argon2", "scrypt"],
                "would_re_examine_if": "if argon2 lands in stdlib",
                "context": "hashed passwords, no clear winner",
            }
        ]
        g = _build_graph(decisions, with_files=False)
        n = g["nodes"][0]
        assert n["outcome"] == "kept"
        assert n["alternatives_considered"] == ["argon2", "scrypt"]
        assert n["would_re_examine_if"] == "if argon2 lands in stdlib"
        assert n["context"] == "hashed passwords, no clear winner"

    def test_meta_outcomes_distribution(self):
        decisions = [
            {"id": "D1", "decision": "x", "outcome": "kept"},
            {"id": "D2", "decision": "y", "outcome": "modified"},
            {"id": "D3", "decision": "z", "outcome": "reverted"},
            {"id": "D4", "decision": "w"},  # unclassified
        ]
        g = _build_graph(decisions, with_files=False)
        assert g["meta"]["outcomes"] == {
            "kept": 1,
            "modified": 1,
            "reverted": 1,
            "unclassified": 1,
        }

    def test_meta_chains_precomputes_supersedes_lineage(self):
        """For every decision in a supersedes chain, meta.chains[id]
        is the full ordered list oldest → newest."""
        decisions = [
            {
                "id": "D1",
                "decision": "v1",
                "superseded_by": "D2",
                "is_superseded": True,
            },
            {
                "id": "D2",
                "decision": "v2",
                "supersedes": "D1",
                "superseded_by": "D3",
                "is_superseded": True,
            },
            {"id": "D3", "decision": "v3", "supersedes": "D2"},
            {"id": "D9", "decision": "singleton"},
        ]
        g = _build_graph(decisions, with_files=False)
        chains = g["meta"]["chains"]
        assert chains["D1"] == ["D1", "D2", "D3"]
        assert chains["D2"] == ["D1", "D2", "D3"]
        assert chains["D3"] == ["D1", "D2", "D3"]
        # Singleton has no chain.
        assert "D9" not in chains

    def test_skill_supersedes_chain(self):
        skills = [
            {
                "id": "K1",
                "name": "v1",
                "procedure": "x",
                "status": "superseded",
                "superseded_by": "K2",
            },
            {
                "id": "K2",
                "name": "v2",
                "procedure": "y",
                "status": "active",
                "supersedes": "K1",
            },
        ]
        g = _build_graph([], with_files=False, skills=skills)
        assert {"source": "K1", "target": "K2", "kind": "supersedes"} in g["edges"]


class TestRenderHtml:
    def test_self_contained_and_no_placeholders(self):
        decisions = [{"id": "D1", "decision": "hello", "tags": ["t"]}]
        h = render_graph_html(decisions)
        assert h.lstrip().startswith("<!DOCTYPE")
        assert "@@" not in h, "template placeholder left unfilled"
        # No external resource loading — fully offline (the SVG xmlns URI
        # is a namespace identifier, never fetched, so it's allowed).
        assert 'src="http' not in h
        assert "<link" not in h.lower()
        assert "cdn" not in h.lower()
        # Data is inlined and parseable.
        m = re.search(r"const DATA = (\{.*?\});\n", h, re.S)
        assert m, "inlined DATA assignment missing"
        data = json.loads(m.group(1))
        assert data["nodes"][0]["id"] == "D1"

    def test_script_breakout_is_neutralized(self):
        # A decision containing </script> must not break out of the data
        # island (P4). It should be escaped, yet still round-trip on parse.
        decisions = [{"id": "D1", "decision": "</script><b>pwn</b>"}]
        h = render_graph_html(decisions)
        assert "</script><b>pwn</b>" not in h  # raw breakout absent
        assert "\\u003c/script>" in h  # escaped form present
        m = re.search(r"const DATA = (\{.*?\});\n", h, re.S)
        data = json.loads(m.group(1))
        # Escaped JSON still decodes back to the original text.
        assert data["nodes"][0]["decision"] == "</script><b>pwn</b>"

    def test_skill_text_breakout_is_neutralized(self):
        skills = [
            {"id": "K1", "name": "p", "procedure": "</script><img onerror=alert(1)>"}
        ]
        h = render_graph_html([], skills=skills)
        assert "</script><img" not in h
        assert "\\u003c/script>" in h

    def test_reflection_text_breakout_is_neutralized(self):
        reflections = [
            {"id": "R1", "abstraction": "</script><script>alert('x')</script>"}
        ]
        h = render_graph_html([], reflections=reflections)
        assert "<script>alert" not in h
        assert "\\u003c/script>" in h

    def test_template_wires_lens_layout_filters_time(self):
        """The interactive controls must all be present in the rendered HTML."""
        h = render_graph_html([{"id": "D1", "decision": "a"}])
        # Lens + Layout dropdowns.
        assert 'id="lens"' in h
        assert 'value="ide"' in h and 'value="tag"' in h and 'value="age"' in h
        assert 'id="layout"' in h
        assert 'value="radial"' in h and 'value="timeline"' in h
        # Node-type + edge-kind filter checkboxes.
        for typ in ("decision", "file", "skill", "reflection"):
            assert f'data-type="{typ}"' in h
        for kind in ("supersedes", "touches", "depends", "induced", "covers"):
            assert f'data-kind="{kind}"' in h
        # Time scrubber + play button.
        assert 'id="timeLo"' in h and 'id="timeHi"' in h
        assert 'id="playBtn"' in h
        # URL hash + keyboard nav present in JS.
        assert "writeHashState" in h
        assert "readHashState" in h

    def test_template_wires_v2_enhancements(self):
        """The v3.1.x enhancement features must all be present."""
        h = render_graph_html([{"id": "D1", "decision": "a", "do_not_revert": True}])
        # Brand strip + hero banner + minimap + isolation chip
        assert 'id="brand"' in h
        assert 'id="hero"' in h
        assert 'id="minimap"' in h
        assert 'id="isoChip"' in h
        # Selection history + help dialog
        assert 'id="btnBack"' in h and 'id="btnFwd"' in h
        assert 'id="btnHelp"' in h
        assert 'id="helpDlg"' in h and 'id="helpBackdrop"' in h
        # Context menu + edge tooltip
        assert 'id="ctx"' in h
        assert 'id="tipE"' in h
        # Search badge + until: token
        assert 'id="qBadge"' in h
        assert "until:" in h
        # CSS palette tokens (one of each major token family)
        for tok in (
            "--bg-0",
            "--c-decision",
            "--c-protected",
            "--c-file",
            "--c-skill",
            "--c-reflection",
            "--accent",
        ):
            assert tok in h, f"missing palette token {tok}"
        # SVG filters for shadow + protected glow
        assert 'id="nodeShadow"' in h
        assert 'id="nodeGlow"' in h
        # JS landmarks
        for sym in (
            "GLYPHS",
            "HUB_IDS",
            "edgeIsCurved",
            "quadPath",
            "openCtx",
            "isolateSet",
            "showEdgeTip",
            "renderMinimap",
            "miniRecenter",
            "goBack",
            "goFwd",
            "openHelp",
            "renderHero",
            "fadeHero",
            "clearIsolation",
        ):
            assert sym in h, f"missing JS symbol {sym}"
        # Animated edge flow @keyframes
        assert "@keyframes flow" in h
        # Hub label CSS rule (always visible)
        assert ".node.hub text" in h
        # Accessibility roles + labels
        assert 'role="img"' in h
        assert 'role="tooltip"' in h
        assert 'role="dialog"' in h
        assert 'role="menu"' in h
        assert 'aria-label="Memory graph"' in h
        # Drop-shadow filter is actually applied via CSS on shapes
        assert "filter:url(#nodeShadow)" in h
        assert "filter:url(#nodeGlow)" in h

    def test_template_wires_v3_1x_search_qa_lineage(self):
        """v3.1.x viewer overhaul: ranked search panel + Q&A + outcome
        lens + lineage trace mode must all be wired into the template."""
        h = render_graph_html([{"id": "D1", "decision": "x", "outcome": "kept"}])
        # New panel containers
        assert 'id="rankedResults"' in h
        assert 'id="askAnswer"' in h
        # New lens option
        assert 'value="outcome"' in h
        # New JS landmarks
        for sym in (
            "renderRankedAndAsk",
            "_scoreForQuery",
            "_detectIntent",
            "_answerAbout",
            "_answerWhy",
            "_answerOutcome",
            "_answerProtected",
            # v3.2.0 vocab expansion
            "_answerWho",
            "_answerWhen",
            "_answerCompare",
            "enterLineageMode",
            "exitLineageMode",
            "lineage-mode",
        ):
            assert sym in h, f"missing JS symbol {sym}"

    def test_template_wires_v3_2x_qa_vocab_help(self):
        """v3.2.0 Q&A vocab expansion: cheatsheet must surface new patterns."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        for phrase in (
            "who decided X",
            "when did we X",
            "compare X and Y",
        ):
            assert phrase in h, f"cheatsheet missing prompt: {phrase}"
        # Lineage-mode CSS
        assert "svg.lineage-mode" in h
        # Rich-detail field classes
        for cls in (
            ".alts",
            ".re-examine",
            ".outcome-badge",
            ".chain",
        ):
            assert cls in h, f"missing CSS class {cls}"

    def test_protected_node_gets_protected_class_in_render(self):
        """Protected (do_not_revert) decisions must be marked so the glow
        filter applies. We verify the JS classList toggle is wired."""
        h = render_graph_html([{"id": "D1", "decision": "x", "do_not_revert": True}])
        assert "g.classList.add('protected')" in h

    def test_hub_classification_emits_in_render(self):
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert "HUB_IDS.has(n.id)" in h
        assert "g.classList.add('hub')" in h

    def test_initial_fit_zoom_has_floor(self):
        """First-paint must clamp zoom to >= 1.5 so nodes don't render tiny."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        # The fitToView's initial-paint floor lives in the JS body.
        assert "opts.initial" in h
        assert "Math.max(k, 1.5)" in h
        assert "{ initial: true }" in h

    def test_default_fit_margin_is_generous(self):
        """The default margin must be >= 80px to avoid bbox-fit packing."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        # The default margin assignment in fitToView.
        m = re.search(r"margin\s*=\s*margin\s*\|\|\s*(\d+);", h)
        assert m, "could not locate default-margin assignment"
        assert int(m.group(1)) >= 80, f"default margin too tight: {m.group(1)}"

    def test_node_radius_is_desktop_sized(self):
        """nodeRadius for decisions must start >= 10 px so hubs read at a glance."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        # Match the actual nodeRadius() formula for decisions.
        m = re.search(r"const base = n\.do_not_revert \? (\d+) : (\d+);", h)
        assert m, "could not locate base-radius formula"
        protected_base, base = int(m.group(1)), int(m.group(2))
        assert base >= 10, f"base node radius too small: {base}"
        assert protected_base > base, "protected base must exceed unprotected"

    def test_curved_edges_use_path_not_line(self):
        """touches/induced/covers must render as <path d='M..Q..'> not <line>."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert "edgeIsCurved" in h
        # edgeIsCurved must include the three semantic kinds
        m = re.search(r"function edgeIsCurved\(k\)\s*\{\s*return\s*([^;]+);", h)
        assert m, "could not locate edgeIsCurved body"
        body = m.group(1)
        for kind in ("touches", "induced", "covers"):
            assert f"'{kind}'" in body, f"{kind} missing from curved kinds"

    def test_until_search_token_recognized(self):
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        # parseQuery must accept both since and until.
        assert "^(since|until)" in h or "(since|until)" in h

    def test_ide_lens_disabled_when_single_origin(self):
        """When DATA.meta.ides has <2 entries the IDE lens option is disabled."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert "ideOpt.disabled = true" in h
        assert "(DATA.meta.ides || []).length < 2" in h

    def test_focused_id_bug_fix_present(self):
        """applyVisibility must clear focusedId at the top, not early-return."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        # The fix introduces a comment + the focusedId clear logic.
        assert "BUG FIX" in h
        assert "focusedId = null;" in h
        # And there is no early-return-on-focused-id anywhere.
        assert "if (focusedId !== null) return;" not in h

    def test_decision_labeled_by_text_not_id(self):
        """Legibility pass: labelFor must surface decision TEXT, not the
        opaque id. On pre-fix code the decision branch returned n.id, so a
        reader saw 'D000123' instead of the sentence."""
        h = render_graph_html([{"id": "D000123", "decision": "use bcrypt for hashing"}])
        # The decision branch of labelFor returns the shortened text.
        assert "if (n.type === 'decision') return shortLabel(n.decision)" in h
        # The shortLabel helper exists and truncates long text.
        assert "function shortLabel(s)" in h

    def test_decision_reflection_labels_visible_by_default(self):
        """Legibility pass: decision/reflection labels must be opacity>0 by
        default (they were 0 until hover/zoom on pre-fix code)."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert '.node[data-type="decision"] text' in h
        assert '.node[data-type="reflection"] text { opacity:0.9; }' in h

    def test_entry_node_selected_on_first_paint(self):
        """Legibility pass: INIT must auto-select an entry node so the
        details panel opens on a real decision, not an empty panel."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert "function pickEntryNode()" in h
        assert "if (!selectedId) {" in h
        assert "selectNode(entry, { noHist: true })" in h

    def test_self_contained_no_external_loads(self):
        """No CDN, no external scripts, no remote images even after the rewrite."""
        h = render_graph_html([{"id": "D1", "decision": "x"}])
        assert 'src="http' not in h
        assert "<link " not in h.lower()
        assert "cdn" not in h.lower()
        # No data URIs sneaking in (we don't need them).
        assert "data:image" not in h
        # No external font imports.
        assert "@import" not in h


# ---------------------------------------------------------------------------
# Multi-scenario rendering — exercise each data shape
# ---------------------------------------------------------------------------


class TestMultiScenarioRender:
    def _data_block(self, h: str) -> dict:
        m = re.search(r"const DATA = (\{.*?\});\n", h, re.S)
        assert m, "DATA block missing"
        return json.loads(m.group(1))

    def test_skills_reflections_supersession_multi_ide(self):
        """Full v3.1.0 data shape: every node type + every edge kind + multi-IDE."""
        decisions = [
            {
                "id": "D000001",
                "decision": "old auth",
                "tags": ["auth"],
                "file_path": "src/auth.py",
                "is_superseded": True,
                "superseded_by": "D000002",
                "ts": "2026-02-01T10:00:00+00:00",
                "session_id": "s1",
                "origin": {"ide": "cursor"},
            },
            {
                "id": "D000002",
                "decision": "new JWT auth",
                "tags": ["auth", "security"],
                "file_path": "src/auth.py",
                "do_not_revert": True,
                "ts": "2026-03-12T12:00:00+00:00",
                "session_id": "s2",
                "origin": {"ide": "claude_code"},
            },
            {
                "id": "D000003",
                "decision": "tests use real db",
                "tags": ["testing"],
                "file_path": "tests/test_db.py",
                "do_not_revert": True,
                "ts": "2026-04-22T15:00:00+00:00",
                "session_id": "s3",
                "origin": {"ide": "windsurf"},
            },
        ]
        skills = [
            {
                "id": "K000001",
                "name": "rebase-flow",
                "summary": "how we rebase",
                "procedure": "git fetch && git rebase",
                "triggers": {"tags": ["git"], "file_patterns": []},
                "source": "induced",
                "source_session_ids": ["s1"],
                "success_count": 3,
                "failure_count": 0,
                "status": "active",
                "ts": "2026-02-10T10:00:00+00:00",
                "origin": {"ide": "claude_code"},
            },
        ]
        reflections = [
            {
                "id": "R000001",
                "abstraction": "Auth refactor pattern emerged",
                "source_decision_ids": ["D000001", "D000002"],
                "tags": ["auth"],
                "ts": "2026-03-15T08:00:00+00:00",
                "period_start": "2026-02-01",
                "period_end": "2026-03-15",
                "confidence": 0.85,
                "model_used": "test",
                "origin": {"ide": "claude_code"},
            },
        ]
        h = render_graph_html(decisions, skills=skills, reflections=reflections)
        data = self._data_block(h)
        types = sorted({n["type"] for n in data["nodes"]})
        kinds = sorted({e["kind"] for e in data["edges"]})
        assert types == ["decision", "file", "reflection", "skill"]
        # All four "memory-edge" kinds present (no depends without a code graph DB).
        assert "supersedes" in kinds
        assert "touches" in kinds
        assert "induced" in kinds
        assert "covers" in kinds
        # Meta block carries every IDE (so the IDE lens has something to color).
        assert set(data["meta"]["ides"]) == {"cursor", "claude_code", "windsurf"}
        # Tag inventory includes every tag.
        assert {"auth", "security", "testing", "git"}.issubset(
            set(data["meta"]["tags"])
        )

    def test_large_synthetic_dataset_stays_under_size_cap(self):
        """50 decisions + 20 files + 10 skills + 5 reflections renders < 200 KB."""
        decisions = [
            {
                "id": f"D{i:06X}",
                "decision": f"decision text #{i}",
                "tags": [f"t{i % 5}"],
                "file_path": f"src/mod{i % 20}.py",
                "do_not_revert": i % 3 == 0,
                "ts": f"2026-{(i % 12) + 1:02d}-15T10:00:00+00:00",
                "session_id": f"s{i % 10}",
                "origin": {"ide": ["claude_code", "cursor", "windsurf"][i % 3]},
            }
            for i in range(50)
        ]
        skills = [
            {
                "id": f"K{i:06X}",
                "name": f"skill-{i}",
                "summary": f"summary {i}",
                "procedure": f"do thing {i}",
                "triggers": {"tags": [f"t{i % 5}"]},
                "source": "induced",
                "source_session_ids": [f"s{i}"],
                "success_count": i,
                "failure_count": 0,
                "status": "active",
                "ts": "2026-05-01T10:00:00+00:00",
                "origin": {"ide": "claude_code"},
            }
            for i in range(10)
        ]
        reflections = [
            {
                "id": f"R{i:06X}",
                "abstraction": f"abstraction {i}",
                "source_decision_ids": [f"D{j:06X}" for j in range(i * 5, i * 5 + 5)],
                "tags": [f"t{i}"],
                "ts": f"2026-{(i % 12) + 1:02d}-25T10:00:00+00:00",
                "period_start": "2026-01-01",
                "period_end": "2026-12-31",
                "confidence": 0.5 + i * 0.1,
                "model_used": "test",
                "origin": {"ide": "claude_code"},
            }
            for i in range(5)
        ]
        h = render_graph_html(decisions, skills=skills, reflections=reflections)
        data = self._data_block(h)
        assert data["meta"]["counts"]["decisions"] == 50
        assert data["meta"]["counts"]["files"] == 20
        assert data["meta"]["counts"]["skills"] == 10
        assert data["meta"]["counts"]["reflections"] == 5
        # Reflection-covers edges hit at least some real decisions.
        covers = [e for e in data["edges"] if e["kind"] == "covers"]
        assert covers, "expected reflection→decision covers edges"
        # Sanity check file size: well under 200 KB.
        assert len(h) < 200_000, f"viewer too large: {len(h)} bytes"

    def test_empty_decisions_with_only_skills_still_renders(self):
        """A project with skills but no decisions yet must still render."""
        skills = [
            {
                "id": "K1",
                "name": "first-skill",
                "procedure": "do it",
                "triggers": {"tags": ["x"]},
                "status": "active",
                "ts": "2026-05-01T00:00:00+00:00",
                "origin": {"ide": "claude_code"},
            },
        ]
        h = render_graph_html([], skills=skills, reflections=[])
        data = self._data_block(h)
        assert data["meta"]["counts"]["decisions"] == 0
        assert data["meta"]["counts"]["skills"] == 1
        assert "@@" not in h

    def test_supersession_chain_emits_arrow_link(self):
        """A 3-link supersession chain → 2 supersedes edges, no dangling."""
        decisions = [
            {
                "id": "D000001",
                "decision": "v1",
                "superseded_by": "D000002",
                "is_superseded": True,
            },
            {
                "id": "D000002",
                "decision": "v2",
                "superseded_by": "D000003",
                "is_superseded": True,
            },
            {"id": "D000003", "decision": "v3"},
        ]
        h = render_graph_html(decisions, with_files=False)
        data = self._data_block(h)
        sup_edges = [e for e in data["edges"] if e["kind"] == "supersedes"]
        assert len(sup_edges) == 2
        chain = {(e["source"], e["target"]) for e in sup_edges}
        assert ("D000001", "D000002") in chain
        assert ("D000002", "D000003") in chain


# ---------------------------------------------------------------------------
# JS syntax check — catches typos / missing brackets the moment they appear
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed in this environment"
)
class TestEmbeddedJsSyntax:
    def _extract_js(self, h: str) -> str:
        m = re.search(r"<script>(.*?)</script>", h, re.S)
        assert m, "no <script> block found in rendered HTML"
        return m.group(1)

    def _check(self, js: str, tmp_path: Path) -> None:
        f = tmp_path / "viewer.js"
        f.write_text(js, encoding="utf-8")
        # `node --check` is parse-only; identifiers like `document`/`window`
        # are unbound but that's fine (it doesn't execute).
        result = subprocess.run(
            ["node", "--check", str(f)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"node --check failed:\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}\n"
            f"(first 400 chars of JS: {js[:400]!r})"
        )

    def test_minimal_decision(self, tmp_path: Path):
        h = render_graph_html([{"id": "D1", "decision": "a"}])
        self._check(self._extract_js(h), tmp_path)

    def test_full_v3_dataset(self, tmp_path: Path):
        decisions = [
            {
                "id": "D1",
                "decision": "x",
                "do_not_revert": True,
                "tags": ["a"],
                "ts": "2026-01-01T00:00:00+00:00",
                "origin": {"ide": "cursor"},
                "file_path": "a.py",
            }
        ]
        skills = [
            {
                "id": "K1",
                "name": "n",
                "procedure": "p",
                "triggers": {"tags": ["a"]},
                "status": "active",
                "ts": "2026-02-01T00:00:00+00:00",
                "origin": {"ide": "claude_code"},
            }
        ]
        reflections = [
            {
                "id": "R1",
                "abstraction": "a",
                "source_decision_ids": ["D1"],
                "tags": ["a"],
                "ts": "2026-03-01T00:00:00+00:00",
                "confidence": 0.8,
                "period_start": "2026-01-01",
                "period_end": "2026-03-01",
                "model_used": "test",
            }
        ]
        h = render_graph_html(decisions, skills=skills, reflections=reflections)
        self._check(self._extract_js(h), tmp_path)

    def test_decision_with_xss_payload(self, tmp_path: Path):
        """Even with </script> in decision text, the embedded JS must still
        parse cleanly (the \\u003c escape neutralizes the breakout)."""
        decisions = [{"id": "D1", "decision": "</script><img onerror=alert(1)>"}]
        h = render_graph_html(decisions)
        self._check(self._extract_js(h), tmp_path)

    def test_large_synthetic_dataset(self, tmp_path: Path):
        """Make sure JSON-stringification of a large dataset stays valid JS."""
        decisions = [
            {
                "id": f"D{i:06X}",
                "decision": f"d {i}",
                "tags": [f"t{i % 5}"],
                "file_path": f"f{i}.py",
                "ts": f"2026-{(i % 12) + 1:02d}-15T10:00:00+00:00",
                "origin": {"ide": "claude_code"},
            }
            for i in range(100)
        ]
        h = render_graph_html(decisions)
        self._check(self._extract_js(h), tmp_path)


# ---------------------------------------------------------------------------
# cmd_graph — against an isolated store
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(project))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return project


class TestCmdGraph:
    def test_returns_2_when_no_decisions(self, isolated_store, capsys):
        from mcp_server.storage import paths as store_paths

        store_paths.ensure_dirs()  # initialized but empty
        rc = cmd_graph(out=str(isolated_store / "g.html"))
        assert rc == 2
        assert "nothing to visualize" in capsys.readouterr().err.lower()

    def test_writes_viewer_with_lineage(self, isolated_store):
        from mcp_server.tools.learning import record_decision, supersede_decision

        r = record_decision(decision="first decision", file_path="a.py", force=True)
        supersede_decision(
            old_id=r["decision_id"],
            new_decision="better decision",
            reason="superseded for test",
        )
        out = isolated_store / "viewer.html"
        rc = cmd_graph(out=str(out))
        assert rc == 0
        assert out.is_file()
        h = out.read_text(encoding="utf-8")
        m = re.search(r"const DATA = (\{.*?\});\n", h, re.S)
        data = json.loads(m.group(1))
        types = [n.get("type") for n in data["nodes"]]
        kinds = [e.get("kind") for e in data["edges"]]
        # 2 decisions (original + replacement) + 1 shared file node (a.py).
        assert types.count("decision") == 2
        assert types.count("file") == 1
        # Exactly one supersedes edge; both decisions touch the file.
        assert kinds.count("supersedes") == 1
        assert kinds.count("touches") == 2
