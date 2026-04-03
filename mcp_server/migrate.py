"""
migrate.py — Legacy → Centralized storage migration for Codevira v1.6.

Migrates per-project .codevira/ directories into ~/.codevira/projects/<key>/
so that data lives centrally and no longer pollutes the project tree.

Key behaviors:
- Idempotent: safe to call multiple times; second call is a no-op.
- Non-destructive: renames old .codevira/ to .codevira.migrated/ (not deleted).
- SQLite-safe: uses sqlite3.Connection.backup() to copy WAL-mode databases.
- Partial-migration recovery: if metadata.json is missing from centralized dir,
  migration is re-run from scratch.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CODEVIRA_VERSION = "1.6.0"


def detect_migration_needed(project_root: Path) -> bool:
    """Return True if the project has a legacy .codevira/ that needs migration.

    Conditions:
      - <project_root>/.codevira/config.yaml exists (legacy initialized project)
      - No corresponding centralized dir with metadata.json exists yet
    """
    from mcp_server.paths import _sanitize_path_key, get_global_home

    legacy = project_root / ".codevira"
    if not (legacy / "config.yaml").is_file():
        return False

    key = _sanitize_path_key(project_root)
    centralized = get_global_home() / "projects" / key
    # Migration complete only if metadata.json is present
    if (centralized / "metadata.json").is_file():
        return False

    return True


def migrate_to_centralized(project_root: Path) -> dict:
    """Migrate <project_root>/.codevira/ to ~/.codevira/projects/<key>/.

    Returns a summary dict:
      {migrated: True, files_copied: N, old_path: str, new_path: str}
    or
      {migrated: False, reason: str}

    Migration steps:
      1. Create centralized directory structure
      2. Copy config.yaml and roadmap.yaml
      3. Copy graph.db via sqlite3 backup API (WAL-safe)
      4. Copy codeindex/ directory (ChromaDB persistent storage)
      5. Write metadata.json
      6. Update global.db project registry
      7. Rename old .codevira/ → .codevira.migrated/ (safety net)
    """
    from mcp_server.paths import _sanitize_path_key, _get_git_remote_url, get_global_home, get_global_db_path

    legacy = project_root / ".codevira"
    if not (legacy / "config.yaml").is_file():
        return {"migrated": False, "reason": "No legacy .codevira/config.yaml found"}

    key = _sanitize_path_key(project_root)
    centralized = get_global_home() / "projects" / key

    # Already migrated?
    if (centralized / "metadata.json").is_file():
        return {"migrated": False, "reason": "Already migrated"}

    logger.info("Migrating %s → %s", legacy, centralized)

    # Create directory structure
    (centralized / "graph" / "changesets").mkdir(parents=True, exist_ok=True)
    (centralized / "codeindex").mkdir(parents=True, exist_ok=True)
    (centralized / "logs").mkdir(parents=True, exist_ok=True)

    files_copied = 0

    # 1. Copy config.yaml
    src_config = legacy / "config.yaml"
    dst_config = centralized / "config.yaml"
    shutil.copy2(src_config, dst_config)
    files_copied += 1

    # 2. Copy roadmap.yaml (may not exist)
    src_roadmap = legacy / "roadmap.yaml"
    if src_roadmap.exists():
        shutil.copy2(src_roadmap, centralized / "roadmap.yaml")
        files_copied += 1

    # 3. Copy graph.db via sqlite3 backup API (safe under WAL mode)
    src_db = legacy / "graph" / "graph.db"
    dst_db = centralized / "graph" / "graph.db"
    if src_db.exists():
        src_conn = None
        dst_conn = None
        try:
            src_conn = sqlite3.connect(str(src_db))
            dst_conn = sqlite3.connect(str(dst_db))
            src_conn.backup(dst_conn)
            files_copied += 1
        except Exception as e:
            logger.warning("Could not backup graph.db via API, falling back to copy: %s", e)
            # Fallback: copy main db + WAL/SHM files if present
            shutil.copy2(src_db, dst_db)
            for suffix in ("-wal", "-shm"):
                wal_file = src_db.parent / (src_db.name + suffix)
                if wal_file.exists():
                    shutil.copy2(wal_file, dst_db.parent / (dst_db.name + suffix))
            files_copied += 1
        finally:
            if src_conn:
                src_conn.close()
            if dst_conn:
                dst_conn.close()

    # 4. Copy codeindex/ (ChromaDB directory)
    src_index = legacy / "codeindex"
    dst_index = centralized / "codeindex"
    if src_index.exists() and src_index.is_dir():
        if dst_index.exists():
            shutil.rmtree(dst_index)
        shutil.copytree(src_index, dst_index)
        files_copied += 1

    # 5. Write metadata.json
    git_remote = _get_git_remote_url(project_root)
    metadata = {
        "path_key": key,
        "git_remote": git_remote,
        "original_path": str(project_root),
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "version": _CODEVIRA_VERSION,
    }
    (centralized / "metadata.json").write_text(json.dumps(metadata, indent=2))

    # 6. Update global.db project registry
    gdb = None
    try:
        from indexer.global_db import GlobalDB
        gdb = GlobalDB(get_global_db_path())
        _ensure_git_remote_column(gdb)
        gdb.register_project(
            path=str(centralized),
            name=project_root.name,
            language="unknown",
            git_remote=git_remote,
        )
    except Exception as e:
        logger.warning("Could not update global.db during migration: %s", e)
    finally:
        if gdb is not None:
            gdb.close()

    # 7. Rename old .codevira/ → .codevira.migrated/ (safety net, not deleted)
    migrated_backup = project_root / ".codevira.migrated"
    if migrated_backup.exists():
        shutil.rmtree(migrated_backup)
    legacy.rename(migrated_backup)

    logger.info(
        "Migration complete: %d files copied. Legacy dir renamed to %s",
        files_copied,
        migrated_backup,
    )

    return {
        "migrated": True,
        "files_copied": files_copied,
        "old_path": str(legacy),
        "new_path": str(centralized),
    }


def cleanup_legacy_dir(project_root: Path) -> bool:
    """Remove the .codevira.migrated/ safety-net directory after confirmation.

    Returns True if the directory was removed, False if it didn't exist.
    Call only after verifying the migration was successful.
    """
    backup = project_root / ".codevira.migrated"
    if backup.exists():
        shutil.rmtree(backup)
        logger.info("Removed legacy backup dir: %s", backup)
        return True
    return False


def _ensure_git_remote_column(gdb) -> None:
    """Add git_remote column to global_db.projects if not present (v1.6 schema upgrade)."""
    try:
        cols = [row[1] for row in gdb.conn.execute("PRAGMA table_info(projects)").fetchall()]
        if "git_remote" not in cols:
            gdb.conn.execute("ALTER TABLE projects ADD COLUMN git_remote TEXT")
            gdb.conn.commit()
    except Exception:
        pass
