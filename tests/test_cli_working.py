"""
Tests for mcp_server.cli_working — v3.1.0 M2 Phase 3 CLI.

Covers ``codevira working commit <session_id>``:
  * usage error when session_id missing.
  * success path: live entries copied to working_archived.
  * no-op when session has no live entries.
  * storage errors surface to stderr with non-zero exit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.cli_working import cmd_working_commit
from mcp_server.storage import jsonl_store, paths, working_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


class TestCmdWorkingCommit:
    def test_missing_session_id_is_usage_error(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_working_commit(None)
        assert rc == 2
        err = capsys.readouterr().err
        assert "session_id is required" in err
        assert "Usage:" in err

    def test_commit_with_no_live_entries_is_no_op(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_working_commit("nonexistent-session")
        assert rc == 0
        out = capsys.readouterr().out
        assert "no live entries" in out

    def test_commit_copies_live_entries(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        working_store.add("first observation", session_id="ship-m2")
        working_store.add("second observation", session_id="ship-m2")
        working_store.add("other session", session_id="other-sess")

        rc = cmd_working_commit("ship-m2")
        assert rc == 0
        out = capsys.readouterr().out
        assert "copied 2" in out
        assert "ship-m2" in out

        # Verify archive file landed with the two entries.
        archive = paths.working_archived_path("ship-m2")
        assert archive.is_file()
        archived = jsonl_store.read_all(archive)
        contents = {r["content"] for r in archived}
        assert contents == {"first observation", "second observation"}

    def test_commit_excludes_evicted_entries(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wid_keep = working_store.add("keep me", session_id="s")
        wid_drop = working_store.add("drop me", session_id="s")
        working_store.mark_evicted(wid_drop)

        rc = cmd_working_commit("s")
        assert rc == 0
        out = capsys.readouterr().out
        assert "copied 1" in out

        archived = jsonl_store.read_all(paths.working_archived_path("s"))
        assert [r["id"] for r in archived] == [wid_keep]

    def test_commit_idempotent_appends(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        working_store.add("only entry", session_id="s")
        cmd_working_commit("s")
        capsys.readouterr()
        cmd_working_commit("s")
        archived = jsonl_store.read_all(paths.working_archived_path("s"))
        # Two appends of the same entry (documented behavior).
        assert len(archived) == 2

    def test_storage_failure_returns_one(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("synthetic")

        monkeypatch.setattr("mcp_server.storage.working_store.commit_session", _boom)
        rc = cmd_working_commit("any")
        assert rc == 1
        err = capsys.readouterr().err
        assert "unexpected error" in err
