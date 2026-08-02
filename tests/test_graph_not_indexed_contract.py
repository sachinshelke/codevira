"""`get_node` must answer with the same shape on every not-found path.

v2.1.2 Item 2 established the contract: an unindexed path returns
``not_indexed: True`` and ``null`` for every numeric count, so an agent can
tell "unindexed" from "indexed, genuinely zero dependencies". Without it a
zero blast-radius reads as authoritative when it is merely unknown.

That fix covered three of the four ``found=False`` returns. The fourth --
``mcp_server/tools/graph.py``'s "a background index is running" branch --
returned ``{"found": False, "status": "initializing", ...}`` with no
``not_indexed`` key at all, so ``result["not_indexed"]`` raised KeyError.

It fires exactly when a background index is mid-flight, which is first
contact with a new project: the moment a client is most likely to be
polling ``get_node``. In the suite it presented as an unexplained flake in
``test_get_node_not_indexed_returns_null_counts`` across three sessions,
because the trigger was a background thread leaving ``_progress`` at
"indexing" -- not test ordering, which is what everyone chased. Full
diagnosis in D000138.

These tests parametrise over the progress states rather than testing the
one that happened to fail, because the defect was a MISSING case, and a
test that only covers the case you already know about cannot catch the
next one.
"""

from __future__ import annotations

import pytest

from mcp_server import auto_init
from mcp_server.tools import graph

#: Every state get_init_progress() can report. The two middle values take
#: the early-return branch; the rest fall through to the graph lookup.
ALL_PROGRESS_STATES = ("not_started", "initializing", "indexing", "ready", "error")

COUNT_FIELDS = ("rules_count", "dependencies_count", "key_functions_count")


@pytest.fixture(autouse=True)
def _restore_progress():
    """Leave the global exactly as found — this module mutates the very
    state whose leakage caused the original flake."""
    before = dict(auto_init._progress)
    yield
    auto_init._progress.clear()
    auto_init._progress.update(before)


@pytest.mark.parametrize("status", ALL_PROGRESS_STATES)
def test_not_indexed_is_present_whatever_the_index_is_doing(status: str) -> None:
    """The load-bearing test. Fails with KeyError on the pre-fix code for
    status in ('initializing', 'indexing')."""
    auto_init._progress.update(status=status)
    result = graph.get_node("does/not/exist.py")

    assert result["found"] is False
    assert "not_indexed" in result, (
        f"get_node omitted not_indexed while the index reported {status!r}. "
        f"A client keying on this field gets a KeyError instead of an answer."
    )
    assert result["not_indexed"] is True


@pytest.mark.parametrize("status", ALL_PROGRESS_STATES)
def test_counts_are_null_not_zero_whatever_the_index_is_doing(status: str) -> None:
    """`0` would be a lie: it reads as "indexed, no dependencies"."""
    auto_init._progress.update(status=status)
    result = graph.get_node("does/not/exist.py")

    for field in COUNT_FIELDS:
        assert field in result, f"{field} missing while index reported {status!r}"
        assert result[field] is None, (
            f"{field} is {result[field]!r} rather than None while the file is "
            f"unindexed — a zero here is indistinguishable from a real zero."
        )


def test_every_not_found_path_agrees_on_its_shape() -> None:
    """Compare the branches against each other rather than against a
    hardcoded list, so a fifth branch added later cannot quietly differ."""
    shapes = {}
    for status in ALL_PROGRESS_STATES:
        auto_init._progress.update(status=status)
        result = graph.get_node("does/not/exist.py")
        shapes[status] = {
            k for k in ("found", "not_indexed", *COUNT_FIELDS) if k in result
        }

    reference = shapes["not_started"]
    for status, keys in shapes.items():
        assert keys == reference, (
            f"the {status!r} path returns {sorted(keys)} but the not_started "
            f"path returns {sorted(reference)} — not-found answers must not "
            f"depend on what the indexer happens to be doing"
        )


def test_the_initializing_branch_still_says_so() -> None:
    """Adding not_indexed must not cost the caller the retry hint."""
    auto_init._progress.update(status="indexing")
    result = graph.get_node("does/not/exist.py")

    assert result.get("status") == "initializing"
    assert "hint" in result and "again" in result["hint"].lower()
