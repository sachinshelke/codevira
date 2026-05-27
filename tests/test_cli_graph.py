"""Tests for `codevira graph` — the self-contained memory viewer (D000016).

Covers the pure render/build helpers (no I/O) plus the cmd_graph entry
point against an isolated JSONL store.
"""

from __future__ import annotations

import json
import re
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
