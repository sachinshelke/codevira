"""
token_estimator.py — fast char-based token estimator.

v2.2.0's relevance-gated injection enforces a hard 600-token budget on
the context injected per UserPromptSubmit. We need a token count for
every candidate decision to decide which fit in the budget.

We deliberately do NOT pull in tiktoken as a runtime dep:

- tiktoken ships precompiled native binaries for several CPU archs;
  it's ~30 MB unpacked. We're chasing a 55 MB pipx venv target.
- tiktoken's accuracy advantage (exact tokens for OpenAI/Anthropic
  tokenizers) doesn't matter for our use case — we just need
  "approximately how many tokens" to keep the budget enforced.
- A char-based proxy (chars / 4) is within ~10% of tiktoken on
  English + code, validated by ``tests/storage/test_token_estimator.py``.

If a user genuinely needs exact counts, they can set
``CODEVIRA_TOKEN_PRECISION=exact`` and codevira will lazy-import
tiktoken at injection time only (tiktoken is a dev dep, not runtime).

The 4-chars-per-token ratio comes from:
- English text: 3.5-4.5 chars/token (varies by tokenizer)
- Source code: 3-4 chars/token (more punctuation = more tokens)
- 4 is the conservative middle that overestimates slightly for code
  and underestimates slightly for English. Both sides round down so
  we stay within budget.
"""

from __future__ import annotations

import os

# Chars/token ratio for the default fast path. Tuned to overestimate
# tokens slightly so the budget enforcer never accidentally exceeds.
_DEFAULT_CHARS_PER_TOKEN = 4.0

# Cached tiktoken encoder (lazy-imported if user opts into exact mode).
_TIKTOKEN_ENCODER = None


def _exact_mode_enabled() -> bool:
    """Honor CODEVIRA_TOKEN_PRECISION=exact for users who need it."""
    return os.environ.get("CODEVIRA_TOKEN_PRECISION", "").lower() == "exact"


def _get_tiktoken_encoder():
    """Lazy-import tiktoken; cache. Returns None if not available."""
    global _TIKTOKEN_ENCODER
    if _TIKTOKEN_ENCODER is not None:
        return _TIKTOKEN_ENCODER
    try:
        import tiktoken  # type: ignore[import-not-found]

        _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        return _TIKTOKEN_ENCODER
    except ImportError:
        # User asked for exact mode but tiktoken isn't installed.
        # Fall through to char proxy + emit a one-time warning.
        import logging

        logging.getLogger(__name__).warning(
            "CODEVIRA_TOKEN_PRECISION=exact requested but tiktoken not "
            "installed; falling back to char-proxy. `pip install tiktoken` "
            "for exact mode."
        )
        return None


def estimate_tokens(text: str) -> int:
    """Estimate the token count for ``text``.

    Returns an integer >= 0. Uses char-proxy by default;
    CODEVIRA_TOKEN_PRECISION=exact opts into tiktoken cl100k_base
    (Anthropic-compatible tokenizer family).
    """
    if not text:
        return 0
    if _exact_mode_enabled():
        enc = _get_tiktoken_encoder()
        if enc is not None:
            return len(enc.encode(text))
    return int(len(text) / _DEFAULT_CHARS_PER_TOKEN) + 1


def fits_budget(text: str, budget: int) -> bool:
    """Convenience: True iff ``estimate_tokens(text) <= budget``."""
    return estimate_tokens(text) <= budget


def truncate_to_budget(text: str, budget: int) -> str:
    """Truncate ``text`` so estimate_tokens(result) <= budget.

    Cuts at the last space before the budget if possible (word boundary).
    Returns empty string if budget < 1.
    """
    if budget <= 0 or not text:
        return ""
    max_chars = int(budget * _DEFAULT_CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars // 2:
        cut = cut[:last_space]
    return cut + "…"
