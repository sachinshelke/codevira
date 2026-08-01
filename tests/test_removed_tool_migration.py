"""A removed tool is not an unknown tool.

The 4.0 plan made this a release guardrail, with its reason stated:

    removed tools return "removed in 4.0, see MIGRATING" — never an
    unknown-tool error (agents cache tool lists for weeks)

Step 5's done-when repeats it: "every removed tool returns the migration
message." It shipped unmet — all 15 returned `Unknown tool: <name>`, and
the only trace of the intent was a comment in server.py's module
docstring.

The impact is not hypothetical. An agent session opened before the
upgrade keeps its cached tool list for as long as it lives, so it goes on
calling `consensus_check` and `spatial_nearby` and gets back a message
that names no successor and no migration guide.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_server.server import _REMOVED_IN_4_0, call_tool


def _text(result) -> str:
    if isinstance(result, list):
        return "".join(getattr(c, "text", "") for c in result)
    return str(result)


class TestEveryRemovedToolExplainsItself:
    @pytest.mark.parametrize("tool", sorted(_REMOVED_IN_4_0))
    def test_says_it_was_removed_not_unknown(self, tool: str) -> None:
        body = _text(asyncio.run(call_tool(tool, {})))
        assert "Unknown tool" not in body, (
            f"{tool} still returns the unknown-tool error, which names "
            f"neither a successor nor the migration guide"
        )
        assert "removed in codevira 4.0" in body

    @pytest.mark.parametrize("tool", sorted(_REMOVED_IN_4_0))
    def test_names_a_successor(self, tool: str) -> None:
        """A pointer to MIGRATING.md alone still costs a round trip."""
        body = _text(asyncio.run(call_tool(tool, {})))
        assert (
            _REMOVED_IN_4_0[tool] in body
        ), f"{tool} does not name what to use instead"

    @pytest.mark.parametrize("tool", sorted(_REMOVED_IN_4_0))
    def test_points_at_the_migration_guide(self, tool: str) -> None:
        assert "MIGRATING" in _text(asyncio.run(call_tool(tool, {})))

    @pytest.mark.parametrize("tool", sorted(_REMOVED_IN_4_0))
    def test_message_survives_json_transport(self, tool: str) -> None:
        """The payload is JSON-serialised before an agent reads it, and a
        non-ASCII dash arrives as a literal \\u2014 in the middle of the
        sentence. Caught when three successors written with em-dashes
        failed the successor assertion above for that reason alone."""
        body = _text(asyncio.run(call_tool(tool, {})))
        assert (
            "\\u" not in body
        ), f"escaped non-ASCII in the message an agent reads: {body}"


class TestTheTableMatchesTheRelease:
    def test_covers_the_documented_count(self) -> None:
        """The docs say 15 tools were removed. If that number and this
        table disagree, one of them is lying to a user."""
        assert len(_REMOVED_IN_4_0) == 15, (
            f"table has {len(_REMOVED_IN_4_0)} entries; README, MIGRATING "
            f"and CHANGELOG all say 15 tools were removed"
        )

    def test_no_removed_tool_is_still_advertised(self) -> None:
        """Belt and braces: a name cannot be both live and retired."""
        import mcp_server.server as server

        advertised = {t.name for t in asyncio.run(server.list_tools())}
        overlap = advertised & set(_REMOVED_IN_4_0)
        assert not overlap, f"advertised but marked removed: {sorted(overlap)}"

    def test_a_genuinely_unknown_tool_still_says_unknown(self) -> None:
        """The migration path must not swallow real typos."""
        body = _text(asyncio.run(call_tool("no_such_tool_xyz", {})))
        assert "Unknown tool" in body
