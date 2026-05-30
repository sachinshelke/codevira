"""
origin.py — v3.1.0 M1: provenance metadata for cross-IDE memory.

Every write Codevira makes (decisions, sessions, working memory, skills,
activity, reflections) carries an ``origin`` dict that records *who*
made the write — which IDE, which agent model, which machine, when.
This is Phase A of the v3.1.0 Consensus subsystem: real provenance
that ``check_conflict`` and ``get_session_context`` can surface so
agents can answer "this decision contradicts a do_not_revert one
written by Cursor 3 days ago — what would you like to do?"

# Schema

``current_origin()`` returns::

    {
      "ide":         "claude_code" | "claude_desktop" | "cursor" |
                     "windsurf"    | "antigravity"    | "unknown",
      "agent_model": "<model-id>" | None,
      "host_hash":   "<12 hex chars>",
      "ts":          "2026-05-28T10:00:00+00:00",
    }

# Field sources

- ``ide``: read from the ``CODEVIRA_IDE`` env var, which
  ``ide_inject.py`` writes into each detected IDE's MCP server config.
  Defaults to ``"unknown"`` when unset (e.g., bare ``codevira`` CLI
  invocations or pre-v3.1 IDE configs).
- ``agent_model``: ``CODEVIRA_AGENT_MODEL`` env var (optional; most
  IDEs don't expose model id to MCP servers in v3.1, so this is
  commonly ``None``).
- ``host_hash``: ``sha1(uuid.getnode() bytes + username)[:12]``. The
  MAC + username combination is stable per machine across reboots
  (assuming the NIC is real, not a randomized fallback). The SHA1
  truncation is privacy-preserving — no plaintext hostname or
  username leaks if a team commits a ``decisions.jsonl`` to a public
  repo.
- ``ts``: ISO 8601 UTC timestamp of the call.

# Backward compatibility

v3.0.x records have no ``origin`` field. All readers MUST treat the
absence as ``ide="unknown"`` — never raise, never migrate. This file
deliberately does not provide a "fill missing origin" helper because
the value of provenance is in NEW records; back-filling fake origins
on old records would falsely attest authorship.

# Non-goals (v3.1.0)

- Cross-machine consistency. v3.1.0 assumes one machine across many
  IDEs. Two developers on two machines will have different
  ``host_hash`` values; the conflict-materialization layer treats
  them as foreign origins. Cross-machine sync is v3.2+.
- Tamper resistance. ``host_hash`` is not a security primitive — a
  malicious actor can set ``CODEVIRA_IDE`` to whatever they want. The
  field is for informational provenance only.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache


# Sentinel returned when the IDE env var is unset.
_IDE_UNKNOWN = "unknown"


def _normalize_agent_model(raw: str | None) -> str | None:
    """Strip whitespace; coerce the strings 'null' / 'none' / '' to None.

    v3.1.x fix: previously a CODEVIRA_AGENT_MODEL set to whitespace OR
    the literal string 'null'/'None' passed through to the origin
    record. Downstream consensus checks string-compare agent_model;
    those bogus values would polute the provenance chain.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.lower() in {"null", "none"}:
        return None
    return s


def current_origin() -> dict[str, str | None]:
    """Build the origin dict for *this* call.

    ``ts`` is freshly computed each call so per-record timestamps
    are honest. ``host_hash`` is cached (machine identity doesn't
    change between calls in the same process). ``ide`` and
    ``agent_model`` are read each call so a test that monkeypatches
    ``CODEVIRA_IDE`` mid-process sees the override.
    """
    return {
        "ide": os.environ.get("CODEVIRA_IDE", _IDE_UNKNOWN),
        "agent_model": _normalize_agent_model(os.environ.get("CODEVIRA_AGENT_MODEL")),
        "host_hash": _host_hash(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@lru_cache(maxsize=1)
def _host_hash() -> str:
    """sha1(uuid.getnode() bytes + username)[:12].

    Cached because both inputs are process-stable. Falls back to
    ``"unknown"`` if neither source is readable (extremely unusual —
    a container without /etc/passwd and without a usable network
    interface — but documented for completeness).
    """
    try:
        node = uuid.getnode()
        mac_bytes = node.to_bytes(6, "big")
    except Exception:  # pragma: no cover — uuid.getnode() shouldn't raise
        mac_bytes = b""

    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover
        user = ""

    raw = mac_bytes + user.encode("utf-8", errors="replace")
    if not raw:
        return "unknown"
    return hashlib.sha1(raw).hexdigest()[:12]
