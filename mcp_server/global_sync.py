"""
global_sync.py — Register the current project in the cross-machine
                 codevira inventory (``~/.codevira/global.db``).

v3.0.0 (2026-05-22 surface-cut audit): the original module pushed
preferences + learned_rules between project DBs and the global DB.
Those features were deleted in the audit (the preferences / learned-
rules MCP tools returned noise more often than signal). What remains
is the one piece of cross-project value: the **project registry**
that ``codevira projects`` reads to enumerate every codevira-using
project on this machine.

All operations are best-effort: if global DB is locked, missing, or
corrupt, the server operates normally and the registration just
doesn't happen this session.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_current_project() -> dict:
    """Register this project in ``~/.codevira/global.db`` (idempotent).

    Called on MCP server startup. Updates the global registry so
    ``codevira projects`` can list every project the user has opened
    with codevira on this machine. Returns a small summary dict.

    Best-effort: any error returns ``{"registered": False, "error": ...}``
    so the caller can swallow it without breaking server startup.
    """
    try:
        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_db_path, get_project_root

        project_root = get_project_root()
        if project_root is None:
            return {"registered": False, "reason": "no project root"}

        global_db_path = get_global_db_path()
        global_db = GlobalDB(global_db_path)
        try:
            global_db.register_project(
                str(project_root),
                project_root.name,
                _get_project_language() or "unknown",
            )
        finally:
            global_db.close()
        return {"registered": True, "project_root": str(project_root)}
    except Exception as exc:  # noqa: BLE001 — best-effort, don't break startup
        logger.warning("global_sync.register_current_project: %s", exc)
        return {"registered": False, "error": str(exc)}


# v2.x BACKWARD-COMPAT shim — kept ONLY so external callers / tests
# that mock ``mcp_server.global_sync.import_global_to_project`` keep
# working through the v3.0.0 rename. New code should use
# ``register_current_project()`` directly.
def import_global_to_project() -> dict:
    """Deprecated alias for ``register_current_project()``.

    The old function name suggested two-way sync of preferences and
    learned_rules; v3.0.0 deleted those features, so the function now
    just registers the project. Renamed for honesty;  this shim stays
    until the next major release.
    """
    return register_current_project()


def _get_project_language() -> str | None:
    """Read the project language from .codevira/config.yaml.

    Returns the configured language string (e.g. ``"python"``), or
    ``None`` if config is missing / unparseable. Best-effort: never
    raises — global registration shouldn't fail because the config
    file has a typo.
    """
    try:
        import yaml

        from mcp_server.paths import get_data_dir

        config_path = get_data_dir() / "config.yaml"
        if not config_path.is_file():
            return None
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        project = config.get("project", config)
        return project.get("language")
    except Exception as exc:  # noqa: BLE001
        logger.debug("global_sync._get_project_language: %s", exc)
        return None
