"""Tests for the `codevira search` CLI command (v3.6.0).

Exercises the JSON path of cmd_search (rendering-agnostic, so it's immune to
rich-mock leakage), plus a subprocess smoke test that the subcommand is wired
into the argument parser.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import mcp_server.paths as core_paths
from mcp_server.cli_search import cmd_search
from mcp_server.storage import decisions_store

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _project(tmp_path: Path, name: str, decisions: list[str]) -> Path:
    root = tmp_path / name
    (root / ".codevira").mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    core_paths.set_project_dir(root)
    core_paths.invalidate_data_dir_cache()
    for text in decisions:
        decisions_store.record(decision=text, file_path="x.py")
    return root


class TestCmdSearch:
    def test_empty_query_returns_2(self, capsys):
        assert cmd_search(query="   ", output_json=True) == 2

    def test_json_finds_current_project_decision(self, tmp_path, capsys):
        _project(tmp_path, "solo", ["retry uses exponential backoff"])
        rc = cmd_search(query="retry", output_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] >= 1
        assert any("retry" in (r.get("decision") or "") for r in payload["results"])

    def test_no_results_returns_0(self, tmp_path, capsys):
        _project(tmp_path, "solo", ["caching uses redis"])
        rc = cmd_search(query="quantumflux", output_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 0

    def test_all_projects_json_tags_each_row(self, tmp_path, monkeypatch, capsys):
        p1 = _project(tmp_path, "alpha", ["retry uses backoff with jitter"])
        p2 = _project(tmp_path, "beta", ["retry caps at three attempts"])
        entries = [
            SimpleNamespace(canonical_path=str(p1), name="alpha-svc"),
            SimpleNamespace(canonical_path=str(p2), name="beta-svc"),
        ]
        monkeypatch.setattr(
            "mcp_server._project_inventory.enumerate_projects", lambda: entries
        )
        rc = cmd_search(query="retry", all_projects=True, output_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert {r["project"] for r in payload["results"]} == {"alpha-svc", "beta-svc"}

    def test_limit_clamped(self, tmp_path, capsys):
        _project(tmp_path, "solo", [f"retry strategy number {i}" for i in range(5)])
        cmd_search(query="retry", limit=2, output_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] <= 2


class TestMarkupSafety:
    def test_decision_with_rich_markup_renders_literally(self, tmp_path):
        """A decision containing rich-markup-looking brackets (`[provider]`,
        `[/]`) must render literally via the table, not crash to the fallback
        or get silently eaten. Run the real CLI in a subprocess (real rich)."""
        proj = tmp_path / "proj"
        (proj / ".codevira").mkdir(parents=True)
        (proj / "pyproject.toml").write_text("", encoding="utf-8")
        seed = (
            "import mcp_server.paths as p; p.set_project_dir(r'%s'); "
            "p.invalidate_data_dir_cache(); "
            "from mcp_server.storage import decisions_store as d; "
            "d.record(decision='uses [provider] and an unbalanced [/] tag', "
            "file_path='x.py')" % str(proj)
        )
        subprocess.run(
            [sys.executable, "-c", seed], cwd=str(proj), check=True, timeout=20
        )
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server", "search", "provider"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(proj),
        )
        assert result.returncode == 0
        # The bracketed token survives in the output (escaped, not parsed away).
        assert "[provider]" in result.stdout


class TestCliWiring:
    def test_search_subcommand_help_parses(self):
        """`codevira search --help` exits 0 → the subcommand is registered."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server", "search", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(_REPO_ROOT),
        )
        out = result.stdout + result.stderr
        assert result.returncode == 0
        assert "--all-projects" in out
