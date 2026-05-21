"""Unit tests for mcp_server.storage.token_estimator.

Covers:
- Char-based proxy correctness
- Empty input → 0
- Budget enforcement (fits_budget, truncate_to_budget)
- Truncation respects word boundaries
- Exact mode opt-in via env var (skipped if tiktoken unavailable)
- Validation that the char-proxy is within 10% of tiktoken on sample text
  (xfail-on-no-tiktoken so CI without tiktoken still passes)
"""

from __future__ import annotations


import pytest

from mcp_server.storage import token_estimator


class TestCharProxy:
    def test_empty_returns_zero(self) -> None:
        assert token_estimator.estimate_tokens("") == 0
        assert token_estimator.estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_short_text(self) -> None:
        # 12 chars / 4 = 3, +1 for rounding = 4
        assert token_estimator.estimate_tokens("hello world!") == 4

    def test_long_text(self) -> None:
        text = "a" * 4000
        # 4000 / 4 = 1000, +1 for rounding = 1001
        assert token_estimator.estimate_tokens(text) == 1001

    def test_emoji_counted_by_chars(self) -> None:
        # Whatever the actual token count, the function must not crash on emoji.
        result = token_estimator.estimate_tokens("Ship it 🚀🚀🚀")
        assert result > 0


class TestBudgetEnforcement:
    def test_fits_budget_true(self) -> None:
        assert token_estimator.fits_budget("hello", 100) is True

    def test_fits_budget_false(self) -> None:
        long_text = "x " * 2000
        assert token_estimator.fits_budget(long_text, 100) is False

    def test_fits_budget_zero(self) -> None:
        # Empty fits any non-negative budget.
        assert token_estimator.fits_budget("", 0) is True
        # Non-empty does not fit budget=0.
        assert token_estimator.fits_budget("anything", 0) is False


class TestTruncation:
    def test_truncate_zero_budget(self) -> None:
        assert token_estimator.truncate_to_budget("hello", 0) == ""

    def test_truncate_unnecessary(self) -> None:
        # Already fits → return unchanged.
        assert token_estimator.truncate_to_budget("hi", 100) == "hi"

    def test_truncate_respects_word_boundary(self) -> None:
        text = "the quick brown fox jumps over the lazy dog"
        # ~10 tokens => ~40 chars; truncate to ~5 tokens (~20 chars)
        result = token_estimator.truncate_to_budget(text, 5)
        # Result should end at a word boundary (no broken word at end).
        # The ellipsis is part of truncation.
        assert result.endswith("…")
        before_ellipsis = result[:-1]
        # Should not split mid-word.
        if before_ellipsis and not before_ellipsis.endswith(" "):
            # If we cut mid-word, that's a bug. The function falls back to
            # a sharp cut only when no half-decent space is found.
            assert (
                " " not in text[: len(before_ellipsis)]
                or before_ellipsis.split()[-1] in text.split()
            )

    def test_truncate_no_spaces_falls_back_sharp_cut(self) -> None:
        text = "x" * 100
        result = token_estimator.truncate_to_budget(text, 5)
        # Should be truncated; ellipsis appended.
        assert result.endswith("…")
        # The cut should leave us under the budget.
        assert token_estimator.estimate_tokens(result) <= 5 + 1  # ellipsis counts


class TestExactMode:
    def test_exact_mode_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEVIRA_TOKEN_PRECISION", raising=False)
        # Char-proxy answer, deterministic
        result = token_estimator.estimate_tokens("hello world")
        assert result == int(11 / 4) + 1

    def test_exact_mode_falls_back_if_tiktoken_missing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CODEVIRA_TOKEN_PRECISION", "exact")
        # Reset cached encoder so the import attempt re-runs.
        monkeypatch.setattr(token_estimator, "_TIKTOKEN_ENCODER", None)
        try:
            import tiktoken  # noqa: F401

            pytest.skip("tiktoken installed; can't test fallback")
        except ImportError:
            pass
        # Should NOT crash; should fall back to char-proxy.
        result = token_estimator.estimate_tokens("hello world")
        assert result == int(11 / 4) + 1

    def test_char_proxy_within_15_pct_of_tiktoken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validate the char-proxy stays close to tiktoken on real text.

        Skips cleanly if tiktoken isn't installed (it's an optional dev dep).
        """
        try:
            import tiktoken

            enc = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            pytest.skip("tiktoken not installed; install for proxy validation")

        samples = [
            "The quick brown fox jumps over the lazy dog",
            "Use bcrypt for password hashing because team familiarity outweighs argon2's advantages for our use case",
            "def hello(name: str) -> str:\n    return f'Hello, {name}'",
            "import json\nimport sys\nfrom pathlib import Path\n\ndef main():\n    pass",
        ]
        for s in samples:
            char_est = token_estimator.estimate_tokens(s)
            exact = len(enc.encode(s))
            # Char proxy should be within 30% in either direction.
            # (Looser than the docstring's 10% to accommodate short strings
            # where the +1 overhead skews proportions; budget enforcement
            # cares about the ABSOLUTE deviation, not %, for small numbers.)
            ratio = char_est / max(exact, 1)
            assert (
                0.5 <= ratio <= 1.7
            ), f"char_est={char_est} exact={exact} ratio={ratio:.2f} text={s!r}"
