"""
test_watcher_coalescing.py — the watcher must never queue reindexes.

GitHub issue #19 (2026-10-14, 4.2.0): the debounce only cancelled a timer that
had not fired yet. Once a reindex outlasted DEBOUNCE_SECONDS, every save added
one more thread blocked on the index lock, each of which later ran its own
pass: 357 threads and 83% CPU after six hours. Several MCP servers watching one
project made it worse, because the lock was per-process and they collided on
SQLite ("database is locked") for 30 s at a time.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import indexer.index_codebase as ic

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ic, "DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(ic, "_index_write_lock", threading.Lock())
    ic.reset_watcher_circuit()
    yield
    ic.reset_watcher_circuit()


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / ".git").mkdir()
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))
    (proj / ".codevira").mkdir()
    (proj / ".codevira/config.yaml").write_text(
        "project:\n  watched_dirs: [src]\n  file_extensions: ['.py']\n"
    )
    from mcp_server.paths import invalidate_data_dir_cache

    invalidate_data_dir_cache()
    yield proj
    invalidate_data_dir_cache()


def _wait(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _threads() -> int:
    return sum(t.name == ic.WATCHER_THREAD_NAME for t in threading.enumerate())


class _Slow:
    """A reindex that blocks until told to finish, and counts overlaps."""

    def __init__(self) -> None:
        self.starts = 0
        self.running = 0
        self.worst = 0
        self.go = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, **_: Any) -> None:
        with self._lock:
            self.starts += 1
            self.running += 1
            self.worst = max(self.worst, self.running)
        self.go.wait(10)
        with self._lock:
            self.running -= 1


class TestNoPileUp:
    def test_saves_during_a_slow_reindex_add_no_threads(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE regression test for issue #19."""
        slow = _Slow()
        monkeypatch.setattr(ic, "cmd_incremental", slow)
        r = ic.ReindexCoalescer(quiet=True)
        r.request()
        assert _wait(lambda: slow.starts == 1)
        for _ in range(30):
            r.request()
            assert _threads() == 1, "one reindex in flight, nothing beside it"
            time.sleep(0.03)  # longer than the debounce, as real saves are
        slow.go.set()
        assert _wait(lambda: _threads() == 0)
        assert slow.starts == 2, "30 saves collapse into exactly one more pass"
        assert slow.worst == 1

    def test_the_last_save_is_never_lost(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []
        version = [0]

        def reindex(**_: Any) -> None:
            seen.append(version[0])
            time.sleep(0.05)

        monkeypatch.setattr(ic, "cmd_incremental", reindex)
        r = ic.ReindexCoalescer(quiet=True)
        for i in range(1, 41):
            version[0] = i
            r.request()
            time.sleep(0.007 * (i % 5))
        assert _wait(lambda: _threads() == 0 and bool(seen) and seen[-1] == 40)

    def test_a_burst_of_saves_is_one_reindex(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: list[int] = []
        monkeypatch.setattr(ic, "cmd_incremental", lambda **_: runs.append(1))
        monkeypatch.setattr(ic, "DEBOUNCE_SECONDS", 0.15)
        r = ic.ReindexCoalescer(quiet=True)
        for _ in range(20):
            r.request()
        assert _wait(lambda: bool(runs) and _threads() == 0)
        assert len(runs) == 1

    def test_a_timer_firing_mid_run_does_not_start_a_second_run(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cancel() on a timer that already fired does nothing."""
        slow = _Slow()
        monkeypatch.setattr(ic, "cmd_incremental", slow)
        r = ic.ReindexCoalescer(quiet=True)
        r.request()
        assert _wait(lambda: slow.starts == 1)
        for _ in range(5):
            r._run()  # what a late timer does
        assert slow.worst == 1 and slow.starts == 1
        slow.go.set()
        assert _wait(lambda: _threads() == 0 and slow.starts == 2)

    def test_a_failing_reindex_does_not_wedge_the_watcher(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def reindex(**_: Any) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ic, "cmd_incremental", reindex)
        r = ic.ReindexCoalescer(quiet=True)
        r.request()
        assert _wait(lambda: len(calls) == 1 and _threads() == 0)
        assert ic.watcher_circuit_status()["consecutive_failures"] == 1
        r.request()
        assert _wait(lambda: len(calls) == 2 and _threads() == 0)
        assert ic.watcher_circuit_status()["consecutive_failures"] == 0

    def test_real_file_events_reach_it(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slow = _Slow()
        monkeypatch.setattr(ic, "cmd_incremental", slow)
        obs = ic.start_background_watcher(quiet=True)
        try:
            f = project / "src" / "a.py"
            for i in range(12):
                f.write_text(f"x = {i}\n")
                time.sleep(0.1)
            assert _wait(lambda: slow.starts == 1)
            assert _threads() <= 1
        finally:
            slow.go.set()
            obs.stop()
            obs.join(5)
        assert _wait(lambda: _threads() == 0)
        assert slow.worst == 1


class TestCircuit:
    def _open(self) -> None:
        for _ in range(ic._CIRCUIT_OPEN_THRESHOLD):
            ic._watcher_circuit_record_failure(RuntimeError("x"))

    def test_opening_while_waiting_for_the_lock_stops_the_run(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check used to happen only before blocking on the lock."""
        runs: list[int] = []
        monkeypatch.setattr(ic, "cmd_incremental", lambda **_: runs.append(1))
        r = ic.ReindexCoalescer(quiet=True)
        ic._index_write_lock.acquire()
        try:
            r.request()
            assert _wait(lambda: _threads() == 1)
            time.sleep(0.1)
            self._open()
        finally:
            ic._index_write_lock.release()
        time.sleep(0.2)
        assert runs == []
        r.cancel()

    def test_a_save_while_open_is_indexed_when_it_closes(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: list[int] = []
        monkeypatch.setattr(ic, "cmd_incremental", lambda **_: runs.append(1))
        monkeypatch.setattr(ic, "_CIRCUIT_BACKOFF_INITIAL", 0.3)
        self._open()
        r = ic.ReindexCoalescer(quiet=True)
        for _ in range(10):
            r.request()
            time.sleep(0.01)
        assert runs == [] and _threads() <= 1
        assert _wait(lambda: len(runs) == 1 and _threads() == 0)


_HOLDER = """
import sys, time
import indexer.index_codebase as ic
with ic.index_write_guard() as held:
    print("held" if held else "no", flush=True)
    sys.stdin.read()
"""


def _holder(project: Path) -> subprocess.Popen[str]:
    p = subprocess.Popen(
        [sys.executable, "-c", _HOLDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")
class TestAcrossProcesses:
    def test_a_second_process_waits(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: list[int] = []
        monkeypatch.setattr(ic, "cmd_incremental", lambda **_: runs.append(1))
        p = _holder(project)
        try:
            r = ic.ReindexCoalescer(quiet=True)
            for _ in range(10):
                r.request()
                time.sleep(0.05)
            assert runs == [] and _threads() <= 1
        finally:
            p.communicate("")
        assert _wait(lambda: len(runs) >= 1 and _threads() == 0)

    def test_a_killed_process_does_not_keep_the_lock(self, project: Path) -> None:
        p = _holder(project)
        p.kill()
        p.wait()
        with ic.index_write_guard(timeout=2) as held:
            assert held

    def test_a_bounded_wait_gives_up(self, project: Path) -> None:
        p = _holder(project)
        try:
            t = time.monotonic()
            with ic.index_write_guard(timeout=0.3) as held:
                assert not held
            assert time.monotonic() - t < 3
        finally:
            p.communicate("")

    def test_different_projects_do_not_wait_for_each_other(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _holder(project)
        try:
            other = tmp_path / "other"
            (other / ".codevira").mkdir(parents=True)
            (other / ".git").mkdir()
            (other / ".codevira/config.yaml").write_text("project: {}\n")
            monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(other))
            with ic.index_write_guard(timeout=0.5) as held:
                assert held
        finally:
            p.communicate("")

    def test_processes_reindexing_together_never_hit_a_locked_database(
        self, project: Path
    ) -> None:
        for i in range(25):
            (project / "src" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
        code = (
            "import sys,pathlib,indexer.index_codebase as ic\n"
            "p=pathlib.Path(sys.argv[1])\n"
            "for i in range(6):\n"
            "    (p/'src'/f'w{sys.argv[2]}.py').write_text(f'def g{i}(): return {i}\\n')\n"
            "    ic._locked_incremental(True)\n"
        )
        ps = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(project), str(n)],
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(REPO),
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            for n in range(4)
        ]
        errs = [p.communicate(timeout=120)[1] for p in ps]
        assert [p.returncode for p in ps] == [0] * 4, errs


class TestGuardDegrades:
    def test_no_resolvable_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Background work degrades; cmd_incremental reports it (D000140)."""

        def refuse() -> Path:
            raise ValueError("refuses invalid project root")

        monkeypatch.setattr(ic, "get_data_dir", refuse)
        with ic.index_write_guard(timeout=1) as held:
            assert held

    def test_an_unwritable_data_dir_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        monkeypatch.setattr(ic, "get_data_dir", lambda: blocker / "sub")
        with ic.index_write_guard(timeout=1) as held:
            assert held


def test_an_unchanged_project_is_not_reindexed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import indexer.graph_generator as gg

    (project / "src" / "a.py").write_text("x = 1\n")
    ic.cmd_incremental(quiet=True)

    def unexpected(*a: Any, **k: Any) -> Any:
        raise AssertionError("nothing changed")

    monkeypatch.setattr(gg, "generate_graph_sqlite", unexpected)
    ic.cmd_incremental(quiet=True)


def test_a_change_survives_a_failed_reindex(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashes were written BEFORE the graph, so a file whose reindex failed
    looked up to date for ever after."""
    import indexer.graph_generator as gg

    f = project / "src" / "a.py"
    f.write_text("def one():\n    return 1\n")
    ic.cmd_incremental(quiet=True)
    f.write_text("def two():\n    return 2\n")

    real = gg.generate_graph_sqlite

    def locked(*a: Any, **k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gg, "generate_graph_sqlite", locked)
    with pytest.raises(sqlite3.OperationalError):
        ic.cmd_incremental(quiet=True)
    monkeypatch.setattr(gg, "generate_graph_sqlite", real)
    ic.cmd_incremental(quiet=True)

    db = sqlite3.connect(ic.get_data_dir() / "graph" / "graph.db")
    names = {r[0] for r in db.execute("SELECT name FROM symbols")}
    db.close()
    assert "two" in names


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")
def test_codevira_index_says_so_when_another_process_is_indexing(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_server.cli as cli

    monkeypatch.setattr(cli, "_INDEX_WAIT_SECONDS", 0.3)
    ran: list[int] = []
    monkeypatch.setattr(ic, "cmd_incremental", lambda **_: ran.append(1))
    p = _holder(project)
    try:
        cli.cmd_index(quiet=False)
    finally:
        p.communicate("")
    assert "another" in capsys.readouterr().err.lower()
    assert ran == [1], "it still runs: never worse than before the lock existed"
