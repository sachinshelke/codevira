"""
auto_init.py — Auto-initialization on first tool call for Codevira v1.6.

When an AI tool calls any Codevira MCP tool and the project has not been
initialized yet, this module:
  1. Detects the project from cwd
  2. Creates the centralized data directory (~/.codevira/projects/<key>/)
  3. Writes a minimal config.yaml
  4. Starts background indexing (graph generation + optional semantic index)
  5. Returns partial/minimal results while indexing progresses

Tools check ensure_project_initialized() before dispatching. If already
initialized, the call is a no-op (< 1ms overhead via a flag).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level state — initialized once per process
_init_lock = threading.Lock()
# True once the init thread has been *started* (not necessarily finished).
# Named _init_started to be precise — use _progress["status"] to know if
# initialization has *completed*.
_init_started: bool = False
_indexing_thread: threading.Thread | None = None
_progress: dict = {
    "status": "not_started",  # not_started | initializing | indexing | ready | error
    "files_indexed": 0,
    "total_files": 0,
    "elapsed_seconds": 0.0,
    "error": None,
}
_progress_lock = threading.Lock()
_start_time: float | None = None


@dataclass
class InitStatus:
    """Result of ensure_project_initialized()."""

    ready: bool  # True if the project was already initialized
    indexing: bool  # True if background indexing is running now
    files_indexed: int = 0
    total_files: int = 0


def get_init_progress() -> dict:
    """Return current indexing progress. Thread-safe."""
    with _progress_lock:
        prog = dict(_progress)
    if _start_time is not None:
        prog["elapsed_seconds"] = round(time.monotonic() - _start_time, 1)
    return prog


def ensure_project_initialized(project_root: Path | None = None) -> InitStatus:
    """Check if the project is initialized; auto-init in background if not.

    This is a fast path: if already done, returns immediately (<1ms).
    Only the first call that finds an uninitialized project triggers init.

    Args:
        project_root: Override for testing. Uses get_project_root() by default.

    Returns:
        InitStatus with ready/indexing flags and progress counts.
    """
    global _init_started, _indexing_thread, _start_time

    # Fast path — init thread already started this process
    if _init_started:
        with _progress_lock:
            status = _progress["status"]
            files_indexed = _progress["files_indexed"]
            total_files = _progress["total_files"]
        return InitStatus(
            ready=(status == "ready"),
            indexing=(status == "indexing"),
            files_indexed=files_indexed,
            total_files=total_files,
        )

    with _init_lock:
        # Double-checked locking
        if _init_started:
            with _progress_lock:
                return InitStatus(
                    ready=(_progress["status"] == "ready"),
                    indexing=(_progress["status"] == "indexing"),
                    files_indexed=_progress["files_indexed"],
                    total_files=_progress["total_files"],
                )

        from mcp_server.paths import (
            get_project_root,
            get_data_dir,
            is_invalid_project_root,
        )

        root = project_root or get_project_root()
        data_dir = get_data_dir()

        # Bug 21a (rc.4): self-heal the cheap pieces (config.yaml +
        # metadata.json + global.db.projects row) SYNCHRONOUSLY before
        # deciding whether heavy background init is needed. Pre-fix the
        # whole init ran on a daemon thread; if the MCP server exited
        # before that thread finished (a brief tool call from a
        # short-lived AI session), the data dir was left "ghost"
        # — present on disk, absent from inventory.
        #
        # Refuse system top-levels FIRST (same v1.8.1 guard as the heavy
        # path uses) — we don't want to repair-bootstrap $HOME etc.
        rejection = is_invalid_project_root(root)
        if not rejection:
            try:
                from mcp_server._repair_init import repair_incomplete_init

                repair_incomplete_init(data_dir, root)
            except Exception as e:
                logger.warning("Bug 21a self-heal skipped: %s", e)

        # After self-heal, config.yaml + metadata.json + global.db row are
        # in place (assuming detection didn't fail). The remaining question
        # is whether HEAVY init (graph + semantic index) is also done.
        # P0-B (rc.5): "graph.db exists" isn't enough — an empty graph file
        # (from a prior interrupted init or sqlite-bootstrap-only path) means
        # tools like get_node and query_graph will still return found=false.
        # Treat empty graph as "needs heavy build" so the auto-trigger fires.
        graph_db = data_dir / "graph" / "graph.db"
        graph_has_nodes = False
        if graph_db.is_file():
            try:
                import sqlite3 as _sqlite3

                _conn = _sqlite3.connect(str(graph_db))
                try:
                    row = _conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
                    graph_has_nodes = bool(row and row[0] > 0)
                except Exception:
                    # nodes table doesn't exist yet — needs build.
                    graph_has_nodes = False
                finally:
                    _conn.close()
            except Exception:
                graph_has_nodes = False

        if (data_dir / "config.yaml").is_file() and graph_has_nodes:
            _init_started = True
            with _progress_lock:
                _progress["status"] = "ready"
            return InitStatus(ready=True, indexing=False)

        # Heavy init still needed (no graph.db yet). Start background thread.
        logger.info("Project not initialized. Starting auto-init for %s", root)
        _start_time = time.monotonic()
        with _progress_lock:
            _progress["status"] = "initializing"

        _indexing_thread = threading.Thread(
            target=_run_background_init,
            args=(root, data_dir),
            daemon=True,
            name="codevira-auto-init",
        )
        _indexing_thread.start()
        _init_started = True  # Prevent duplicate init threads — thread completion
        # is tracked via _progress["status"], not this flag.

        return InitStatus(ready=False, indexing=True)


def _run_background_init(project_root: Path, data_dir: Path) -> None:
    """Background thread: detect project, write config, build graph, index files."""
    global _start_time

    try:
        # v1.8.1: refuse $HOME and system top-levels as a project root.
        # An unguarded auto-init triggered by an MCP tool call from $HOME
        # is exactly how the rogue project (`original_path: /Users/sachin`)
        # in the v1.8.0 production crash log got created. Set status=error
        # and return early — the MCP server's fast path checks the flag and
        # won't loop on retries.
        from mcp_server.paths import is_invalid_project_root

        rejection = is_invalid_project_root(project_root)
        if rejection:
            logger.warning("Auto-init refused: %s", rejection)
            _update_progress(status="error", error=rejection)
            return

        _update_progress(status="initializing")

        # Step 1: Auto-detect project settings
        from mcp_server.detect import auto_detect_project

        detected = auto_detect_project(project_root)

        # Step 2: Create data directory structure
        (data_dir / "graph").mkdir(parents=True, exist_ok=True)
        (data_dir / "codeindex").mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Step 3: Write config.yaml
        _write_config(data_dir, detected, project_root)

        # Step 4: Write metadata.json (centralized storage marker)
        _write_metadata(data_dir, project_root)

        # Invalidate the data-dir cache so get_data_dir() now returns the newly
        # created centralized directory instead of the pre-init default path.
        from mcp_server.paths import invalidate_data_dir_cache

        invalidate_data_dir_cache(project_root)

        # Step 5: Register in global.db
        _register_global(data_dir, project_root, detected)

        # Step 6: Generate graph (fast — no ML deps required)
        _update_progress(status="indexing")
        try:
            from indexer.graph_generator import generate_graph_sqlite

            generate_graph_sqlite(
                str(project_root), str(data_dir / "graph" / "graph.db")
            )
            logger.info("Auto-init: graph generated for %s", project_root)
        except Exception as e:
            logger.warning("Auto-init: graph generation failed: %s", e)

        # Step 7: Count source files for progress tracking
        try:
            from mcp_server.gitignore import discover_source_files

            files = discover_source_files(project_root)
            _update_progress(total_files=len(files))
        except Exception:
            files = []

        # Step 8: Build semantic search index (optional — requires [search] extras)
        # Uses start_background_full_index() which holds _chroma_write_lock to prevent
        # race conditions with the file watcher.
        try:
            from indexer.index_codebase import start_background_full_index

            _update_progress(status="indexing")
            idx_thread = start_background_full_index()
            # Wait up to 5 minutes; if ChromaDB or embedding model hangs we still
            # surface "ready" so tool calls aren't blocked indefinitely.
            idx_thread.join(timeout=300)
            if idx_thread.is_alive():
                logger.warning(
                    "Auto-init: semantic indexing timed out after 5 min; "
                    "continuing in graph-only mode"
                )
            _update_progress(files_indexed=len(files), status="ready")
        except ImportError:
            # ChromaDB not installed — graph-only mode is fine
            _update_progress(files_indexed=0, status="ready")
        except Exception as e:
            logger.warning("Auto-init: semantic indexing failed (non-fatal): %s", e)
            _update_progress(status="ready")

        logger.info(
            "Auto-init complete for %s (%.1fs)",
            project_root,
            time.monotonic() - (_start_time or 0),
        )

    except Exception as e:
        logger.error("Auto-init failed: %s", e)
        _update_progress(status="error", error=str(e))


def _update_progress(**kwargs) -> None:
    with _progress_lock:
        _progress.update(kwargs)


def _write_config(data_dir: Path, detected: dict, project_root: Path) -> None:
    """Write .codevira/config.yaml (or centralized equivalent)."""
    import yaml

    config = {
        "project": {
            "name": detected["name"],
            "language": detected["language"],
            "watched_dirs": detected["watched_dirs"],
            "file_extensions": detected["file_extensions"],
            "collection_name": detected["collection_name"],
        }
    }
    with open(data_dir / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _codevira_version() -> str:
    try:
        from mcp_server import __version__

        return __version__
    except Exception:
        return "unknown"


def _write_metadata(data_dir: Path, project_root: Path) -> None:
    """Write metadata.json so the centralized dir is recognized."""
    import json
    from datetime import datetime, timezone
    from mcp_server.paths import _sanitize_path_key, _get_git_remote_url

    metadata = {
        "path_key": _sanitize_path_key(project_root),
        "git_remote": _get_git_remote_url(project_root),
        "original_path": str(project_root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": _codevira_version(),
        "auto_initialized": True,
    }
    (data_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


def _register_global(data_dir: Path, project_root: Path, detected: dict) -> None:
    """Register the project in global.db for cross-project intelligence.

    Bug 20 (rc.4): registers under ``project_root`` (the canonical project
    path), not ``data_dir`` (the ``~/.codevira/projects/<slug>`` storage path).
    Pre-fix, cli.py + auto_init.py both passed ``data_dir`` while
    global_sync.py passed ``project_root`` — same logical project ended up
    with two rows in ``global.db.projects`` keyed differently. The
    one-shot dedup migration in :func:`GlobalDB.dedupe_by_git_remote` collapses
    legacy duplicates on next startup.
    """
    try:
        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_db_path, _get_git_remote_url

        gdb = GlobalDB(get_global_db_path())
        gdb.register_project(
            path=str(project_root),
            name=detected["name"],
            language=detected["language"],
            git_remote=_get_git_remote_url(project_root),
        )
        gdb.close()
    except Exception as e:
        logger.warning("Auto-init: could not register in global.db: %s", e)
