"""
Tests for mcp_server/prompts.py — MCP workflow prompt templates.
"""
from mcp_server.prompts import list_prompts, get_prompt, PROMPTS


class TestListPrompts:
    def test_returns_all_five_prompts(self):
        prompts = list_prompts()
        assert len(prompts) == 5

    def test_prompt_names(self):
        names = {p["name"] for p in list_prompts()}
        assert names == {
            "review_changes",
            "debug_issue",
            "onboard_session",
            "pre_commit_check",
            "architecture_overview",
        }

    def test_each_prompt_has_description(self):
        for p in list_prompts():
            assert "description" in p
            assert isinstance(p["description"], str)
            assert len(p["description"]) > 10

    def test_each_prompt_has_arguments_list(self):
        for p in list_prompts():
            assert "arguments" in p
            assert isinstance(p["arguments"], list)

    def test_debug_issue_has_required_argument(self):
        prompts = {p["name"]: p for p in list_prompts()}
        debug = prompts["debug_issue"]
        assert len(debug["arguments"]) == 1
        assert debug["arguments"][0]["name"] == "description"
        assert debug["arguments"][0]["required"] is True

    def test_review_changes_has_optional_argument(self):
        prompts = {p["name"]: p for p in list_prompts()}
        review = prompts["review_changes"]
        assert len(review["arguments"]) == 1
        assert review["arguments"][0]["required"] is False

    def test_onboard_session_has_no_arguments(self):
        prompts = {p["name"]: p for p in list_prompts()}
        assert prompts["onboard_session"]["arguments"] == []


class TestGetPrompt:
    def test_known_prompt_returns_dict(self):
        result = get_prompt("debug_issue", {"description": "Login fails"})
        assert result is not None
        assert result["name"] == "debug_issue"

    def test_argument_substitution(self):
        result = get_prompt("debug_issue", {"description": "Login fails on mobile"})
        text = result["messages"][0]["content"]["text"]
        assert "Login fails on mobile" in text
        assert "{description}" not in text

    def test_unknown_prompt_returns_none(self):
        assert get_prompt("nonexistent_prompt") is None

    def test_prompt_without_arguments(self):
        result = get_prompt("onboard_session")
        assert result is not None
        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"

    def test_prompt_with_none_arguments(self):
        result = get_prompt("pre_commit_check", None)
        assert result is not None

    def test_all_prompts_renderable(self):
        """Every prompt should render without error."""
        for name in PROMPTS:
            result = get_prompt(name)
            assert result is not None
            assert "messages" in result
            assert len(result["messages"]) == 1

    def test_message_structure(self):
        result = get_prompt("architecture_overview")
        msg = result["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"]["type"] == "text"
        assert isinstance(msg["content"]["text"], str)

    def test_unused_placeholder_stays(self):
        """If an argument is not provided, its placeholder remains."""
        result = get_prompt("debug_issue")
        text = result["messages"][0]["content"]["text"]
        assert "{description}" in text

    def test_extra_arguments_ignored(self):
        result = get_prompt("debug_issue", {"description": "bug", "extra": "ignored"})
        assert result is not None
        text = result["messages"][0]["content"]["text"]
        assert "bug" in text
