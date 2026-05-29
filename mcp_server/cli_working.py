"""
cli_working.py — v3.1.0 M2 Phase 3: `codevira working` CLI subcommands.

Surface today:

    codevira working commit <session_id>

Copies non-evicted entries for ``session_id`` from
``.codevira-cache/working.jsonl`` (ephemeral, per-machine) to
``.codevira/working_archived/<session_id>.jsonl`` (canonical,
gitable). The cache file is left untouched so the agent can keep
iterating.

Future surface (reserved):

    codevira working list [--session SID]
    codevira working show <entry_id>
    codevira working clear --session SID --yes

Kept thin on purpose — the MCP tools (``working_add`` / ``working_get`` /
``working_promote``) are the agent-facing surface; this module is the
escape hatch for the human user who wants to operate on the cache
outside an IDE session.
"""

from __future__ import annotations

import sys


_OK = 0
_USAGE = 2
_FAILURE = 1


def cmd_working_commit(session_id: str | None) -> int:
    """Commit a session's live working entries to the canonical archive.

    Args:
        session_id: which session to commit. Required. The user
            usually copies this from ``working_get`` output or from
            their own slug they passed to ``working_add(session_id=...)``.

    Returns:
        0 on success (including empty session_id with no entries —
        reported as a no-op).
        1 on storage error.
        2 on missing session_id argument.
    """
    if not session_id:
        sys.stderr.write(
            "codevira working commit: error: session_id is required\n"
            "  Usage: codevira working commit <session_id>\n"
            "  Tip: run `codevira working list` to see live session_ids.\n"
        )
        return _USAGE

    try:
        from mcp_server.storage import working_store
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"codevira working commit: working_store import failed: {exc}\n"
        )
        return _FAILURE

    try:
        result = working_store.commit_session(session_id)
    except ValueError as exc:
        sys.stderr.write(f"codevira working commit: {exc}\n")
        return _FAILURE
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"codevira working commit: unexpected error: {exc}\n")
        return _FAILURE

    count = result.get("committed_count", 0)
    dest = result.get("destination")
    if count == 0:
        sys.stdout.write(
            f"codevira working commit: no live entries for session_id "
            f"{session_id!r} — nothing to commit.\n"
        )
        return _OK
    sys.stdout.write(
        f"codevira working commit: copied {count} entry/entries for "
        f"session_id {session_id!r}\n  -> {dest}\n"
        f"  (cache file .codevira-cache/working.jsonl untouched; "
        f"re-running is idempotent-with-appends)\n"
    )
    return _OK
