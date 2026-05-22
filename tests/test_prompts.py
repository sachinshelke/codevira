"""
Tests for mcp_server/prompts.py — v3.0.0 MCP workflow prompt templates.

v3.0.0 (2026-05-22 surface-cut audit): the prompt library was pruned
from 5 templates → 1. The deleted templates (``review_changes``,
``debug_issue``, ``pre_commit_check``, ``architecture_overview``) all
referenced MCP tools that the audit deleted — see prompts.py module
docstring for the full kill list. This test file was rewritten in the
same audit to cover the single surviving template (``onboard_session``)
plus the get_prompt() / list_prompts() machinery they share.
"""

from mcp_server.prompts import PROMPTS, get_prompt, list_prompts


class TestListPrompts:
    def test_returns_only_the_kept_prompt(self):
        """v3.0.0: one prompt — onboard_session. The other four were
        deleted in the audit because they referenced deleted MCP tools."""
        prompts = list_prompts()
        assert len(prompts) == 1
        assert prompts[0]["name"] == "onboard_session"

    def test_each_prompt_has_description(self):
        for p in list_prompts():
            assert "description" in p
            assert isinstance(p["description"], str)
            assert len(p["description"]) > 10

    def test_each_prompt_has_arguments_list(self):
        for p in list_prompts():
            assert "arguments" in p
            assert isinstance(p["arguments"], list)

    def test_onboard_session_has_no_arguments(self):
        prompts = {p["name"]: p for p in list_prompts()}
        assert prompts["onboard_session"]["arguments"] == []


class TestGetPrompt:
    def test_known_prompt_returns_dict(self):
        result = get_prompt("onboard_session")
        assert result is not None
        assert result["name"] == "onboard_session"

    def test_unknown_prompt_returns_none(self):
        assert get_prompt("nonexistent_prompt") is None
        # v3.0.0: previously-existing prompts are now unknown too.
        for legacy in (
            "review_changes",
            "debug_issue",
            "pre_commit_check",
            "architecture_overview",
        ):
            assert get_prompt(legacy) is None, (
                f"{legacy!r} should NOT come back from get_prompt — it was "
                f"deleted in the 2026-05-22 surface-cut audit"
            )

    def test_prompt_with_none_arguments(self):
        """get_prompt accepts None as a no-arg call shape."""
        result = get_prompt("onboard_session", None)
        assert result is not None

    def test_all_prompts_renderable(self):
        """Every registered prompt should render without error."""
        for name in PROMPTS:
            result = get_prompt(name)
            assert result is not None
            assert "messages" in result
            assert len(result["messages"]) == 1

    def test_message_structure(self):
        """The kept prompt's message structure matches the MCP shape
        that Claude Desktop / Code expect."""
        result = get_prompt("onboard_session")
        msg = result["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"]["type"] == "text"
        assert isinstance(msg["content"]["text"], str)

    def test_onboard_session_calls_get_session_context(self):
        """The body should instruct the agent to call
        get_session_context() — that's the single MCP entry the prompt
        is wrapped around."""
        result = get_prompt("onboard_session")
        text = result["messages"][0]["content"]["text"]
        assert "get_session_context" in text
