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


# ── the opt-in gate: the FIFTH not-found path ────────────────────────────
#
# The tests above parametrise the progress states, which was the right
# generalisation for the bug that prompted them — but it stops at the graph
# lookup. `_opt_in_gate` returns `found=False` BEFORE any of that, on every
# graph read of a project that never ran `codevira init`, and it carried
# neither `not_indexed` nor the null counts. That is the same defect one
# frame further up, and no state-parametrised test could have reached it.


@pytest.fixture
def _not_opted_in(monkeypatch: pytest.MonkeyPatch):
    """Force the opt-in gate closed."""
    import mcp_server.opt_in as opt_in

    monkeypatch.setattr(opt_in, "activation_allowed", lambda *a, **k: False)


@pytest.mark.parametrize(
    ("tool", "counts"),
    [
        (graph.get_node, COUNT_FIELDS),
        (graph.get_impact, ("blast_radius", "protected_count", "high_stability_count")),
    ],
)
def test_the_opt_in_gate_answers_with_the_same_shape(
    _not_opted_in, tool, counts: tuple[str, ...]
) -> None:
    """Fails with KeyError on the pre-fix code."""
    result = tool("does/not/exist.py")

    assert result["found"] is False
    assert result["not_opted_in"] is True
    assert result["not_indexed"] is True, (
        f"{tool.__name__} omitted not_indexed on the opt-in path — a client "
        f"keying on it gets a KeyError instead of an answer, on the single "
        f"most common not-found case there is."
    )
    for field in counts:
        assert result[field] is None, (
            f"{tool.__name__} returned {field}={result[field]!r} rather than "
            f"None — a zero here reads as an authoritative measurement."
        )


def test_query_graph_gate_carries_the_flag(_not_opted_in) -> None:
    """query_graph has no counts, but the flag is the contract."""
    result = graph.query_graph("does/not/exist.py")
    assert result["not_indexed"] is True
