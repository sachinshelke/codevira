"""Heuristic signal extraction — E2 (Phase 20). NO LLM in this path.

The scan is a deterministic, fast pass: it flags FAILED tool calls and
user CORRECTIONS using cheap lexical rules, then hands a sanitized digest
to the (LLM-backed) reflect/induce pipeline. Keeping the scan LLM-free
means it's free to run on every session and can't itself hallucinate.

Excerpts are always run through :func:`scrub_sensitive` and hard-capped,
so a prompt or stack trace pasted into a session can't leak a secret into
a reflection candidate.
"""

from __future__ import annotations

import re

from mcp_server.storage.sanitize import scrub_sensitive

# Per-session caps — a digest is a SIGNAL, not a replay.
MAX_FAILURES_PER_SESSION = 8
MAX_CORRECTIONS_PER_SESSION = 8
EXCERPT_CHARS = 160

# A user turn is a "correction" when it opens with / contains one of these.
# Deliberately conservative: we'd rather miss a soft correction than flag
# every neutral follow-up. Matched case-insensitively on the FIRST ~200 chars.
_CORRECTION_PATTERNS = (
    r"\bno,?\b",
    r"\bnope\b",
    r"\bdon'?t\b",
    r"\bstop\b",
    r"\bundo\b",
    r"\brevert\b",
    r"\binstead\b",
    r"\bactually\b",
    r"\bthat'?s (not|wrong|incorrect)\b",
    r"\bnot what\b",
    r"\bwrong\b",
    r"\bincorrect\b",
    r"\bshould(n'?t| not)\b",
    r"\bwhy did you\b",
    r"\byou (broke|missed|forgot)\b",
    r"\brather than\b",
    r"\bmistake\b",
)
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)

# Error markers in a tool-result/output string when the format lacks an
# explicit is_error flag (Codex/Gemini). Conservative substring set.
_ERROR_MARKERS = (
    "error",
    "exception",
    "traceback",
    "failed",
    "failure",
    "not found",
    "permission denied",
    "exit code 1",
    "exit status 1",
    "command not found",
)


def excerpt(text: str | None) -> str:
    """Sanitize + collapse-whitespace + cap a free-text snippet."""
    if not text:
        return ""
    cleaned = " ".join(scrub_sensitive(str(text)).split())
    if len(cleaned) <= EXCERPT_CHARS:
        return cleaned
    return cleaned[: EXCERPT_CHARS - 1] + "…"


# E4 (Phase 22) echo-safety: codevira's own managed marker-block, injected
# into CLAUDE.md / AGENTS.md / .cursor rules, can echo back into a transcript.
# It must never be mistaken for a user correction (that would let codevira
# re-ingest its own output). Marker substring is enough to recognize it.
_MANAGED_MARKER = "codevira:begin"


def is_managed_block(text: str | None) -> bool:
    """True if ``text`` contains codevira's injected managed-block marker."""
    return bool(text) and _MANAGED_MARKER in str(text)


def looks_like_correction(text: str | None) -> bool:
    """True if a user turn reads like a correction of the assistant.

    Only the leading window is scanned (corrections lead; long pastes that
    merely contain "wrong" deep inside don't count). E4 echo-safety: codevira's
    own injected managed-block is never a correction."""
    if not text or is_managed_block(text):
        return False
    return bool(_CORRECTION_RE.search(str(text)[:200]))


def output_looks_failed(output: object) -> bool:
    """Best-effort failure detection for formats without an is_error flag.

    Used by the Codex/Gemini parsers where a tool result is just an output
    string/blob. Conservative: only fires on explicit error markers.
    """
    if output is None:
        return False
    if isinstance(output, dict):
        # Common shapes: {"success": false}, {"error": ...}, {"exit_code": 1}
        if output.get("success") is False or output.get("error"):
            return True
        for key in ("exit_code", "returncode", "status_code"):
            val = output.get(key)
            if isinstance(val, int) and val != 0:
                return True
        text = str(output.get("output") or output.get("content") or output)
    else:
        text = str(output)
    low = text.lower()
    return any(marker in low for marker in _ERROR_MARKERS)
