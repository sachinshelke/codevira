"""No background entry point may crash on an unresolvable project root.

WHY THIS FILE EXISTS
--------------------
Three bugs shipped in 4.1.0 with 3,400+ tests green and every release gate
passing. They were not logic errors — each was a REACHABILITY error, a correct
guard that simply was not on the path that runs:

    register_all.discover_projects   -> swallowed 11 real projects
    log_retention.enforce_retention  -> 39 false crash entries over 3 weeks
    index_codebase.cmd_incremental   -> raised on a timer, into a thread

``paths.is_invalid_project_root()`` returned the right answer in all three
cases. The callers did not handle it. G1-G4 stayed green throughout, because
the gauntlet verifies that things RUN, not that a guard is WIRED IN.

I found all three by hand — running each entry point under ``cwd=/``. A manual
sweep does not survive a release cycle, which is why the same class recurred
across three releases. This file is that sweep, mechanised.

THE CONDITION BEING REPRODUCED
------------------------------
Claude Desktop registers ONE dynamic entry (``args: []``, no ``cwd``), so the
server process runs at ``/`` and binds per tool call from ``file_path``.
Background work runs on timers and at startup, independent of any tool call —
so it executes while the root is still unresolvable and ``get_data_dir()``
refuses ``/``.

THE LINE THIS FILE DEFENDS
--------------------------
Background work DEGRADES. A tool answering a question REFUSES LOUDLY.

Both halves are asserted here. Silently returning empty from a tool is what
produced a wrong-project session brief on 2026-09-09 and cost half a session:
the call succeeded, returned a plausible empty result, and nothing indicated
it had read the wrong project's store.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


_REFUSAL = (
    "get_data_dir() refuses invalid project root: / is a system "
    "directory, not a project. (root resolved to /). Set "
    "CODEVIRA_PROJECT_DIR or cd into a real project subdirectory."
)


class _Stub:
    """A get_data_dir replacement that RECORDS whether it was reached.

    Counting matters more than raising. `get_data_dir` is imported at module
    scope in most of these modules, so each holds its own reference and
    patching `mcp_server.paths.get_data_dir` does nothing to them. A patch
    that misses makes the call SUCCEED — and a test asserting "does not
    raise" then passes while exercising nothing.

    That is not hypothetical: the first cut of this file patched the wrong
    target for both tool entry points and went green, because the modules
    happened not to be imported yet when it ran alone. Under the full suite
    they were already imported and the same tests failed. Asserting `calls`
    turns a missed patch into a failure instead of a false green.
    """

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise self.exc


def _assert_patch_was_live(stub: _Stub, patch_target: str) -> None:
    assert stub.calls > 0, (
        f"{patch_target} was never called — the patch missed its target, so "
        "this test proved nothing. `get_data_dir` is bound at module scope "
        "in most of these modules; patch it in the CONSUMING module's "
        "namespace (e.g. 'indexer.index_codebase.get_data_dir'), not in "
        "mcp_server.paths, unless the import is function-local."
    )


# ── the registry ────────────────────────────────────────────────────────────
# (label, module path, attribute to patch, callable)
#
# Every entry runs WITHOUT a preceding tool call — at startup or on a timer —
# so each can execute while the project root is unresolvable. Adding a new
# startup task means adding a row here; the coverage test below fails if a new
# crash-logged startup task appears in server.py and is not represented.
BACKGROUND_ENTRY_POINTS = [
    (
        "log retention cleanup",
        "mcp_server.paths.get_data_dir",
        lambda: __import__(
            "mcp_server.log_retention", fromlist=["x"]
        ).enforce_retention(),
    ),
    (
        "watcher incremental reindex",
        "indexer.index_codebase.get_data_dir",
        lambda: __import__("indexer.index_codebase", fromlist=["x"]).cmd_incremental(
            quiet=True
        ),
    ),
]


@pytest.mark.parametrize(
    "label,patch_target,call",
    BACKGROUND_ENTRY_POINTS,
    ids=[e[0] for e in BACKGROUND_ENTRY_POINTS],
)
def test_background_work_degrades_on_unresolvable_root(
    label, patch_target, call, monkeypatch
):
    """Must return, not raise. Raising here reaches a crash log nobody reads."""
    stub = _Stub(ValueError(_REFUSAL))
    monkeypatch.setattr(patch_target, stub)
    try:
        call()
    except ValueError as exc:  # pragma: no cover - this IS the failure
        pytest.fail(
            f"{label!r} raises on an unresolvable project root.\n"
            f"  {exc}\n"
            "Background work runs without a tool call, so the root can be "
            "unresolvable. Guard it the way cmd_status and enforce_retention "
            "do: catch the ValueError, return a no-op that SAYS why."
        )
    _assert_patch_was_live(stub, patch_target)


@pytest.mark.parametrize(
    "label,patch_target,call",
    BACKGROUND_ENTRY_POINTS,
    ids=[e[0] for e in BACKGROUND_ENTRY_POINTS],
)
def test_a_real_error_still_propagates(label, patch_target, call, monkeypatch):
    """The counter-guard. Swallowing everything would trade a noisy crash log
    for a silent one — a broken indexer that reports success is worse than one
    that shouts."""
    stub = _Stub(OSError("disk is on fire"))
    monkeypatch.setattr(patch_target, stub)
    with pytest.raises(OSError):
        call()
    _assert_patch_was_live(stub, patch_target)


# ── the half that must NOT degrade ──────────────────────────────────────────
TOOL_ENTRY_POINTS = [
    (
        "tools.roadmap._roadmap_file",
        "mcp_server.tools.roadmap.get_data_dir",
        lambda: __import__("mcp_server.tools.roadmap", fromlist=["x"])._roadmap_file(),
    ),
    (
        "tools.learning._get_db",
        "mcp_server.tools.learning.get_data_dir",
        lambda: __import__("mcp_server.tools.learning", fromlist=["x"])._get_db(),
    ),
]


@pytest.mark.parametrize(
    "label,patch_target,call",
    TOOL_ENTRY_POINTS,
    ids=[e[0] for e in TOOL_ENTRY_POINTS],
)
def test_tools_refuse_loudly_instead_of_returning_empty(
    label, patch_target, call, monkeypatch
):
    """A tool answers a caller's question. With no resolvable project the
    honest answer is an error naming the reason — NOT an empty result.

    This is asserted, not merely left alone, because the tempting "fix" next
    time will be to guard these too. That would recreate the exact failure of
    2026-09-09: get_session_context returned a plausible empty brief bound to
    the wrong project, and nothing said so.
    """
    stub = _Stub(ValueError(_REFUSAL))
    monkeypatch.setattr(patch_target, stub)
    with pytest.raises(ValueError, match="invalid project root"):
        call()
    _assert_patch_was_live(stub, patch_target)


# ── the meta-test: keep the registry from rotting ───────────────────────────
def test_every_crash_logged_startup_task_is_represented():
    """A hand-written list decays. This fails when a NEW background task
    appears in server.py's startup path without a row above.

    `safe_log_crash(context=...)` in main() is the machine-readable marker for
    "background work that runs before any tool call" — the exact population
    this file must cover.
    """
    src = (REPO / "mcp_server" / "server.py").read_text()
    contexts = set(re.findall(r'safe_log_crash\([^)]*context="([^"]+)"', src))

    # Startup tasks that provably cannot hit an unresolvable root, with the
    # reason each is exempt. Verified 2026-09-10 by running every one under
    # `cd / ; no --project-dir ; no env pin` — see the sweep in the session log.
    EXEMPT = {
        # start_background_watcher() only spawns the thread; the work it
        # schedules is cmd_incremental, which IS covered above.
        "background watcher startup",
        # scan_git_log(project_root) takes the root as an ARGUMENT — it never
        # calls get_data_dir(), so it cannot hit this failure.
        "startup git fix-history scan",
        # register_current_project() resolves through global.db, not the
        # per-project data dir.
        "cross-project registration",
    }
    covered = {label for label, _, _ in BACKGROUND_ENTRY_POINTS}
    # map the one context string that names a covered entry point
    covered_contexts = {"log retention cleanup"}

    unaccounted = contexts - EXEMPT - covered_contexts
    assert not unaccounted, (
        f"new crash-logged startup task(s) in server.py: {sorted(unaccounted)}.\n"
        "Background work runs before any tool call, so it can execute with an "
        "unresolvable project root. Either add it to BACKGROUND_ENTRY_POINTS "
        "(if it touches get_data_dir) or to EXEMPT with the reason it cannot."
    )
    assert covered, "registry is empty — the sweep would assert nothing"
