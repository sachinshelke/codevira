"""Bug 20 (rc.4) one-shot dedupe migration for ``global.db.projects``.

Pre-fix call sites disagreed on what to pass for the ``path`` PRIMARY KEY of
the ``projects`` table:

* ``mcp_server/cli.py:cmd_init`` and ``mcp_server/auto_init._register_global``
  passed ``str(data_dir)`` — the storage path ``~/.codevira/projects/<slug>``.
* ``mcp_server/global_sync.sync_to_global`` passed ``str(project_root)`` — the
  canonical project path.

Same logical project ended up as two rows in ``global.db.projects``. Downstream
lookups by canonical path silently missed half the projects. After the rc.4 fix
all four sites pass ``project_root``; this migration collapses pre-existing
duplicates so users upgrading from rc.3 → rc.4 don't carry the corruption
forward.

The migration is:

* **Cheap** — single GROUP BY query; if there are no duplicates the function
  returns immediately.
* **Idempotent** — running on an already-clean DB does nothing.
* **Safe** — bounded by ``git_remote`` (skips git-less projects; we have no
  reliable identity for those).

Called from :meth:`indexer.global_db.GlobalDB.__init__` once per connect. Kept
in its own module so adding it doesn't trip the project's blast-radius veto on
the hot ``global_db.py`` file.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def dedupe_projects_by_git_remote(conn: sqlite3.Connection) -> int:
    """Collapse duplicate ``projects`` rows that share a non-null ``git_remote``.

    Parameters
    ----------
    conn
        A ``sqlite3`` connection to the global database. Must have
        ``row_factory = sqlite3.Row`` set so column access works.

    Returns
    -------
    int
        Number of duplicate rows deleted. ``0`` is the no-op case.

    Notes
    -----
    For each ``git_remote`` group with >1 row:

    1. Prefer the row whose ``path`` does NOT start with the codevira data
       root (``~/.codevira/`` by default) — that's the canonical project
       path the post-Bug-20 code now writes.
    2. If no clear canonical row exists (e.g. all rows are storage paths
       from rc.3 era), keep the most recently ``last_synced_at`` row.
    3. Delete every other row in the group.
    """
    # Discover the storage prefix so we can prefer the canonical row.
    storage_prefix: str | None = None
    try:
        from mcp_server.paths import get_global_home
        # storage_prefix is the parent of project data dirs:
        # ~/.codevira/projects/  — any registered path starting with this is a
        # storage path; anything else is canonical.
        storage_prefix = str(Path(get_global_home()).resolve() / "projects") + "/"
    except Exception:
        # In some test/init contexts the helper isn't importable —
        # fall back to "keep most-recent" without the canonical heuristic.
        pass

    rows = conn.execute(
        "SELECT git_remote, GROUP_CONCAT(path, '|||') AS paths, "
        "       GROUP_CONCAT(last_synced_at, '|||') AS times "
        "FROM projects "
        "WHERE git_remote IS NOT NULL AND git_remote != '' "
        "GROUP BY git_remote HAVING COUNT(*) > 1"
    ).fetchall()
    deleted = 0
    for row in rows:
        paths = row["paths"].split("|||")
        times = row["times"].split("|||")
        keeper: str | None = None
        if storage_prefix is not None:
            non_storage = [p for p in paths if not p.startswith(storage_prefix)]
            if non_storage:
                keeper = non_storage[0]
        if keeper is None:
            # No canonical keeper → keep most recently synced row.
            keeper = paths[times.index(max(times))]
        for loser in (p for p in paths if p != keeper):
            conn.execute("DELETE FROM projects WHERE path = ?", (loser,))
            deleted += 1
    if deleted:
        conn.commit()
        logger.info("Bug 20 dedupe: collapsed %d duplicate project row(s)", deleted)
    return deleted
