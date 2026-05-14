"""Bug 21a (rc.4) self-healing for "ghost" project data directories.

A *ghost* dir is a ``~/.codevira/projects/<slug>/`` that exists on disk but is
missing one or more of:

* ``config.yaml`` — required for the project to be considered "initialized".
* ``metadata.json`` — used by ``codevira projects`` inventory + git-remote-based
  rename-resilient lookup.
* A row in ``~/.codevira/global.db`` under the ``projects`` table.

How ghosts are born: an MCP tool (e.g. ``get_roadmap``, decision-write tools,
graph queries) calls into a helper that resolves ``get_data_dir()``, then writes
a file underneath — ``mkdir(parents=True, exist_ok=True)`` creates the
``~/.codevira/projects/<slug>/`` tree as a side effect. The background
``_run_background_init`` thread either crashed mid-flight or never started for
that project, leaving the data dir partially populated.

User symptom: ``~/.codevira/projects/`` accumulates dirs the developer never
asked for, and they don't show up in ``codevira status --global`` /
``codevira projects`` because they're not in ``global.db``.

This module fixes that by repairing the cheap-to-write pieces synchronously
whenever :func:`mcp_server.auto_init.ensure_project_initialized` is called.
The heavy parts (graph generation, semantic indexing) still go through the
background thread; this is just the bookkeeping bootstrap.

Keeping the implementation in its own module avoids inflating the public
signature surface of ``mcp_server/auto_init.py`` (which has high downstream
blast radius).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def repair_incomplete_init(data_dir: Path, project_root: Path) -> dict:
    """Cheap synchronous repair for ghost project dirs.

    Writes any of ``config.yaml`` / ``metadata.json`` / ``global.db.projects``
    row that's missing. Each piece is idempotent and cheap (one yaml/json
    file write, or one SQL insert). Does NOT trigger graph generation or
    semantic indexing.

    Parameters
    ----------
    data_dir
        Project storage dir (``~/.codevira/projects/<slug>/``). Created if
        absent.
    project_root
        Canonical project path. Used for detection, metadata, and the
        ``path`` key in ``global.db.projects`` (post-Bug-20 conventions).

    Returns
    -------
    dict
        ``{"config_written": bool, "metadata_written": bool,
        "registered": bool}`` — caller can log what was repaired.
    """
    repaired = {
        "config_written": False,
        "metadata_written": False,
        "registered": False,
    }

    # Lazy imports — keep the cold path light + avoid import cycles.
    from mcp_server.detect import auto_detect_project
    from mcp_server.auto_init import _write_config, _write_metadata

    detected_cache: dict | None = None

    def _detected() -> dict:
        nonlocal detected_cache
        if detected_cache is None:
            detected_cache = auto_detect_project(project_root)
        return detected_cache

    # (1) config.yaml — without this, ensure_project_initialized() will keep
    # firing background init on every tool call. Required gate.
    if not (data_dir / "config.yaml").is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_config(data_dir, _detected(), project_root)
        repaired["config_written"] = True

    # (2) metadata.json — needed by `codevira projects` inventory + git_remote
    # rename-resilient lookup.
    if not (data_dir / "metadata.json").is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_metadata(data_dir, project_root)
        repaired["metadata_written"] = True

    # (3) global.db registration — without this, the project is invisible to
    # cross-project search and inventory commands.
    try:
        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_db_path, _get_git_remote_url
        gdb = GlobalDB(get_global_db_path())
        try:
            existing = gdb.conn.execute(
                "SELECT 1 FROM projects WHERE path = ?",
                (str(project_root),),
            ).fetchone()
            if not existing:
                d = _detected()
                gdb.register_project(
                    path=str(project_root),
                    name=d["name"],
                    language=d["language"],
                    git_remote=_get_git_remote_url(project_root),
                )
                repaired["registered"] = True
        finally:
            gdb.close()
    except Exception as e:
        # Don't fail the whole repair if global.db is unavailable.
        logger.warning("Bug 21a: global.db registration repair skipped: %s", e)

    if any(repaired.values()):
        logger.info(
            "Bug 21a self-heal for %s: %s",
            project_root,
            ", ".join(k for k, v in repaired.items() if v),
        )
    return repaired
