"""A project folder that vanishes under a running server must never be recreated.

THE DEFECT (reproduced on 4.2.0)
--------------------------------
An MCP server pinned with ``--project-dir /x/OLD`` keeps serving after the user
renames the folder to ``/x/NEW``. Its next ``record_decision`` RECREATED
``/x/OLD``, started a brand-new store at D000001 and answered
``recorded: true`` — memory silently split in two. Three causes stacked:
writers ``mkdir(parents=True)``; the opt-in gate caches positives for the life
of the process and accepts a centralized marker that outlives the folder; and
the dispatch gate was fail-open.

WHAT THESE TESTS DEFEND
-----------------------
* storage layer: ``ensure_dirs`` / ``get_data_dir`` / every raw writer refuse
  (``ProjectFolderMissingError``, a ``ValueError`` per D000140) and create
  nothing;
* dispatch: every WRITE tool and every READ tool returns a loud
  ``project_folder_missing`` error, before the opt-in gate, and works again
  without a restart once the folder is back;
* recreated-empty folder: if this process saw ``<root>/.codevira`` and it is
  gone, writes refuse even though a centralized marker exists;
* hooks and background work degrade quietly — no crash, no crash-log entry,
  no recreated folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import opt_in
from mcp_server import paths as paths_mod
from mcp_server.opt_in import WRITE_TOOLS, READ_TOOLS
from mcp_server.paths import ProjectFolderMissingError

REPO = Path(__file__).resolve().parent.parent


# ── helpers ────────────────────────────────────────────────────────────────


def _make_project(root: Path) -> Path:
    """An opted-in project: in-repo ``.codevira/config.yaml``."""
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: vanish\n")
    return root


def _pin(root: Path) -> None:
    """Pin this process to ``root`` the way ``--project-dir`` does."""
    paths_mod.set_project_dir(root)
    opt_in.invalidate_opt_in_cache()
    opt_in._seen_in_repo_store.clear()


@pytest.fixture
def vanish(tmp_path, monkeypatch):
    """(OLD, NEW): OLD is an opted-in project pinned for this process."""
    monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)
    monkeypatch.delenv("CODEVIRA_PROJECT_DIR", raising=False)
    old = _make_project(tmp_path / "OLD")
    new = tmp_path / "NEW"
    _pin(old)
    yield old, new
    opt_in._seen_in_repo_store.clear()
    paths_mod.reset_pinned_root()


def _call(name: str, args: dict | None = None) -> dict:
    from mcp_server.server import call_tool

    result = asyncio.run(call_tool(name, args or {}))
    return json.loads(result[0].text)


def _rename_away(old: Path, new: Path) -> None:
    old.rename(new)
    assert not old.exists()


# ── storage layer ──────────────────────────────────────────────────────────


class TestEnsureDirs:
    def test_is_a_value_error(self):
        # D000140: background work catches ValueError and degrades quietly.
        assert issubclass(ProjectFolderMissingError, ValueError)

    def test_refuses_and_creates_nothing(self, tmp_path):
        from mcp_server.storage import paths as sp

        gone = tmp_path / "does" / "not" / "exist"
        with pytest.raises(ProjectFolderMissingError) as ei:
            sp.ensure_dirs(gone)
        assert not (tmp_path / "does").exists(), "ensure_dirs recreated the path"
        msg = str(ei.value)
        # WHAT / WHY / FIX
        assert "no longer exists" in msg
        assert "moved, renamed or deleted" in msg
        assert "nothing was saved" in msg
        assert "reopen the project" in msg.lower()

    def test_existing_root_still_scaffolds(self, tmp_path):
        from mcp_server.storage import paths as sp

        sp.ensure_dirs(tmp_path)
        assert (tmp_path / ".codevira").is_dir()
        assert (tmp_path / ".codevira-cache").is_dir()

    def test_file_in_place_of_root_is_refused(self, tmp_path):
        from mcp_server.storage import paths as sp

        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(ProjectFolderMissingError):
            sp.ensure_dirs(f)

    def test_default_root_pinned_then_renamed(self, vanish):
        from mcp_server.storage import paths as sp

        old, new = vanish
        _rename_away(old, new)
        with pytest.raises(ProjectFolderMissingError):
            sp.ensure_dirs()
        assert not old.exists()


class TestGetDataDir:
    def test_refuses_vanished_root_without_creating(self, vanish, tmp_path):
        old, new = vanish
        _rename_away(old, new)
        with pytest.raises(ProjectFolderMissingError):
            paths_mod.get_data_dir()
        assert not old.exists()

    def test_cached_fast_path_also_refuses(self, vanish):
        old, new = vanish
        first = paths_mod.get_data_dir()  # populates _data_dir_cache
        assert first.is_dir()
        assert paths_mod._data_dir_cache, "cache not populated — test is vacuous"
        _rename_away(old, new)
        with pytest.raises(ProjectFolderMissingError):
            paths_mod.get_data_dir()

    def test_recovers_when_folder_returns(self, vanish):
        old, new = vanish
        _rename_away(old, new)
        with pytest.raises(ProjectFolderMissingError):
            paths_mod.get_data_dir()
        new.rename(old)
        assert paths_mod.get_data_dir().is_dir()

    def test_no_data_written_under_dead_key(self, vanish):
        old, new = vanish
        paths_mod.get_data_dir()
        _rename_away(old, new)
        projects = paths_mod.get_global_home() / "projects"
        before = sorted(p.name for p in projects.glob("*")) if projects.exists() else []
        with pytest.raises(ProjectFolderMissingError):
            paths_mod.get_data_dir()
        after = sorted(p.name for p in projects.glob("*")) if projects.exists() else []
        assert before == after

    def test_is_invalid_project_root_still_allows_nonexistent(self, tmp_path):
        # doctor C13 and 186 files depend on this: DO NOT tighten it.
        assert paths_mod.is_invalid_project_root(tmp_path / "gone") is None


class TestSharedPredicate:
    def test_project_root_exists(self, tmp_path):
        assert paths_mod.project_root_exists(tmp_path)
        assert not paths_mod.project_root_exists(tmp_path / "nope")

    def test_mkdir_in_project_refuses_inside_vanished_root(self, tmp_path):
        gone = tmp_path / "gone"
        with pytest.raises(ProjectFolderMissingError):
            paths_mod.mkdir_in_project(gone / ".codevira", gone)
        assert not gone.exists()

    def test_mkdir_in_project_allows_outside_root(self, tmp_path):
        gone = tmp_path / "gone"
        other = tmp_path / "elsewhere" / "deep"
        paths_mod.mkdir_in_project(other, gone)
        assert other.is_dir()

    def test_mkdir_in_project_noop_when_exists(self, tmp_path):
        paths_mod.mkdir_in_project(tmp_path, tmp_path / "gone")


# Writers that bypass ensure_dirs. Each takes OLD and must raise, not recreate.
def _w_atomic_text(old: Path):
    from mcp_server.storage import atomic

    atomic.atomic_write_text(old / ".codevira" / "x.txt", "x")


def _w_atomic_bytes(old: Path):
    from mcp_server.storage import atomic

    atomic.atomic_write_bytes(old / ".codevira" / "x.bin", b"x")


def _w_file_lock(old: Path):
    from mcp_server.storage import atomic

    with atomic.file_lock(old / ".codevira" / "x.lock"):
        pass


def _w_jsonl_append(old: Path):
    from mcp_server.storage import jsonl_store

    jsonl_store.append(old / ".codevira" / "decisions.jsonl", {"id": "D000001"})


def _w_manifest(old: Path):
    from mcp_server.storage import manifest

    manifest.incremental_add(old / ".codevira" / "manifest.yaml", {"id": "D000001"})


def _w_agents_md(old: Path):
    from mcp_server.storage import agents_md_generator as g

    g._merge_into_file(old / "AGENTS.md", "block")


def _w_mdc(old: Path):
    from mcp_server.storage import agents_md_generator as g

    g._write_owned_mdc(old / ".cursor" / "rules" / "codevira.mdc", "block", "p")


def _w_fts5(old: Path):
    from mcp_server.storage import fts5_index

    fts5_index._connect(old / ".codevira-cache" / "fts5.sqlite")


def _w_working_archive(old: Path):
    from mcp_server.storage import working_store

    working_store.commit_session("ad-hoc-abc123")


def _w_ensure_dirs(old: Path):
    from mcp_server.storage import paths as sp

    sp.ensure_dirs(old)


def _w_audit(old: Path):
    # audit.record is "never raises" — covered separately; use its mkdir.
    from mcp_server.engine import audit

    paths_mod.mkdir_in_project(audit.path(old).parent, old)


RAW_WRITERS = [
    _w_atomic_text,
    _w_atomic_bytes,
    _w_file_lock,
    _w_jsonl_append,
    _w_manifest,
    _w_agents_md,
    _w_mdc,
    _w_fts5,
    _w_working_archive,
    _w_ensure_dirs,
    _w_audit,
]


@pytest.mark.parametrize("writer", RAW_WRITERS, ids=lambda f: f.__name__)
def test_raw_writer_never_recreates_vanished_root(writer, vanish):
    old, new = vanish
    _rename_away(old, new)
    with pytest.raises(ValueError) as ei:
        writer(old)
    assert isinstance(ei.value, ProjectFolderMissingError), (
        f"{writer.__name__} raised {type(ei.value).__name__}: {ei.value}"
    )
    assert not old.exists(), f"{writer.__name__} recreated the vanished project root"


def test_playbook_promotion_never_recreates_root(vanish):
    """tools/skills.py writes a playbook with its own mkdir."""
    from mcp_server.paths import mkdir_in_project
    from mcp_server.storage import paths as sp

    old, new = vanish
    dest_dir = sp.codevira_dir(old) / "playbooks"
    _rename_away(old, new)
    with pytest.raises(ProjectFolderMissingError):
        mkdir_in_project(dest_dir)
    assert not old.exists()


def test_fix_history_does_not_mint_db_for_vanished_root(vanish):
    from indexer import fix_history

    old, new = vanish
    _rename_away(old, new)
    fix_history._conn_cache.pop(old.resolve(), None)
    key_dir = paths_mod.get_global_home() / "projects"
    before = sorted(p.name for p in key_dir.glob("*")) if key_dir.exists() else []
    with pytest.raises(ProjectFolderMissingError):
        fix_history._connect_locked(old)
    after = sorted(p.name for p in key_dir.glob("*")) if key_dir.exists() else []
    assert before == after
    assert not old.exists()


# ── dispatch gate (in-process) ─────────────────────────────────────────────


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_every_write_tool_refuses_when_folder_is_gone(tool, vanish):
    old, new = vanish
    _rename_away(old, new)
    before = sorted(p.name for p in new.rglob("*"))
    payload = _call(tool, {})
    assert payload["error"] == "project_folder_missing"
    assert payload["recorded"] is False
    assert (
        str(old.resolve()) == payload["project_root"]
        or str(old) in payload["project_root"]
    )
    assert payload["message"] and payload["fix_command"]
    assert not old.exists(), f"{tool} recreated the vanished root"
    assert sorted(p.name for p in new.rglob("*")) == before, "NEW was modified"


@pytest.mark.parametrize("tool", sorted(READ_TOOLS))
def test_every_read_tool_errors_loudly_when_folder_is_gone(tool, vanish):
    old, new = vanish
    _rename_away(old, new)
    payload = _call(tool, {})
    assert payload["error"] == "project_folder_missing"
    assert payload["recorded"] is False
    assert not old.exists()


def test_gate_runs_before_cached_opt_in(vanish):
    """The opt-in positive is cached for the process; that must not matter."""
    old, new = vanish
    assert opt_in.is_project_opted_in(old)  # caches True
    _rename_away(old, new)
    assert opt_in.is_project_opted_in(old), "premise: cache still says opted in"
    assert _call("record_decision", {"decision": "x"})["error"] == (
        "project_folder_missing"
    )


def test_gate_runs_despite_centralized_marker(vanish):
    old, new = vanish
    key = paths_mod._sanitize_path_key(old.resolve())
    central = paths_mod.get_global_home() / "projects" / key
    central.mkdir(parents=True)
    (central / "config.yaml").write_text("project:\n  name: x\n")
    _rename_away(old, new)
    assert opt_in._has_config_marker(old.resolve()), "premise: marker survives"
    assert _call("record_decision", {"decision": "x"})["error"] == (
        "project_folder_missing"
    )
    assert not old.exists()


def test_folder_returns_without_restart(vanish):
    old, new = vanish
    first = _call("record_decision", {"decision": "first decision before rename"})
    assert first.get("recorded") is True, first
    _rename_away(old, new)
    assert _call("record_decision", {"decision": "while gone"})["error"] == (
        "project_folder_missing"
    )
    new.rename(old)
    second = _call("record_decision", {"decision": "second decision after return"})
    assert second.get("recorded") is True, second
    assert second["decision_id"] != first["decision_id"]
    lines = (old / ".codevira" / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_rebinding_to_new_folder_keeps_working(vanish):
    """A client that reports the new folder re-binds; the gate runs AFTER it."""
    old, new = vanish
    _rename_away(old, new)
    assert _call("get_session_context")["error"] == "project_folder_missing"
    _pin(new)  # what _bind_project_from_client_roots / tool-path binding does
    payload = _call("record_decision", {"decision": "recorded in the new home"})
    assert payload.get("recorded") is True, payload
    assert (new / ".codevira" / "decisions.jsonl").is_file()
    assert not old.exists()


def test_gate_is_not_fail_open(vanish, monkeypatch):
    """If the answer cannot be read, the call must not proceed to a write."""
    old, new = vanish

    def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(opt_in, "project_folder_gate", boom)
    from mcp_server.server import call_tool

    with pytest.raises(RuntimeError):
        asyncio.run(call_tool("record_decision", {"decision": "x"}))
    assert not (old / ".codevira" / "decisions.jsonl").exists()


def test_gate_cost_is_small(vanish):
    import time

    old, new = vanish
    opt_in.project_folder_gate("record_decision")
    t0 = time.perf_counter()
    for _ in range(200):
        opt_in.project_folder_gate("record_decision")
    per_call_ms = (time.perf_counter() - t0) / 200 * 1000
    assert per_call_ms < 1.0, f"gate costs {per_call_ms:.3f} ms per dispatch"


# ── recreated-empty folder ─────────────────────────────────────────────────


def _central_marker(root: Path) -> Path:
    key = paths_mod._sanitize_path_key(root.resolve())
    central = paths_mod.get_global_home() / "projects" / key
    central.mkdir(parents=True, exist_ok=True)
    (central / "config.yaml").write_text("project:\n  name: x\n")
    return central


class TestRecreatedEmptyFolder:
    def test_write_refuses_when_seen_store_disappeared(self, vanish):
        old, new = vanish
        _central_marker(old)
        assert _call("get_session_context").get("error") != "project_folder_missing"
        _rename_away(old, new)
        (old / ".claude").mkdir(parents=True)  # another program recreated it
        payload = _call("record_decision", {"decision": "must not split memory"})
        assert payload["error"] == "project_folder_missing"
        assert payload["recorded"] is False
        assert "no longer in" in payload["message"]
        assert not (old / ".codevira").exists()
        assert sorted(p.name for p in old.iterdir()) == [".claude"]

    def test_every_write_tool_refuses(self, vanish):
        old, new = vanish
        _central_marker(old)
        _call("get_session_context")
        _rename_away(old, new)
        (old / ".claude").mkdir(parents=True)
        for tool in sorted(WRITE_TOOLS):
            assert _call(tool, {})["error"] == "project_folder_missing", tool
        assert not (old / ".codevira").exists()

    def test_reads_are_not_blocked_by_the_store_check(self, vanish):
        old, new = vanish
        _central_marker(old)
        _call("get_session_context")
        _rename_away(old, new)
        (old / ".claude").mkdir(parents=True)
        payload = _call("list_tags")
        assert payload.get("error") != "project_folder_missing"

    def test_init_again_restores_writes_without_restart(self, vanish):
        old, new = vanish
        _central_marker(old)
        _call("get_session_context")
        _rename_away(old, new)
        (old / ".claude").mkdir(parents=True)
        assert _call("record_decision", {"decision": "x"})["error"] == (
            "project_folder_missing"
        )
        # `codevira init` writes the in-repo config.yaml again.
        (old / ".codevira").mkdir()
        (old / ".codevira" / "config.yaml").write_text("project:\n  name: x\n")
        payload = _call("record_decision", {"decision": "after init again"})
        assert payload.get("recorded") is True, payload

    def test_centralized_only_project_keeps_working(self, tmp_path, monkeypatch):
        """No in-repo .codevira EVER seen in this process: unaffected."""
        monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)
        root = tmp_path / "central_only"
        root.mkdir()
        _central_marker(root)
        _pin(root)
        try:
            for _ in range(2):
                assert opt_in.project_folder_gate("record_decision") is None
            payload = _call("record_decision", {"decision": "legit central only"})
            assert payload.get("recorded") is True, payload
        finally:
            opt_in._seen_in_repo_store.clear()


# ── hooks: never crash the editor, never recreate ──────────────────────────


class TestHooksDegradeQuietly:
    def test_audit_record_never_raises_or_recreates(self, vanish):
        from mcp_server.engine import audit

        old, new = vanish
        _rename_away(old, new)
        audit.record(
            project_root=old,
            event_type="PRE_TOOL_USE",
            session_id="s",
            action="allow",
            policies=[],
            target=None,
            metadata=None,
            eligible=1,
        )
        assert not old.exists()

    def test_session_log_enforcer_degrades(self, vanish):
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies import session_log_enforcer as sle
        from mcp_server.engine.signals import SignalContext

        old, new = vanish
        _rename_away(old, new)
        policy = sle.SessionLogEnforcer()
        for et in (EventType.SESSION_START, EventType.STOP):
            ev = HookEvent(
                event_type=et,
                tool_name=None,
                project_root=old,
                session_id="sess-1",
                timestamp=1.0,
            )
            verdict = policy.evaluate(ev, SignalContext(project_root=old))
            assert not verdict.is_blocking()
        sle._record_outcome(
            old, session_id="s", outcome="x", commit_count=0, mode="warn"
        )
        assert not old.exists()

    def test_token_meter_persist_refuses_without_minting_dir(self, vanish):
        from mcp_server.engine import token_meter

        old, new = vanish
        _rename_away(old, new)
        projects = paths_mod.get_global_home() / "projects"
        before = sorted(p.name for p in projects.glob("*")) if projects.exists() else []
        with pytest.raises(ProjectFolderMissingError):
            token_meter._persist_session_summary(old, {"session_id": "s"})
        after = sorted(p.name for p in projects.glob("*")) if projects.exists() else []
        assert before == after

    def test_claude_code_hook_entry_survives_vanished_root(self, vanish, monkeypatch):
        import io

        from mcp_server.engine.wiring import claude_code_hooks

        old, new = vanish
        _central_marker(old)  # keeps the opt-in gate open: the hook runs fully
        payload = json.dumps(
            {
                "session_id": "s1",
                "cwd": str(old),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(old / "a.py"), "new_string": "x"},
            }
        )
        _rename_away(old, new)
        for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
            monkeypatch.setattr("sys.stdin", io.StringIO(payload))
            rc = claude_code_hooks.handle(event)
            assert rc in (0, 1), (event, rc)  # returned — did not raise
            assert not old.exists(), f"{event} hook recreated the project root"

    def test_hook_subprocess_exits_clean_and_recreates_nothing(self, tmp_path):
        old = _make_project(tmp_path / "OLD")
        new = tmp_path / "NEW"
        old.rename(new)
        env = dict(os.environ, CODEVIRA_PROJECT_DIR=str(old), PYTHONPATH=str(REPO))
        env["CODEVIRA_HOME"] = str(tmp_path / "home")
        payload = json.dumps(
            {"session_id": "s", "cwd": str(old), "tool_name": "Edit", "tool_input": {}}
        )
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            proc = subprocess.run(
                [sys.executable, "-m", "mcp_server", "engine", "handle", event],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_path),
                timeout=60,
            )
            assert proc.returncode in (0, 1), (event, proc.returncode, proc.stderr)
            assert "Traceback" not in proc.stderr, proc.stderr
            assert not old.exists(), f"{event} hook recreated {old}"


# ── background work: quiet ─────────────────────────────────────────────────


class TestBackgroundWorkIsQuiet:
    def _crash_log_text(self) -> str:
        log = paths_mod.get_global_home() / "logs" / "crashes.log"
        return log.read_text() if log.exists() else ""

    def test_log_retention_returns_skip(self, vanish):
        from mcp_server.log_retention import enforce_retention

        old, new = vanish
        _rename_away(old, new)
        result = enforce_retention()
        assert result["ran"] is False
        assert not old.exists()
        assert self._crash_log_text() == ""

    def test_watcher_incremental_reindex_degrades(self, vanish):
        from indexer.index_codebase import cmd_incremental

        old, new = vanish
        _rename_away(old, new)
        cmd_incremental(quiet=True)  # must return, not raise
        assert not old.exists()
        assert self._crash_log_text() == ""

    def test_git_fix_scan_degrades(self, vanish):
        from indexer.fix_history import scan_git_log

        old, new = vanish
        _rename_away(old, new)
        summary = scan_git_log(old)
        assert "error" in summary
        assert not old.exists()

    def test_crash_log_ignores_project_folder_missing(self, vanish):
        from mcp_server._safe_crash import safe_log_crash

        old, new = vanish
        safe_log_crash(ProjectFolderMissingError(old), context="background watcher")
        assert self._crash_log_text() == ""

    def test_crash_log_still_records_real_errors(self, vanish):
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(ValueError("a genuine bug"), context="somewhere")
        assert "a genuine bug" in self._crash_log_text()


class TestInFlightBackgroundThreads:
    """A thread that captured OLD's paths BEFORE the rename must not rebuild OLD.

    This was a real, intermittent leak: the first tool call starts auto-init on
    a background thread, the user renames the folder, and the thread's
    ``mkdir(parents=True)`` recreated ``OLD/.codevira/graph``.
    """

    def test_auto_init_thread_does_not_recreate_root(self, vanish, monkeypatch):
        from mcp_server import auto_init

        old, new = vanish
        data_dir = old / ".codevira"  # captured before the rename

        def detect_then_rename(project_root):
            # Deterministic version of the race: the folder is renamed after
            # the thread started but before it creates its directories.
            _rename_away(old, new)
            return {}

        monkeypatch.setattr("mcp_server.detect.auto_detect_project", detect_then_rename)
        auto_init._run_background_init(old, data_dir, {})
        assert not old.exists()

    def test_sqlite_graph_does_not_recreate_root(self, vanish):
        from indexer.sqlite_graph import SQLiteGraph

        old, new = vanish
        db = old / ".codevira" / "graph" / "graph.db"
        _rename_away(old, new)
        with pytest.raises(ProjectFolderMissingError):
            SQLiteGraph(db)
        assert not old.exists()

    def test_repair_init_does_not_recreate_root(self, vanish):
        from mcp_server import _repair_init

        old, new = vanish
        _central_marker(old)  # keeps the opt-in guard inside repair open
        _rename_away(old, new)
        try:
            _repair_init.repair_incomplete_init(old / ".codevira", old)
        except ValueError:
            pass
        assert not old.exists()


# ── tools that legitimately inspect a vanished project ─────────────────────


class TestVanishedProjectStillInspectable:
    def test_untrack_a_vanished_project(self, vanish, monkeypatch):
        from mcp_server import cli

        old, new = vanish
        key = paths_mod._sanitize_path_key(old.resolve())
        data = paths_mod.get_global_home() / "projects" / key
        data.mkdir(parents=True)
        (data / "config.yaml").write_text("schema_version: 1\n")
        _rename_away(old, new)
        rc = cli.cmd_untrack(path=str(old), yes=True)
        assert rc == 0
        assert not data.exists()
        assert not old.exists()

    def test_projects_listing_with_vanished_project(self, vanish, capsys):
        from mcp_server.cli_projects import cmd_projects

        old, new = vanish
        key = paths_mod._sanitize_path_key(old.resolve())
        data = paths_mod.get_global_home() / "projects" / key
        data.mkdir(parents=True)
        (data / "config.yaml").write_text("schema_version: 1\n")
        _rename_away(old, new)
        assert cmd_projects() in (0, None)
        assert not old.exists()

    def test_prune_orphans_dry_run_with_vanished_project(self, vanish):
        from mcp_server import cli

        old, new = vanish
        key = paths_mod._sanitize_path_key(old.resolve())
        data = paths_mod.get_global_home() / "projects" / key
        data.mkdir(parents=True)
        (data / "config.yaml").write_text("schema_version: 1\n")
        _rename_away(old, new)
        cli.cmd_clean(dry_run=True, yes=True, orphans_only=True)
        assert data.exists(), "dry run must not delete"
        assert not old.exists()

    def test_doctor_reports_instead_of_crashing(self, vanish):
        from mcp_server import doctor

        old, new = vanish
        _rename_away(old, new)
        report = doctor.run_all_checks()  # must return a report, not raise
        assert report.results, "doctor produced no checks"
        assert not old.exists(), "doctor recreated the vanished project root"

    def test_c13_still_inspects_nonexistent_paths(self, tmp_path):
        # is_invalid_project_root() deliberately allows non-existent paths.
        assert paths_mod.is_invalid_project_root(tmp_path / "gone" / "deeper") is None


# ── end to end over the real stdio server ──────────────────────────────────


class _StdioServer:
    """Newline-delimited JSON-RPC against ``python -m mcp_server``."""

    def __init__(self, project: Path, home: Path):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        env["CODEVIRA_HOME"] = str(home)
        env["CODEVIRA_NO_UPDATE_CHECK"] = "1"
        env["CODEVIRA_NO_GIT_SCAN"] = "1"
        env.pop("CODEVIRA_PROJECT_DIR", None)
        env.pop("CODEVIRA_AUTO_ADOPT", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server", "--project-dir", str(project)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=str(home),
        )
        self._id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        assert self.proc.stdout is not None
        self._id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        while True:
            line = self.proc.stdout.readline()
            assert line, "server closed stdout"
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def tool(self, name: str, args: dict) -> dict:
        resp = self.request("tools/call", {"name": name, "arguments": args})
        assert "result" in resp, resp
        return json.loads(resp["result"]["content"][0]["text"])

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def test_end_to_end_rename_while_server_runs(tmp_path):
    old = _make_project(tmp_path / "OLD")
    new = tmp_path / "NEW"
    home = tmp_path / "home"
    home.mkdir()
    srv = _StdioServer(old, home)
    try:
        first = srv.tool("record_decision", {"decision": "decision before the rename"})
        assert first.get("recorded") is True, first
        assert first["decision_id"] == "D000001", first

        old.rename(new)
        before = (new / ".codevira" / "decisions.jsonl").read_bytes()

        refused = srv.tool("record_decision", {"decision": "while the folder is gone"})
        assert refused["error"] == "project_folder_missing", refused
        assert refused["recorded"] is False
        assert not old.exists(), "server recreated the old project folder"
        assert (new / ".codevira" / "decisions.jsonl").read_bytes() == before

        read = srv.tool("get_session_context", {})
        assert read["error"] == "project_folder_missing", read
        assert not old.exists()

        new.rename(old)
        second = srv.tool("record_decision", {"decision": "after the folder is back"})
        assert second.get("recorded") is True, second
        assert second["decision_id"] == "D000002", second
    finally:
        srv.close()
