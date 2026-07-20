"""
test_pin_generation.py — v3.7.1: an explicit rebind must bind EVERY later tool
call, not just the one that performed it.

The MCP SDK starts a fresh task per incoming message::

    tg.start_soon(self._handle_message, message, session, ...)

and `main()` calls `get_project_root()` BEFORE `asyncio.run`, which pins the
inherited-cwd root in the ROOT context. asyncio copies the context into each
task, so every request inherits that pin — and `_pinned_root.set(None)` can
only clear it for the CURRENT task; it cannot unset an ancestor's value for
siblings.

Net effect before the fix: `set_project_dir()` (called by the roots-binding
handshake) took effect for exactly ONE tool call, and every subsequent call
silently reverted to the stale inherited pin. That is the user-visible
"my LH session returns UDAP's decisions" bug.

These tests exercise the real shape: pin in a parent context, then resolve from
sibling tasks.
"""

from __future__ import annotations

import asyncio

import pytest

import mcp_server.paths as paths


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    right = tmp_path / "right"
    wrong = tmp_path / "wrong"
    for p in (right, wrong):
        (p / ".codevira").mkdir(parents=True)
        (p / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    monkeypatch.delenv("CODEVIRA_PROJECT_DIR", raising=False)
    paths.reset_pinned_root()
    yield right, wrong
    paths.reset_pinned_root()
    paths._project_dir_override = None


def test_rebind_applies_to_every_subsequent_task(two_projects):
    """THE regression: call 1 correct, call 2 reverted to the stale pin."""
    right, wrong = two_projects

    async def scenario():
        # Ancestor context pins `wrong` — exactly what main() does before
        # asyncio.run by calling get_project_root() at import/startup time.
        paths.set_project_dir(wrong)
        paths.get_project_root()

        results: list[str] = []

        async def tool_call(rebind_to=None):
            # Each SDK message handler runs in its own task, inheriting the
            # ancestor context (and therefore the ancestor's pin).
            if rebind_to is not None:
                paths.set_project_dir(rebind_to)
            results.append(str(paths.get_project_root()))

        # Call 1 performs the rebind (the roots-binding handshake).
        await asyncio.create_task(tool_call(rebind_to=right))
        # Calls 2 and 3 are fresh sibling tasks — they must NOT revert.
        await asyncio.create_task(tool_call())
        await asyncio.create_task(tool_call())
        return results

    results = asyncio.run(scenario())

    assert results[0] == str(right)
    assert results[1] == str(right), (
        "second tool call reverted to the stale inherited pin — "
        "the wrong project's memory would be read/written"
    )
    assert results[2] == str(right)


def test_rebind_visible_in_sibling_task_that_never_rebound(two_projects):
    """A sibling task that only READS must still see the rebind."""
    right, wrong = two_projects

    async def scenario():
        paths.set_project_dir(wrong)
        paths.get_project_root()

        async def rebinder():
            paths.set_project_dir(right)
            paths.get_project_root()

        async def reader():
            return str(paths.get_project_root())

        await asyncio.create_task(rebinder())
        return await asyncio.create_task(reader())

    assert asyncio.run(scenario()) == str(right)


def test_pin_still_prevents_drift_within_one_request(two_projects, monkeypatch):
    """D000118 must survive: inside ONE request the first resolution wins even
    if cwd moves underneath us.

    NOTE: this exercises the cwd-drift branch with NO intervening rebind, which
    is the easy half. The concurrent case below is the one that actually
    discriminates — an earlier revision of this fix passed THIS test while
    silently breaking cross-task isolation.
    """
    right, wrong = two_projects
    paths._project_dir_override = None

    monkeypatch.chdir(right)
    paths.reset_pinned_root()
    first = paths.get_project_root()

    monkeypatch.chdir(wrong)  # cwd drifts mid-request
    second = paths.get_project_root()

    assert second == first, "pin no longer protects against intra-request drift"


def test_concurrent_request_rebind_does_not_drag_a_sibling(two_projects):
    """A rebind in ONE request must not move a CONCURRENT request.

    This is the Phase 31 multiplex guarantee: a single server process serves
    several projects at once. A module-global invalidation scheme breaks it —
    task B's rebind invalidated task A's pin, so A's next call re-resolved
    against B's `_project_dir_override` (also a module global) and silently
    landed in the wrong project's store. That is cross-project bleed, and it is
    worse than the bug it replaced because the drift warning never fires.
    """
    right, wrong = two_projects

    async def scenario():
        a_seen: list[str] = []
        # Explicit ordering — a bare `sleep(0)` let B rebind AFTER A's second
        # read, so the race never happened and the test passed against the
        # broken implementation too.
        a_pinned = asyncio.Event()
        b_rebound = asyncio.Event()

        async def request_a():
            paths.set_project_dir(right)
            a_seen.append(str(paths.get_project_root()))  # pins `right` for A
            a_pinned.set()
            await b_rebound.wait()  # B has now rebound to `wrong`
            a_seen.append(str(paths.get_project_root()))  # must STILL be `right`

        async def request_b():
            await a_pinned.wait()
            paths.set_project_dir(wrong)
            paths.get_project_root()
            b_rebound.set()

        await asyncio.gather(request_a(), request_b())
        return a_seen

    seen = asyncio.run(scenario())

    assert seen[0] == str(right)
    assert seen[1] == str(right), (
        "a concurrent request's rebind dragged this request into another "
        "project — cross-project bleed"
    )


def test_reset_clears_pin_for_sibling_tasks(two_projects):
    right, wrong = two_projects

    async def scenario():
        paths.set_project_dir(wrong)
        paths.get_project_root()
        paths._project_dir_override = None
        paths.reset_pinned_root()

        async def reader():
            # No override and no valid pin -> resolves fresh from cwd.
            return paths._pinned_root.get()

        return await asyncio.create_task(reader())

    assert asyncio.run(scenario()) is None
