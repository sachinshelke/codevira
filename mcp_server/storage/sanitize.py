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
    ("long-b64", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
)


def scrub_sensitive(text: str) -> str:
    """Replace recognised secret-shaped substrings with
    ``<redacted:kind>`` markers. Conservative — better to over-redact
    than to ship a key into a committed memory file.

    Non-string / empty inputs round-trip unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for kind, _pattern in _SECRET_PATTERNS:
        out = _pattern.sub(f"<redacted:{kind}>", out)
    return out
