"""
intent_classifier.py — Hero 9's pure regex-based intent classifier
and file-mention extractor.

Used by ``mcp_server/engine/policies/intent_inference.py`` to decide
which signals to pre-fetch on UserPromptSubmit.

v2.0-alpha is regex-only (local-first). v2.1 may add an optional
LLM-backed classifier behind an env var for users who explicitly
opt in to a local LLM (Ollama, llama.cpp, etc.).

Design properties:

  - **Pure**: no I/O, no signals access, no env vars. The policy
    layer wraps env-var configuration; this module just classifies.
  - **Ordered specificity**: ``"fix the broken test"`` classifies
    as ``fix-bug`` (specific intent), not ``test`` (general). Order
    of the patterns matters and is documented inline.
  - **Conservative file extraction**: prefer false negatives over
    false positives. ``email@example.com`` must NOT be extracted
    as ``example.com``. ``5.0.1`` must NOT be extracted as a file.
    The extension allowlist defends against this.

Public API:
  - ``classify_intent(prompt: str) -> Intent``
  - ``extract_file_mentions(prompt: str, max_files: int = 3) -> list[str]``
"""
from __future__ import annotations

import re
from typing import Final


# ---------------------------------------------------------------------
# Intent labels
# ---------------------------------------------------------------------

#: Six concrete intents + ``other`` fallback. Defined as plain strings
#: (not Enum) so policies can stringify them cheaply.
INTENT_FIX_BUG: Final[str] = "fix-bug"
INTENT_ADD_FEATURE: Final[str] = "add-feature"
INTENT_REFACTOR: Final[str] = "refactor"
INTENT_EXPLAIN: Final[str] = "explain"
INTENT_TEST: Final[str] = "test"
INTENT_DOCS: Final[str] = "docs"
INTENT_OTHER: Final[str] = "other"

INTENTS: Final[tuple[str, ...]] = (
    INTENT_FIX_BUG, INTENT_ADD_FEATURE, INTENT_REFACTOR,
    INTENT_EXPLAIN, INTENT_TEST, INTENT_DOCS, INTENT_OTHER,
)


# ---------------------------------------------------------------------
# Pattern table — ORDER IS SIGNIFICANT
# ---------------------------------------------------------------------

# Specific intents (fix-bug, test, docs) come first so that prompts like
# "fix the broken test" classify as fix-bug rather than test, and
# "rewrite the docstring" classifies as docs rather than refactor.
#
# Within each intent, patterns are ordered roughly by specificity too
# (multi-word phrases first, then single words).

_INTENT_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    # ----- fix-bug — most specific; runs FIRST so "fix the test" wins -----
    (INTENT_FIX_BUG, (
        re.compile(r"\bfix\b", re.IGNORECASE),
        re.compile(r"\bbug(s|gy)?\b", re.IGNORECASE),
        re.compile(r"\bbroken\b", re.IGNORECASE),
        re.compile(r"\bdoesn'?t\s+work\b", re.IGNORECASE),
        re.compile(r"\bnot\s+working\b", re.IGNORECASE),
        re.compile(r"\berror\b", re.IGNORECASE),
        re.compile(r"\bcrash(ing|ed)?\b", re.IGNORECASE),
        re.compile(r"\bfailing\b", re.IGNORECASE),
        re.compile(r"\bregression\b", re.IGNORECASE),
        re.compile(r"\bissue\b", re.IGNORECASE),
    )),
    # ----- test — runs SECOND so "add a test" beats "add" feature -----
    (INTENT_TEST, (
        re.compile(r"\b(write|add|create)\s+(a\s+|an\s+|some\s+)?tests?\b", re.IGNORECASE),
        re.compile(r"\btest\s+coverage\b", re.IGNORECASE),
        re.compile(r"\bunit\s+tests?\b", re.IGNORECASE),
        re.compile(r"\bintegration\s+tests?\b", re.IGNORECASE),
    )),
    # ----- docs — similar reason -----
    (INTENT_DOCS, (
        re.compile(r"\b(write|add|update)\s+(docs?|comments?|docstrings?)\b", re.IGNORECASE),
        re.compile(r"\bdocument\b", re.IGNORECASE),
        re.compile(r"\bdocstring\b", re.IGNORECASE),
    )),
    # ----- explain -----
    (INTENT_EXPLAIN, (
        re.compile(r"\bexplain\b", re.IGNORECASE),
        re.compile(r"\bwhat\s+does\b", re.IGNORECASE),
        re.compile(r"\bhow\s+does\b", re.IGNORECASE),
        re.compile(r"\bdescribe\b", re.IGNORECASE),
        re.compile(r"\bsummari[sz]e\b", re.IGNORECASE),
    )),
    # ----- refactor -----
    (INTENT_REFACTOR, (
        re.compile(r"\brefactor\b", re.IGNORECASE),
        re.compile(r"\bclean[\s-]*up\b", re.IGNORECASE),
        re.compile(r"\bsimplify\b", re.IGNORECASE),
        re.compile(r"\brename\b", re.IGNORECASE),
        re.compile(r"\bextract\b.*\b(method|function|module|class)\b", re.IGNORECASE),
    )),
    # ----- add-feature — broadest; runs LAST among specific intents -----
    (INTENT_ADD_FEATURE, (
        re.compile(r"\b(add|implement|create|build)\b\s+(?!.*\b(tests?|docs?|comments?)\b)", re.IGNORECASE),
        re.compile(r"\bnew\s+(feature|endpoint|method|function|module)\b", re.IGNORECASE),
        re.compile(r"\bmake\s+(it|this|a)\s+\w+\s+(do|return|handle|support)\b", re.IGNORECASE),
    )),
)


def classify_intent(prompt: str) -> str:
    """Classify a user prompt into one of the seven intents.

    Returns ``INTENT_OTHER`` when no patterns match. Always returns a
    string from ``INTENTS``.

    Note on ordering: see the inline comments in ``_INTENT_PATTERNS``.
    Specific intents (fix-bug, test, docs) are matched before broader
    ones (refactor, add-feature) so that compound prompts resolve to
    the more actionable label.
    """
    if not prompt:
        return INTENT_OTHER
    for intent, patterns in _INTENT_PATTERNS:
        for p in patterns:
            if p.search(prompt):
                return intent
    return INTENT_OTHER


# ---------------------------------------------------------------------
# File-mention extractor
# ---------------------------------------------------------------------

#: Extensions we recognize as code files. Conservative — favors
#: false negatives (we'd rather miss `foo.cpp` than match `foo.com`).
_KNOWN_EXTENSIONS: frozenset[str] = frozenset({
    # Python
    "py", "pyi",
    # JS / TS
    "js", "jsx", "ts", "tsx", "mjs", "cjs",
    # Web
    "html", "htm", "css", "scss", "sass", "less",
    # Backend / systems
    "go", "rs", "java", "kt", "scala", "rb", "php",
    "c", "h", "cpp", "cc", "cxx", "hpp",
    "cs", "swift", "m", "mm",
    # Config / data
    "json", "yaml", "yml", "toml", "ini", "cfg",
    # Docs
    "md", "rst", "txt",
    # Build
    "sh", "bash", "fish", "zsh",
    "dockerfile", "makefile",
})

#: Regex captures path-like tokens with extensions. Anchored to a
#: word boundary (start, whitespace, quote, paren) on the left and
#: a word boundary or punctuation on the right. Captures group 1.
#:
#: Examples that match:
#:   ``auth.py``, ``src/auth.py``, ``tests/test_auth.py``
#:   "fix `auth.py`": → ``auth.py``
#:   "in (foo.go), the func": → ``foo.go``
#: Examples that DO NOT match:
#:   ``v1.2.3`` (no extension that's in the allowlist)
#:   ``email@example.com`` (the `@` breaks the boundary)
#:   ``5.0.1`` (digits-only "ext" rejected by the extension filter)
_FILE_MENTION_RE = re.compile(
    r"(?:^|[\s'\"`(\[{,])"
    r"((?:[\w.\-]+/)*[\w.\-]+\.([A-Za-z][A-Za-z0-9]{0,4}))"
    r"(?=$|[\s'\"`)\]}.,:;!?])"
)


def extract_file_mentions(prompt: str, *, max_files: int = 3) -> list[str]:
    """Return up to ``max_files`` file-path-looking tokens from the prompt.

    Filters:
      - Extension must be in ``_KNOWN_EXTENSIONS``.
      - Distinct (deduped, first-occurrence wins).
      - max_files clamped to [1, 10].

    Returns: list of strings as they appeared in the prompt (case
    preserved). Empty list if nothing matched.

    Defensive: the regex doesn't anchor to filesystem semantics; the
    caller (the policy) treats results as best-effort hints. A
    matched string that doesn't actually exist on disk just means
    the signals' lookups return empty.
    """
    if not prompt:
        return []
    cap = max(1, min(int(max_files), 10))
    seen: set[str] = set()
    out: list[str] = []
    for match in _FILE_MENTION_RE.finditer(prompt):
        full = match.group(1)
        ext = match.group(2).lower()
        if ext not in _KNOWN_EXTENSIONS:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
        if len(out) >= cap:
            break
    return out
