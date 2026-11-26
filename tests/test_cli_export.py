"""
test_cli_export.py — v3.3.0 regression guard for the export bug found
2026-06-12: `codevira export decisions` read ONLY the legacy graph.db
and silently exported 0 rows on v3.x projects whose memory lives in
.codevira/*.jsonl. Verified live: 714 decisions in decisions.jsonl,
export produced 0 rows from all tables.

Covers:
  1. JSON export sources rows from .codevira/*.jsonl (the fix).
  2. JSON export works with NO graph.db at all (v3.x-only project).
  3. Per-table legacy fallback to graph.db when a JSONL file is absent.
  4. SQL format still requires graph.db (legacy-only path, unchanged).
  5. SQL format refuses when graph.db lacks rows the JSONL store holds
     (2026-09-25: it reported ✓ with 0 of 44 decisions).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from mcp_server.cli_export import cmd_export, export_decisions_to_path


@pytest.fixture()
def v3_project(tmp_path, monkeypatch):
    """A minimal v3.x project: .codevira/*.jsonl present, no graph.db."""
    (tmp_path / ".git").mkdir()  # project-root marker
    cv = tmp_path / ".codevira"
    cv.mkdir()
    decisions = [
        {"id": "D000001", "decision": "Use bcrypt", "do_not_revert": True},
        {"id": "D000002", "decision": "JSONL is canonical", "do_not_revert": False},
    ]
    sessions = [{"id": "S000001", "summary": "first session"}]
    (cv / "decisions.jsonl").write_text(
        "\n".join(json.dumps(d) for d in decisions) + "\n", encoding="utf-8"
    )
    (cv / "sessions.jsonl").write_text(json.dumps(sessions[0]) + "\n", encoding="utf-8")
    # Anchor BOTH path resolvers (mcp_server.paths + storage.paths) on the
    # tmp project via the env var both consult first.
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_json_export_reads_jsonl_store(v3_project):
    """THE regression test: rows must come from .codevira/*.jsonl."""
    out = v3_project / "backup.json"
    summary = export_decisions_to_path(out, format="json", target="decisions")

    assert summary["tables"]["decisions"] == 2
    assert summary["tables"]["sessions"] == 1
    assert summary["table_sources"]["decisions"] == "jsonl"

    payload = json.loads(out.read_text(encoding="utf-8"))
    exported_ids = {d["id"] for d in payload["tables"]["decisions"]}
    assert exported_ids == {"D000001", "D000002"}


def test_json_export_needs_no_graph_db(v3_project):
    """v3.x-only project (no graph.db anywhere) must still export."""
    out = v3_project / "backup.json"
    summary = export_decisions_to_path(out, format="json", target="decisions")
    assert summary["source_db"] is None
    assert summary["tables"]["decisions"] == 2
    # Tables with neither JSONL nor sqlite are reported, not invented.
    assert summary["table_sources"]["phases"] == "missing"
    assert summary["tables"]["phases"] == 0


def test_json_export_falls_back_to_sqlite_per_table(v3_project, monkeypatch):
    """A table missing from JSONL but present in legacy graph.db is
    sourced from graph.db — pre-v3 projects keep working."""
    legacy_db = v3_project / "legacy-graph.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute("CREATE TABLE phases (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO phases (name) VALUES ('legacy phase')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "mcp_server.cli_export._resolve_graph_db_path", lambda: legacy_db
    )

    out = v3_project / "backup.json"
    summary = export_decisions_to_path(out, format="json", target="decisions")

    # JSONL still wins where it exists...
    assert summary["table_sources"]["decisions"] == "jsonl"
    assert summary["tables"]["decisions"] == 2
    # ...sqlite fills the JSONL-less table.
    assert summary["table_sources"]["phases"] == "sqlite-legacy"
    assert summary["tables"]["phases"] == 1


def test_sql_format_still_requires_graph_db(v3_project):
    """SQL dumps are a graph.db feature; missing graph.db must raise,
    not write an empty file."""
    out = v3_project / "backup.sql"
    with pytest.raises(FileNotFoundError):
        export_decisions_to_path(out, format="sql", target="decisions")
    assert not out.exists()


def _empty_legacy_graph_db(project, monkeypatch, *, decision_rows=0):
    """graph.db with the legacy decision-tier schema — the shape every
    v3+ project has, since v3 never writes these tables (D000002)."""
    db = project / "graph.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT)")
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
    for i in range(decision_rows):
        conn.execute("INSERT INTO decisions (decision) VALUES (?)", (f"legacy {i}",))
    conn.commit()
    conn.close()
    monkeypatch.setattr("mcp_server.cli_export._resolve_graph_db_path", lambda: db)
    return db


def test_sql_export_refuses_when_graph_db_lacks_jsonl_rows(
    v3_project, monkeypatch, capsys
):
    """Found 2026-09-25 on a real project: `export --format sql` printed
    ✓ with 0 decisions while decisions.jsonl held 44. The SQL dump reads
    graph.db, which v3+ never writes — it must refuse, not ship an empty
    "backup"."""
    _empty_legacy_graph_db(v3_project, monkeypatch)
    out = v3_project / "backup.sql"

    rc = cmd_export("decisions", fmt="sql", out=str(out))
    err = capsys.readouterr().err

    assert rc == 1
    assert not out.exists()
    assert "decisions: 2 in .codevira/decisions.jsonl, 0 in graph.db" in err
    assert "sessions: 1 in .codevira/sessions.jsonl, 0 in graph.db" in err
    assert "--format json" in err


def test_sql_refusal_names_the_json_command_for_the_same_target(
    v3_project, monkeypatch, capsys
):
    """`export all` also carries the code graph. Pointing its refusal at the
    plain `export --format json` would trade one incomplete file for another."""
    _empty_legacy_graph_db(v3_project, monkeypatch)
    out = v3_project / "backup.sql"

    assert cmd_export("all", fmt="sql", out=str(out)) == 1
    assert "codevira export all --format json" in capsys.readouterr().err


def test_sql_export_still_works_when_graph_db_holds_the_rows(v3_project, monkeypatch):
    """The guard must not block a legacy project whose graph.db really
    does hold the data (pre-v3: no JSONL store)."""
    for name in ("decisions.jsonl", "sessions.jsonl"):
        (v3_project / ".codevira" / name).unlink()
    _empty_legacy_graph_db(v3_project, monkeypatch, decision_rows=3)
    out = v3_project / "backup.sql"

    assert cmd_export("decisions", fmt="sql", out=str(out)) == 0
    assert out.read_text(encoding="utf-8").count("INSERT INTO") == 3


def test_cmd_export_json_exit_zero_without_graph_db(v3_project, capsys):
    """CLI path: `codevira export decisions` succeeds on a v3.x project
    with no graph.db and reports JSONL-sourced row counts."""
    rc = cmd_export("decisions", fmt="json", out=str(v3_project / "out.json"))
    captured = capsys.readouterr()
    assert rc == 0
    assert "decisions" in captured.out
    assert "(jsonl)" in captured.out
