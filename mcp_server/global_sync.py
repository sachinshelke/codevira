"""
global_sync.py — Sync intelligence between project and global databases.

- import_global_to_project(): Called on server startup. Imports qualifying
  global preferences and rules into the project's local database.
- export_project_to_global(): Called on session end. Pushes qualifying local
  preferences and rules to the global database.

All operations are best-effort: if global DB is locked, missing, or corrupt,
the server operates normally with local memory only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def import_global_to_project() -> dict:
    """
    Import qualifying global intelligence into the current project's database.
    Called on MCP server startup. Returns summary of what was imported.
    """
    from mcp_server.paths import get_global_db_path, get_data_dir, get_project_root
    from indexer.global_db import GlobalDB
    from indexer.sqlite_graph import SQLiteGraph

    stats = {"preferences_imported": 0, "rules_imported": 0}

    global_db_path = get_global_db_path()
    if not global_db_path.exists():
        return stats

    project_db_path = get_data_dir() / "graph" / "graph.db"
    if not project_db_path.exists():
        return stats

    # Read project language from config
    language = _get_project_language()

    global_db = GlobalDB(global_db_path)
    project_db = SQLiteGraph(project_db_path)

    try:
        # Import preferences with frequency >= 3
        global_prefs = global_db.get_preferences(min_frequency=3)
        for pref in global_prefs:
            # Check if already exists locally
            existing = project_db.conn.execute(
                "SELECT id FROM preferences WHERE category = ? AND signal = ?",
                (pref["category"], pref["signal"]),
            ).fetchone()
            if not existing:
                project_db.record_preference(
                    pref["category"], pref["signal"],
                    example=pref.get("example"), source="global",
                )
                stats["preferences_imported"] += 1

        # Import rules with confidence >= 0.6, matching language
        global_rules = global_db.get_rules(min_confidence=0.6, language=language)
        for rule in global_rules:
            existing = project_db.conn.execute(
                "SELECT id FROM learned_rules WHERE rule_text = ?",
                (rule["rule_text"],),
            ).fetchone()
            if not existing:
                # Apply 0.8x confidence decay on import
                decayed_confidence = rule["confidence"] * 0.8
                project_db.add_learned_rule(
                    rule["rule_text"], decayed_confidence,
                    source_sessions=[], category=rule.get("category"),
                    file_pattern=None,
                )
                # Mark as globally sourced
                project_db.conn.execute(
                    "UPDATE learned_rules SET source = 'global' WHERE rule_text = ?",
                    (rule["rule_text"],),
                )
                project_db.conn.commit()
                stats["rules_imported"] += 1

        # Register this project in global DB
        project_root = get_project_root()
        project_name = project_root.name
        global_db.register_project(str(project_root), project_name, language or "unknown")

    finally:
        global_db.close()
        project_db.close()

    if stats["preferences_imported"] or stats["rules_imported"]:
        logger.info("Global sync imported: %d preferences, %d rules",
                     stats["preferences_imported"], stats["rules_imported"])

    return stats


def export_project_to_global() -> dict:
    """
    Export qualifying local intelligence to the global database.
    Called on session end (write_session_log). Returns summary of what was exported.
    """
    from mcp_server.paths import get_global_db_path, get_data_dir, get_project_root
    from indexer.global_db import GlobalDB
    from indexer.sqlite_graph import SQLiteGraph

    stats = {"preferences_exported": 0, "rules_exported": 0}

    project_db_path = get_data_dir() / "graph" / "graph.db"
    if not project_db_path.exists():
        return stats

    language = _get_project_language()
    project_root = str(get_project_root())

    global_db = GlobalDB(get_global_db_path())
    project_db = SQLiteGraph(project_db_path)

    try:
        # Export preferences with frequency >= 2
        local_prefs = project_db.get_preferences(min_frequency=2)
        for pref in local_prefs:
            global_db.upsert_preference(
                pref["category"], pref["signal"],
                example=pref.get("example"),
                source_project=project_root,
                frequency=pref.get("frequency", 1),
            )
            stats["preferences_exported"] += 1

        # Export rules with confidence >= 0.5
        local_rules = project_db.get_learned_rules(min_confidence=0.5)
        for rule in local_rules:
            global_db.upsert_rule(
                rule["rule_text"], rule["confidence"],
                source_project=project_root,
                category=rule.get("category"),
                language=language,
            )
            stats["rules_exported"] += 1

        # Update project sync timestamp
        global_db.register_project(project_root, Path(project_root).name, language or "unknown")

    finally:
        global_db.close()
        project_db.close()

    return stats


def get_global_stats() -> dict | None:
    """Return global database stats for display in get_session_context()."""
    from mcp_server.paths import get_global_db_path
    from indexer.global_db import GlobalDB

    global_db_path = get_global_db_path()
    if not global_db_path.exists():
        return None

    global_db = GlobalDB(global_db_path)
    try:
        return global_db.get_stats()
    finally:
        global_db.close()


def _get_project_language() -> str | None:
    """Read the project language from .codevira/config.yaml."""
    from mcp_server.paths import get_data_dir
    import yaml

    config_path = get_data_dir() / "config.yaml"
    if not config_path.exists():
        return None
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        project = config.get("project", config)
        return project.get("language")
    except Exception:
        return None
