"""
sanitize.py — shared secret-scrubbing for memory stores.

Both M3 (skills) and M8 (reflections) need to redact recognised
secret-shaped substrings before persisting text that may leak into
committed JSONL files / playbook markdown / LLM context. Centralised
here so a new pattern lands in both subsystems at once.

Goal: catch obvious accidents (API keys, Bearer tokens, AWS access
keys, long opaque tokens, base64 blobs). Not a crypto defence — over-
redaction is acceptable, missed secrets are not.
"""

from __future__ import annotations

import re

# Each pattern carries a label that surfaces in the redacted marker
# (``<redacted:KIND>``) so the downstream reader knows what was
# scrubbed without exposing the content.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api-key", re.compile(r"(?i)\b(api[_-]?key)\s*[:=]\s*\S+")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}")),
    ("password", re.compile(r"(?i)\bpassword\s*[:=]\s*\S+")),
    ("aws-akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Long hex / base64 blob — 32+ chars of plausible token material.
    ("long-token", re.compile(r"\b[A-Fa-f0-9]{32,}\b")),
    # ONE pattern over the FULL base64 alphabet, including `/`. Whether a
    # given match is a secret or a filesystem path is decided afterwards by
    # :func:`_b64_is_path` — deliberately not by a second, narrower regex.
    #
    # It was two regexes (a no-slash one, then a slash-one gated on `+`/`=`)
    # and the ordering leaked: the no-slash pattern fired FIRST on the
    # pre-slash half of a real token, which broke the run up so the
    # slash-aware pattern could no longer see it whole. Measured::
    #
    #     'Q'*45 + '/Zm9vYmFy+secretTAIL1234'
    #       -> '<redacted:long-b64>/Zm9vYmFy+secretTAIL1234'
    #
    # i.e. the tail shipped in the clear, in a module whose whole policy is
    # "over-redaction is acceptable, missed secrets are not". One pattern
    # plus one predicate has no ordering to get wrong.
    ("long-b64", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
)


def _b64_is_path(run: str) -> bool:
    """True when a 40+ char base64-alphabet run is really a filesystem path.

    A path is the only realistic non-secret that reaches this length in this
    alphabet, and it is distinguished by carrying `/` while carrying neither
    `+` nor `=`: standard-alphabet base64 of this length almost always has
    padding or a `+`, and POSIX paths essentially never have either.

    Why this matters (4.0 Step 3.1): the rule used to redact any `/`-bearing
    run unconditionally, so 596 of 655 working-memory edit observations (91%)
    on a real store had their file path destroyed — gutting the capture layer
    the anchor work depends on. Narrowing only the slash case keeps every
    other over-redaction exactly as it was.
    """
    return "/" in run and "+" not in run and "=" not in run


def _redactor(kind: str):
    """Replacement callable for one pattern kind."""
    marker = f"<redacted:{kind}>"
    if kind != "long-b64":
        return lambda _m: marker

    def _b64(m: re.Match[str]) -> str:
        run = m.group(0)
        return run if _b64_is_path(run) else marker

    return _b64


_REDACTORS = {kind: _redactor(kind) for kind, _ in _SECRET_PATTERNS}


def scrub_sensitive(text: str) -> str:
    """Replace recognised secret-shaped substrings with
    ``<redacted:kind>`` markers. Conservative — better to over-redact
    than to ship a key into a committed memory file.

    Non-string / empty inputs round-trip unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for kind, pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTORS[kind], out)
    return out
