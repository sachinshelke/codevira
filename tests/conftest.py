"""
Shared pytest fixtures for the Codevira MCP test suite.
"""

import os
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Disable the v3.3.0 update-available check suite-wide. Any test that drives
# cli.main() (e2e/CLI tests) would otherwise spawn a real detached refresh
# subprocess hitting PyPI and write to the developer's ~/.codevira/.
# tests/test_update_check.py re-enables it per-test via monkeypatch.delenv.
# ---------------------------------------------------------------------------
os.environ.setdefault("CODEVIRA_NO_UPDATE_CHECK", "1")

# ---------------------------------------------------------------------------
# Isolate the global home for SUBPROCESSES too.
#
# `_isolate_global_home` below patches `paths.get_global_home` with
# monkeypatch — which does not cross a process boundary. 18 test files spawn
# a `codevira` subprocess, and every one of them was writing into the
# developer's real ~/.codevira/global.db: 371 of 387 rows in the reference
# machine's registry were pytest temp dirs, and the count grew during a
# single suite run.
#
# An env var is the only isolation a child inherits, so it is set here at
# import time — before any test, fixture or collection-time code can spawn
# anything. Deliberately NOT tmp_path_factory: this must exist before pytest
# builds its fixtures.
# ---------------------------------------------------------------------------
import tempfile as _tempfile  # noqa: E402

os.environ.setdefault("CODEVIRA_HOME", _tempfile.mkdtemp(prefix="codevira-test-home-"))

# ---------------------------------------------------------------------------
# Make spawned subprocesses import THIS working tree, not whatever is
# installed.
#
# A subprocess started with `cwd=tmp_path` (which most CLI tests do) has no
# repo on sys.path, so `import mcp_server` falls through to site-packages —
# a copy from an earlier `pip install`. Those tests were therefore
# exercising an OLD build rather than the code under test, and passing.
#
# That is how the global.db registry leak survived four rounds of tracing:
# every tracer patched the repo's classes inside the pytest process, while
# the writes happened in a subprocess importing a DIFFERENT copy of the
# module — one predating $CODEVIRA_HOME, so it wrote to the real
# ~/.codevira. Measured: +4 rows per run without this, +0 with it.
#
# Prepend rather than replace, so a caller's own PYTHONPATH still applies.
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PYTHONPATH"] = (
    _REPO_ROOT + os.pathsep + os.environ["PYTHONPATH"]
    if os.environ.get("PYTHONPATH")
    else _REPO_ROOT
)

# ---------------------------------------------------------------------------
# Pre-import numpy at conftest load time.
#
# Why: pytest.approx (and several pytest assertion helpers) lazy-import numpy
# at use time. If a test (e.g. tests/test_http_server.py) starts a background
# uvicorn / watcher thread that lazy-imports numpy concurrently with another
# test thread doing pytest.approx, Python returns a partially-initialised
# numpy module — `module 'numpy' has no attribute 'isscalar'` — to the latter.
# That manifests as flaky AttributeError in ``tests/test_rule_learner.py`` and
# ``tests/test_sqlite_util.py`` only when those files run after the http
# server tests have ever touched a real uvicorn / startup chain.
#
# Force-importing numpy here, before ANY test runs, guarantees the module is
# fully loaded once. Subsequent imports (in any thread) hit the cached, fully
# initialised module and `numpy.isscalar` is always present. Negligible cost
# (numpy is already a transitive dep of chromadb, sentence-transformers, etc).
# ---------------------------------------------------------------------------
try:
    import numpy as _numpy  # noqa: F401 — eager import to avoid concurrent partial-load

    # Sanity-check: if isscalar isn't present, our pre-import didn't actually
    # complete the load. Force attribute access to surface that immediately.
    _ = _numpy.isscalar
except (ImportError, AttributeError):
    # numpy isn't required by the suite as a whole — only by pytest.approx
    # paths. If numpy can't load, tests that don't use approx still run; the
    # ones that do will fail with a clear AttributeError downstream.
    pass

# ---------------------------------------------------------------------------
# Install a comprehensive mock of indexer.treesitter_parser BEFORE any test
# file imports modules that depend on it. This mock provides all attributes
# that code_reader.py, chunker.py, and graph_generator.py import.
#
# We only mock when the real packages are NOT installed. v2.2.0+ ships
# 4 individual grammar packages (tree-sitter-{typescript,javascript,go,rust}).
# The legacy tree-sitter-language-pack [all-languages] extra was removed
# along with the v2.1.x carryover user base; tests no longer need a
# fallback to it.
# ---------------------------------------------------------------------------
_ts_available = True
try:
    import tree_sitter  # noqa: F401

    # At least one of the v2.2.0 base grammar packages must be importable.
    _grammar_found = False
    for _pkg in (
        "tree_sitter_typescript",
        "tree_sitter_javascript",
        "tree_sitter_go",
        "tree_sitter_rust",
    ):
        try:
            __import__(_pkg)
            _grammar_found = True
            break
        except ImportError:
            continue
    if not _grammar_found:
        _ts_available = False
except ImportError:
    _ts_available = False

if not _ts_available:
    if "tree_sitter" not in sys.modules:
        _ts_mod = types.ModuleType("tree_sitter")
        _ts_mod.Node = MagicMock()
        sys.modules["tree_sitter"] = _ts_mod

    if "indexer.treesitter_parser" not in sys.modules:
        _fake_ts = types.ModuleType("indexer.treesitter_parser")
        _fake_ts.parse_file = MagicMock(return_value=None)
        _fake_ts.get_language = MagicMock(return_value=None)
        _fake_ts.get_symbol_source = MagicMock(return_value={"found": False})
        _fake_ts.EXTENSION_MAP = {}
        # ParsedSymbol dataclass stub (used by graph_generator)
        from dataclasses import dataclass, field as dc_field
        from typing import Optional, List

        @dataclass
        class _ParsedSymbol:
            name: str = ""
            kind: str = ""
            signature_line: str = ""
            start_line: int = 0
            end_line: int = 0
            docstring: Optional[str] = None
            is_public: bool = True
            methods: List[str] = dc_field(default_factory=list)

        @dataclass
        class _ParsedImport:
            module: str = ""
            raw_line: str = ""

        @dataclass
        class _ParsedFile:
            file_path: str = ""
            language: str = ""
            symbols: List[_ParsedSymbol] = dc_field(default_factory=list)
            imports: List[_ParsedImport] = dc_field(default_factory=list)
            module_docstring: Optional[str] = None

        _fake_ts.ParsedSymbol = _ParsedSymbol
        _fake_ts.ParsedImport = _ParsedImport
        _fake_ts.ParsedFile = _ParsedFile
        sys.modules["indexer.treesitter_parser"] = _fake_ts
        # Also set on parent package so `from indexer.treesitter_parser import X` works
        import indexer as _indexer_pkg

        _indexer_pkg.treesitter_parser = _fake_ts

import mcp_server.paths as paths  # noqa: E402 — must follow stub install

# Eagerly import mcp_server.storage.paths so its module-level
# ``from mcp_server.paths import get_project_root`` binding is established
# against the REAL function once, at collection time, before any test patches
# ``mcp_server.paths.get_project_root``. Otherwise a test that runs real code
# which first-imports storage.paths WHILE get_project_root is patched (e.g.
# test_server's main() test) would capture the mock into storage.paths and leak
# it to every later test — a MagicMock decisions_path() → "Bad file descriptor".
# Pre-loading here makes the leak impossible regardless of test order.
import mcp_server.storage.paths  # noqa: E402,F401 — eager-load, see above
from indexer.sqlite_graph import SQLiteGraph  # noqa: E402 — must follow stub install


def _reset_crash_logger() -> None:
    """Drop crash_logger's two process-global caches.

    1. ``_logger`` — a memoised Logger whose file handler is bound to
       ``<global_home>/logs/crashes.log`` at first use. See the call site in
       ``_isolate_global_home``.
    2. ``_recent_crashes`` — the 60-second duplicate-suppression window, keyed
       on ``type(exc).__name__ + str(exc)``. The whole unit suite runs inside
       one window, so two tests raising the same placeholder exception (say
       ``ValueError("boom")``) are ONE signature: after ``_RATE_LIMIT_MAX``
       hits ``log_crash()`` silently drops the write and the test that reads
       the log back sees nothing.

    Import lazily so conftest import order stays independent of crash_logger's
    own imports.
    """
    from mcp_server import crash_logger

    logger = crash_logger._logger
    if logger is not None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 — best-effort fd release
                pass
    crash_logger._logger = None
    with crash_logger._RATE_LIMIT_LOCK:
        crash_logger._recent_crashes.clear()


@pytest.fixture(autouse=True)
def _reset_server_transport_globals():
    """Reset ``mcp_server.server``'s process-global transport / binding latches.

    ``run_http_server()`` sets ``_is_http_transport = True`` and never unsets
    it — correct in production (one process serves exactly one transport) but
    permanent for the rest of a pytest session. Once ``tests/test_http_server``
    had run, ``_bind_project_from_client_roots`` short-circuited for EVERY
    later test: ``tests/test_binding_e2e.py``'s positive cases failed, and —
    worse — its negative cases ("must NOT bind") passed vacuously.
    ``_roots_bind_attempted`` is a once-per-process latch with the same shape.

    Read from ``sys.modules`` rather than importing, so tests that never touch
    the server don't pay for importing the whole tool surface — if the module
    isn't loaded there is no global to leak.
    """
    srv = sys.modules.get("mcp_server.server")
    if srv is not None:
        srv._is_http_transport = False
        srv._roots_bind_attempted = False


@pytest.fixture(autouse=True)
def _isolate_global_home(tmp_path_factory, monkeypatch):
    """Prevent ALL tests from writing to real codevira storage — BOTH the
    global ``~/.codevira/`` AND the per-project ``./.codevira/``.

    2026-06-16 fix: pre-fix this only redirected ``get_global_home``, leaving
    the PER-PROJECT data dir resolving from the cwd (= the repo root under
    pytest). Any test that called ``decisions_store.record()`` without its own
    project fixture therefore wrote to the REAL repo's
    ``.codevira/decisions.jsonl`` — 1240 ``"Use bcrypt for password hashing"``
    fixtures leaked into real memory over three weeks, found by the E3
    relevance eval.

    We now also chdir off the repo root into a throwaway project so cwd-based
    resolution lands in disposable storage. Two deliberate choices:

    * **chdir, not _project_dir_override** — composes with test-local fixtures
      that chdir into their own project, and lets resolution logic still run
      for the tests that exercise it (setup wizard, project binding).
    * **isolated dirs live OUTSIDE the per-test ``tmp_path``** (via
      ``tmp_path_factory``) — otherwise a test doing ``tmp_path.rglob('*')``
      would pick up our injected ``.codevira/config.yaml`` (caught by
      test_gitignore's language inference).
    """
    base = tmp_path_factory.mktemp("cv-isolated")
    fake_home = base / "global-home"
    fake_home.mkdir()
    monkeypatch.setattr(paths, "get_global_home", lambda: fake_home)
    # Clear module-level caches that carry state between tests:
    paths._data_dir_cache.clear()
    paths.reset_pinned_root()  # D000118 pin (ContextVar) must not leak across tests
    # v3.7.0: the opt-in cache is another per-process cache — clear it too so a
    # test that resolves opt-in against a mocked get_project_root (e.g.
    # test_server's main() test) can't leave a stray MagicMock-keyed entry.
    from mcp_server import opt_in as _opt_in

    _opt_in.invalidate_opt_in_cache()
    # crash_logger memoises a Logger whose RotatingFileHandler is bound to
    # <global_home>/logs/crashes.log AT FIRST USE. Every test gets a DIFFERENT
    # fake global home above, so that singleton must not survive: the first test
    # to trigger a crash write (tests/engine/test_runner.py, in collection
    # order) otherwise kept every later log_crash() writing into ITS tmp dir,
    # and a test that recorded a crash then read the log back saw nothing.
    # Invisible in collection order; broke test_doctor.py's
    # TestCrashLogSize::test_surfaces_recorded_crashes under `-p randomly`.
    # Closing the handler also releases the fd on the (by then deleted) tmp
    # file. Also clears the duplicate-suppression window — see the helper.
    _reset_crash_logger()
    monkeypatch.setattr(paths, "_project_dir_override", None)
    iso_project = base / "project"
    (iso_project / ".codevira").mkdir(parents=True)
    (iso_project / ".codevira" / "config.yaml").write_text(
        "project:\n  name: isolated-test\n"
    )
    monkeypatch.chdir(iso_project)


# ---------------------------------------------------------------------------
# Background-thread leak detector.
#
# Every thread codevira starts is named ``codevira-*`` (auto-init, bg-index,
# startup-outcome-analysis, post-edit-refresh). A test that starts one and
# returns without joining it leaves that thread running INSIDE the next,
# unrelated test — where it mutates process-global module state and writes to
# the filesystem after this test's monkeypatches (fake $HOME, patched
# get_data_dir) have been torn down.
#
# That is a RACE, not an ordering problem: a setup-time reset in the next test
# cannot prevent a concurrent write from a thread that is already running.
# It is why tests/test_auto_init.py::TestGetInitProgress::test_default_state
# saw ``status == "indexing"`` on a fresh record in CI run 30764652633
# (Python 3.10 only — slower scheduling widened the window).
#
# The rule this enforces: a test owns the threads it starts. Join them, or
# don't start real ones.
# ---------------------------------------------------------------------------

_CODEVIRA_THREAD_PREFIX = "codevira-"

# How long teardown waits before calling a thread leaked.
#
# Correctness does not depend on this number: the join happens at teardown, so
# no thread crosses a test boundary regardless of how long the wait is. The
# timeout only controls how loudly we REPORT a leak — generous enough that a
# slow-but-terminating thread on a loaded CI runner doesn't cause a spurious
# failure, short enough that a genuinely stuck thread fails fast.
#
# To audit instead of tolerate, drop the grace to zero — then ANY codevira
# thread still alive at teardown is reported, however briefly it would have
# lived:
#
#     CODEVIRA_TEST_THREAD_JOIN_TIMEOUT=0 python -m pytest tests/ -q
#
# That sweep is expected to come back clean. If it doesn't, a test started a
# real background thread it doesn't own.
_THREAD_JOIN_TIMEOUT = float(
    os.environ.get("CODEVIRA_TEST_THREAD_JOIN_TIMEOUT", "10.0")
)


def _live_codevira_threads():
    """Threads codevira started that are still running."""
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and t.name.startswith(_CODEVIRA_THREAD_PREFIX)
    ]


@pytest.fixture(autouse=True)
def _no_leaked_background_threads(_isolate_global_home):
    """Fail any test that leaves a codevira background thread running.

    Depends on ``_isolate_global_home`` purely for ordering: pytest tears
    fixtures down in reverse setup order, so taking it as an argument
    guarantees this join happens BEFORE the fake-$HOME monkeypatches are
    undone. Otherwise the very thread we're waiting on could spend the join
    window writing to the developer's real ``~/.codevira/``.
    """
    yield

    deadline = time.monotonic() + _THREAD_JOIN_TIMEOUT
    for t in _live_codevira_threads():
        t.join(timeout=max(0.0, deadline - time.monotonic()))

    leaked = _live_codevira_threads()
    if not leaked:
        return

    # Containment. The thread is still running and we cannot kill it, so at
    # minimum sever its handle on the progress record — otherwise this one
    # leak cascades into a string of unrelated failures and buries the
    # culprit. Threads launched by ensure_project_initialized write to the
    # record they were handed (see auto_init._update_progress), so rebinding
    # the global makes their remaining writes inert.
    import mcp_server.auto_init as _auto_init

    _auto_init._progress = {
        "status": "not_started",
        "files_indexed": 0,
        "total_files": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    names = ", ".join(sorted(t.name for t in leaked))
    pytest.fail(
        f"Test leaked {len(leaked)} live codevira background thread(s) after "
        f"{_THREAD_JOIN_TIMEOUT}s: {names}.\n"
        "A leaked thread keeps running inside later tests and mutates "
        "process-global state there — the resulting failure looks like it "
        "belongs to whichever test happened to be running.\n"
        "Fix the test that started it: join the thread before the test ends "
        "(inside the `with patch(...)` block, so the patches still cover the "
        "thread's whole life), or stub the call so no real thread starts.",
        pytrace=False,
    )


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Isolated project with .codevira dir, config.yaml, and SQLiteGraph."""
    project = tmp_path / "test-project"
    data_dir = project / ".codevira"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text(
        "project:\n  name: test\n  language: python\n  watched_dirs:\n    - src\n  file_extensions:\n    - .py\n"
    )
    (data_dir / "graph").mkdir(parents=True)

    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(project.resolve())
    # get_global_home is already patched by the autouse _isolate_global_home fixture

    db = SQLiteGraph(data_dir / "graph" / "graph.db")
    yield project, data_dir, db
    db.close()


@pytest.fixture
def populated_db(project_env):
    """project_env with pre-loaded graph data."""
    project, data_dir, db = project_env
    # Nodes
    db.add_node("file:src/api.py", "file", "api.py", "src/api.py", layer="api")
    db.add_node(
        "file:src/service.py", "file", "service.py", "src/service.py", layer="service"
    )
    db.add_node("file:src/db.py", "file", "db.py", "src/db.py", layer="data")
    db.add_node(
        "file:tests/test_api.py",
        "file",
        "test_api.py",
        "tests/test_api.py",
        layer="test",
    )
    # Edges
    db.add_edge("file:src/api.py", "file:src/service.py", kind="imports")
    db.add_edge("file:src/service.py", "file:src/db.py", kind="imports")
    db.add_edge("file:tests/test_api.py", "file:src/api.py", kind="tests")
    # Sessions + decisions
    db.log_session(
        "s1",
        "Initial API setup",
        "1",
        [
            {
                "file_path": "src/api.py",
                "decision": "Use REST endpoints",
                "context": "API design",
            },
            {
                "file_path": "src/service.py",
                "decision": "Use repository pattern",
                "context": "Architecture",
            },
        ],
    )
    db.log_session(
        "s2",
        "Add database layer",
        "2",
        [
            {
                "file_path": "src/db.py",
                "decision": "Use SQLite for local storage",
                "context": "Data layer",
            },
        ],
    )
    # Outcomes
    db.record_outcome("s1", "src/api.py", "kept")
    db.record_outcome(
        "s1", "src/service.py", "modified", delta_summary="Changed naming"
    )
    # v3.0.0 audit cleanup: the preference + learned-rule seed calls
    # were removed. The SQLiteGraph methods that backed them
    # (record_preference / add_learned_rule) were deleted in the
    # 2026-05-22 surface-cut audit because the MCP tools that consumed
    # those tables (get_preferences / get_learned_rules / retire_rule)
    # were also deleted. Outcomes are still seeded — they feed
    # AntiRegression policy + decision-confidence scoring.
    return project, data_dir, db


@pytest.fixture
def sample_source_files(tmp_path):
    """Create realistic Python source files for testing."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        '"""Main application module."""\n'
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "DEFAULT_PORT = 8080\n"
        "\n"
        "class Application:\n"
        '    """The main application class."""\n'
        "    \n"
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "        self._running = False\n"
        "    \n"
        "    def start(self) -> None:\n"
        '        """Start the application."""\n'
        "        self._running = True\n"
        "    \n"
        "    def stop(self) -> None:\n"
        '        """Stop the application."""\n'
        "        self._running = False\n"
        "    \n"
        "    def _internal_method(self):\n"
        "        pass\n"
        "\n"
        "async def fetch_data(url: str, timeout: int = 30) -> dict:\n"
        '    """Fetch data from a URL."""\n'
        '    return {"url": url}\n'
        "\n"
        "def _private_helper():\n"
        "    pass\n"
    )
    (src / "util.py").write_text(
        '"""Utility functions."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text(
        '"""Tests for main module."""\n'
        "from src.main import Application\n"
        "\n"
        "def test_start():\n"
        '    app = Application("test")\n'
        "    app.start()\n"
        "    assert app._running\n"
    )
    return tmp_path


@pytest.fixture
def corrupt_yaml(tmp_path):
    """Factory for creating corrupt YAML files."""

    def _make(name="corrupt.yaml", content="{{invalid yaml: ["):
        p = tmp_path / name
        p.write_text(content)
        return p

    return _make


@pytest.fixture
def corrupt_sqlite(tmp_path):
    """Factory for creating corrupt SQLite database files."""

    def _make(name="corrupt.db"):
        p = tmp_path / name
        p.write_bytes(b"NOT A SQLITE DB" + b"\x00" * 100)
        return p

    return _make
