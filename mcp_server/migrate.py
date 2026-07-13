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


def _codevira_version() -> str:
    try:
        from mcp_server import __version__

        return __version__
    except Exception:
        return "unknown"


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
    from mcp_server.paths import (
        _sanitize_path_key,
        _get_git_remote_url,
        get_global_home,
        get_global_db_path,
    )

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
    (centralized / "graph").mkdir(parents=True, exist_ok=True)
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
            logger.warning(
                "Could not backup graph.db via API, falling back to copy: %s", e
            )
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
        "version": _codevira_version(),
    }
    from mcp_server.storage.atomic import atomic_write_text

    atomic_write_text(centralized / "metadata.json", json.dumps(metadata, indent=2))

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

    # Invalidate the data-dir cache so get_data_dir() now returns the newly
    # populated centralized directory, not the old legacy path.
    try:
        from mcp_server.paths import invalidate_data_dir_cache

        invalidate_data_dir_cache(project_root)
    except Exception:
        pass  # Cache invalidation is best-effort; migration proceeds regardless

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
        cols = [
            row[1] for row in gdb.conn.execute("PRAGMA table_info(projects)").fetchall()
        ]
        if "git_remote" not in cols:
            gdb.conn.execute("ALTER TABLE projects ADD COLUMN git_remote TEXT")
            gdb.conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v3.7.0 (M1) — automatic, non-breaking DATA self-heal at server startup
# ---------------------------------------------------------------------------
#
# Users must never run a manual command after upgrading, even for a big
# upgrade. This migrator runs at server startup (and on `codevira init`) and is:
#
#   * idempotent   — a per-project ledger records which named migrations ran;
#                    each runs at most once.
#   * lock-safe    — a file lock guards the ledger, so two concurrent codevira
#                    processes (two IDE windows) can't double-apply.
#   * non-breaking — collision repair reuses decisions_store.repair_ids, which
#                    writes through the SAME exclusive-flock + atomic-replace
#                    that a normal append uses. A record() in another window is
#                    serialized against it; a concurrent read gets a consistent
#                    snapshot. No lost writes, no torn reads. (Verified: the
#                    write path locks — jsonl_store.append/rewrite_all.)
#   * non-destructive — decisions.jsonl is backed up before any rewrite.
#   * failure-isolated — a migration that raises is logged and left un-marked
#                    (retried next start); it never blocks server startup.
#
# It ONLY touches codevira-owned data files. IDE registration config is NOT
# migrated here — that file is written concurrently by the IDE and can't be
# lock-controlled, so a silent background rewrite could race and corrupt it.
# Registration is healed surgically by init/setup instead (M2).

_LEDGER_NAME = "migration_ledger.json"


def _ledger_path() -> Path:
    """Ledger lives beside decisions.jsonl (the per-project data dir)."""
    from mcp_server.storage import paths as store_paths

    return store_paths.decisions_path().parent / _LEDGER_NAME


def _load_ledger(path: Path) -> dict:
    if not path.is_file():
        return {"applied": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("applied"), list):
            return {"applied": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"applied": []}


def _save_ledger(path: Path, ledger: dict) -> None:
    from mcp_server.storage.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(ledger, indent=2, sort_keys=True))


def _mig_v370_repair_collisions(project_root: Path) -> bool:
    """Heal any PRE-3.7.0 base-id collisions (a shared repo whose decisions.jsonl
    already carries two records minted with the same id — one is silently
    shadowed on read until repaired).

    Backup-first, then reuse ``decisions_store.repair_ids(apply=True)`` —
    deterministic, idempotent, non-lossy, lock+atomic. Returns True if a repair
    was applied. New (post-upgrade) collisions are handled by the merge driver
    at merge time + the doctor check, not here — this is a one-time upgrade heal.
    """
    from mcp_server.storage import decisions_store
    from mcp_server.storage import paths as store_paths

    report = decisions_store.repair_ids(apply=False)
    if not report.get("changed"):
        return False  # clean store — nothing to heal

    src = store_paths.decisions_path()
    backup = src.with_name(src.name + ".bak-pre-v370")
    if src.is_file() and not backup.exists():
        shutil.copy2(src, backup)

    decisions_store.repair_ids(apply=True)
    return True


def _mig_v370_merge_driver(project_root: Path) -> bool:
    """Self-install the decision-log git merge driver so future cross-engineer
    id collisions resolve automatically on merge/rebase. Idempotent; no-op
    outside a git repo.
    """
    from mcp_server.cli_repair import install_merge_driver

    install_merge_driver(project_root)
    return True


def _mig_v370_dedupe_registration(project_root: Path) -> bool:
    """Remove a stale per-project `codevira` IDE entry left by a pre-3.7 init —
    but ONLY when a global codevira entry already exists (non-orphaning).

    This is the ONE registration action safe to do silently at startup: it
    touches only project-local config files (low-write, not the IDE's own
    heavily-written global state), edits are surgical + atomic, and it can never
    leave a user with no server. Creating the global entry is NOT done here —
    that write is handled by init/setup, where it isn't racing the IDE.
    """
    from mcp_server.ide_inject import heal_stale_registration

    heal_stale_registration(project_root, require_global=True)
    return True


# Ordered ledger of named startup migrations. APPEND new entries; never rename
# or renumber an existing one — the name is the idempotency key.
_STARTUP_MIGRATIONS = (
    ("v370_repair_collisions", _mig_v370_repair_collisions),
    ("v370_merge_driver", _mig_v370_merge_driver),
    ("v370_dedupe_registration", _mig_v370_dedupe_registration),
)


def run_startup_migrations(project_root: Path | None = None) -> dict:
    """Run every not-yet-applied startup migration. Automatic + non-breaking.

    Called at server startup and by ``codevira init``. Ledger-gated (each named
    migration runs at most once per project) and lock-protected (concurrent
    processes can't race). A migration that raises is logged and left un-marked
    so it retries next start; it NEVER blocks startup.

    Returns ``{"applied": [names run this call], "ledger": [all applied names]}``.
    """
    from mcp_server.storage.atomic import file_lock

    if project_root is None:
        try:
            from mcp_server.paths import get_project_root

            project_root = get_project_root()
        except Exception as e:
            logger.warning("run_startup_migrations: no project root (%s)", e)
            return {"applied": [], "ledger": []}

    applied_now: list[str] = []
    try:
        ledger_path = _ledger_path()
        lock_path = ledger_path.parent / (_LEDGER_NAME + ".lock")
        with file_lock(lock_path, exclusive=True):
            ledger = _load_ledger(ledger_path)
            done = set(ledger.get("applied", []))
            for name, fn in _STARTUP_MIGRATIONS:
                if name in done:
                    continue
                try:
                    fn(project_root)
                except Exception as e:  # failure-isolated: retry next boot
                    logger.warning(
                        "startup migration %s failed (will retry next start): %s",
                        name,
                        e,
                    )
                    continue
                ledger.setdefault("applied", []).append(name)
                applied_now.append(name)
            if applied_now:
                _save_ledger(ledger_path, ledger)
            return {"applied": applied_now, "ledger": ledger.get("applied", [])}
    except Exception as e:
        # A lock/ledger failure must never crash startup.
        logger.warning("run_startup_migrations skipped (non-fatal): %s", e)
        return {"applied": applied_now, "ledger": []}
