"""
changesets.py — DEPRECATED stub (v2.2.0+).

The changesets feature (multi-file work tracking via start_changeset /
update_changeset_progress / complete_changeset / list_open_changesets)
was removed in v2.2.0 per the 2026-05-22 surface-cut audit. The audit
found zero historical usage in user data dirs.

This file remains as a no-op stub solely so that existing test patches
targeting ``mcp_server.tools.changesets.list_open_changesets`` continue
to resolve. Production code paths no longer import from this module.

Slated for full removal in v2.3.0 once tests have been refactored away
from the legacy patch target.
"""

from __future__ import annotations


def list_open_changesets() -> dict:
    """Always returns an empty changeset list.

    Kept as a test-fixture compatibility shim — production code no
    longer calls this function. The whole concept was deleted in
    v2.2.0 along with the start/update/complete tools.
    """
    return {"open_changesets": [], "count": 0, "warning": None}


# Legacy function stubs — also raise NotImplementedError if production
# code accidentally calls them (no production code should after v2.2.0).
def start_changeset(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
    raise NotImplementedError(
        "changesets removed in v2.2.0 (surface-cut audit 2026-05-22). "
        "Record decisions via record_decision() instead."
    )


def update_changeset_progress(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
    raise NotImplementedError(
        "changesets removed in v2.2.0 (surface-cut audit 2026-05-22). "
        "Record decisions via record_decision() instead."
    )


def complete_changeset(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
    raise NotImplementedError(
        "changesets removed in v2.2.0 (surface-cut audit 2026-05-22). "
        "Record decisions via record_decision() instead."
    )
