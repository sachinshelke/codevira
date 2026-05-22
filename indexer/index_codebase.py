from __future__ import annotations

import argparse
import logging
import sys
import time
import hashlib
import threading
from pathlib import Path

from mcp_server.paths import get_data_dir, get_project_root
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)

COLLECTION_NAME = "codebase_index"

# Global lock — prevents the background watcher and background full-index from
# writing to ChromaDB simultaneously. Both operations must acquire this lock
# before any ChromaDB write (add/delete/recreate collection).
_chroma_write_lock = threading.Lock()

# Atomic counters for background indexing progress
_bg_files_indexed: int = 0
_bg_total_files: int = 0
_bg_status: str = "idle"  # idle | running | done | error
_bg_lock = threading.Lock()


# ---------------------------------------------------------------------
# Watcher circuit breaker (Pillar 3.2 of v2.0 master plan)
# ---------------------------------------------------------------------
#
# Until v2.0-rc.1 the background watcher's debounce-triggered reindex
# would log + continue on failure — but with no rate-limiting, a
# persistently-failing reindex (e.g., disk full, permission lost mid-run)
# would burn CPU and crash log space on every file change.
#
# Now: track consecutive failures; open the circuit after a threshold;
# back off exponentially before letting another reindex through. Closed
# circuit + successful reindex resets the counter.

_CIRCUIT_OPEN_THRESHOLD = 3  # consecutive failures before opening
_CIRCUIT_BACKOFF_INITIAL = 60.0  # 1 minute
_CIRCUIT_BACKOFF_CAP = 1800.0  # 30 minutes

# Module-level state — bounded; never grows.
_watcher_circuit_lock = threading.Lock()
_watcher_circuit_failures: int = 0
_watcher_circuit_next_retry_at: float = 0.0
_watcher_circuit_last_error: str = ""


def watcher_circuit_status() -> dict:
    """Snapshot of the circuit breaker state for ``codevira doctor`` /
    diagnostics. Always returns a dict; never raises."""
    with _watcher_circuit_lock:
        now = time.time()
        is_open = (
            _watcher_circuit_failures >= _CIRCUIT_OPEN_THRESHOLD
            and now < _watcher_circuit_next_retry_at
        )
        return {
            "open": is_open,
            "consecutive_failures": _watcher_circuit_failures,
            "seconds_until_retry": max(
                0.0,
                _watcher_circuit_next_retry_at - now,
            )
            if is_open
            else 0.0,
            "last_error": _watcher_circuit_last_error,
        }


def _watcher_circuit_should_run() -> bool:
    """Return True if the circuit is closed (or half-open)."""
    with _watcher_circuit_lock:
        if _watcher_circuit_failures < _CIRCUIT_OPEN_THRESHOLD:
            return True
        return time.time() >= _watcher_circuit_next_retry_at


def _watcher_circuit_record_success() -> None:
    """A successful reindex resets the circuit."""
    global \
        _watcher_circuit_failures, \
        _watcher_circuit_next_retry_at, \
        _watcher_circuit_last_error
    with _watcher_circuit_lock:
        _watcher_circuit_failures = 0
        _watcher_circuit_next_retry_at = 0.0
        _watcher_circuit_last_error = ""


def _watcher_circuit_record_failure(err: BaseException) -> None:
    """Increment failure count + compute backoff."""
    global \
        _watcher_circuit_failures, \
        _watcher_circuit_next_retry_at, \
        _watcher_circuit_last_error
    with _watcher_circuit_lock:
        _watcher_circuit_failures += 1
        _watcher_circuit_last_error = f"{type(err).__name__}: {err}"
        if _watcher_circuit_failures >= _CIRCUIT_OPEN_THRESHOLD:
            # Geometric backoff: 60s, 120s, 240s, … capped at 30 min.
            extra = _watcher_circuit_failures - _CIRCUIT_OPEN_THRESHOLD
            backoff = min(
                _CIRCUIT_BACKOFF_INITIAL * (2**extra),
                _CIRCUIT_BACKOFF_CAP,
            )
            _watcher_circuit_next_retry_at = time.time() + backoff


def reset_watcher_circuit() -> None:
    """Tests only; production never calls this."""
    _watcher_circuit_record_success()


def _project_root() -> Path:
    return get_project_root()


def _index_dir() -> Path:
    # get_data_dir() is cached in paths.py (_data_dir_cache), so this is fast.
    return get_data_dir() / "codeindex"


def _load_config() -> dict:
    """Load .codevira/config.yaml and return the 'project' sub-dict."""
    config_path = get_data_dir() / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            # config.yaml nests settings under 'project' key
            return raw.get("project", raw)
        except Exception:
            pass
    return {}


def _check_search_deps() -> bool:
    """v2.2.0: ALWAYS False.

    chromadb / sentence-transformers / torch were deleted in v2.2.0
    along with semantic code search (search_codebase). All callers of
    this function already have a graceful-degradation path for the
    False return (graph-only generation); we just lock in that path.
    """
    return False


# 2026-05-17 P2 self-heal: ChromaCorrupted is raised by _get_chroma_client
# (when probe=True) if the on-disk store fails health check. Callers can
# distinguish "Chroma never indexed" (skip semantic) from "Chroma broken"
# (warn user + offer heal) instead of getting cryptic InternalError mid-loop
# the way the 41-crash UDAP pattern surfaced.
class ChromaCorrupted(RuntimeError):
    """Raised when ChromaDB's on-disk store is unreadable / corrupt.

    The user-visible message (via str(exc)) MUST include a fix_command
    (P8 helpful errors). Currently: ``codevira heal --vectors`` (planned),
    or as a manual fallback: ``rm -rf <chroma_dir> && codevira index --full``.
    """


# 2026-05-17: shared signature predicate. Single source of truth (P6) for
# what counts as "this is Chroma corruption" — used by both _check_chroma_health
# (boundary probe) and the cmd_incremental circuit breaker (per-file errors).
_CHROMA_CORRUPTION_HINTS = (
    "hnsw segment writer",
    "Failed to apply logs",
    "backfill request to compactor",
    "compaction",
    "database disk image is malformed",
)


def _looks_like_chroma_corruption(exc: BaseException) -> bool:
    """Return True if exc matches a known Chroma corruption signature.

    Used by the incremental indexer's circuit breaker to distinguish
    "transient blip — log + continue" from "store is corrupted — halt
    the loop and surface the error" (the UDAP 41-crash pattern).
    """
    msg = str(exc).lower()
    return any(hint.lower() in msg for hint in _CHROMA_CORRUPTION_HINTS)


def _check_chroma_health(client) -> None:
    """Probe the Chroma client for a known-broken state.

    The probe is a no-op list_collections call. If the underlying HNSW
    store / log files are corrupt (the UDAP 2026-05-14 failure mode),
    chromadb raises InternalError here BEFORE the indexer hits the
    delete/add loop that produced 41 cascading crashes.

    Raises:
        ChromaCorrupted: if the probe fails.
    """
    try:
        # Cheap probe — just lists collections, no real work.
        client.list_collections()
    except Exception as exc:
        # Use the shared signature predicate (P6 single source of truth).
        if _looks_like_chroma_corruption(exc):
            raise ChromaCorrupted(
                f"ChromaDB store appears corrupted (HNSW writer error): {exc}. "
                f"Fix: run `codevira heal --vectors`, OR manually: "
                f"rm -rf <project_data_dir>/codeindex && codevira index --full"
            ) from exc
        # Not a known corruption pattern — re-raise unchanged so the caller
        # sees the original error.
        raise


def _get_chroma_client(*, probe: bool = False):
    """Get a Chroma client; optionally probe for corruption.

    Args:
        probe: if True, call _check_chroma_health() after client init.
               Default False to keep the common path cheap; set True at
               server startup or before write-heavy operations.

    Raises:
        ImportError: chromadb not installed.
        ChromaCorrupted: probe=True AND the on-disk store fails health check.
    """
    try:
        import chromadb
    except ImportError:
        # chromadb is in the base install (v1.7.0+). If missing, the user
        # likely did `pip install --no-deps` for a minimal install.
        raise ImportError(
            "Semantic search requires chromadb. "
            "Reinstall codevira (chromadb is included in the default install): "
            "pip install --upgrade codevira"
        )
    db_dir = str(_index_dir())
    client = chromadb.PersistentClient(path=db_dir)
    if probe:
        _check_chroma_health(client)
    return client


def _get_embedding_fn():
    # chromadb's SentenceTransformerEmbeddingFunction raises ValueError
    # (not ImportError) when sentence_transformers isn't installed.
    # We catch both and re-raise as ImportError for consistent handling.
    try:
        from chromadb.utils import embedding_functions

        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    except (ImportError, ValueError) as e:
        raise ImportError(
            f"Semantic search requires sentence-transformers. "
            f"Reinstall codevira: pip install --upgrade codevira. Details: {e}"
        )


def _compute_hash(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _warn_zero_chunks(
    watched_dirs: list[str],
    file_extensions: list[str],
    quiet: bool = False,
) -> None:
    """Emit the zero-chunks safety hint to the module logger + stderr.

    **Output goes to stderr, never stdout.** ``cmd_full_rebuild`` is called
    from ``start_background_full_index()`` during auto_init, which runs
    inside the MCP server process. The MCP stdio transport uses
    ``sys.stdout.buffer`` for JSON-RPC — any stdout write from a background
    thread would corrupt the protocol. Using stderr keeps the hint visible
    to humans (CLI, logs) without risking client breakage in stdio mode.

    Single source of truth for the hint text — shared by ``cmd_full_rebuild``
    (when the filtered chunk set is empty) and ``cmd_incremental`` (when a
    project-wide scan finds no files matching the config at all). The logger
    side always fires so background invocations leave a trace; stderr is
    suppressed when ``quiet=True``.
    """
    logger.warning(
        "No files matched your watched_dirs/file_extensions. "
        "watched_dirs=%s file_extensions=%s. Run `codevira configure`.",
        list(watched_dirs),
        list(file_extensions),
    )
    if quiet:
        return
    from rich.console import Console

    c = Console(stderr=True)
    c.print("[yellow]⚠[/yellow]  No files matched your watched_dirs/file_extensions.")
    c.print(f"  watched_dirs:    {list(watched_dirs)}")
    c.print(f"  file_extensions: {list(file_extensions)}")
    c.print(
        "  Run [bold]codevira configure[/bold] to scan your project and pick the right folders."
    )


def _any_files_match(
    watched_dirs: list[str],
    file_extensions: list[str],
    skip_dirs: list[str],
) -> bool:
    """Return True if ≥1 file under watched_dirs has a matching extension.

    2026-05-17 Bug A fix (P6 predictable detection): now delegates to
    ``discover_source_files`` — the same scanner ``configure`` uses.
    Previously this function used a raw ``rglob`` walker that didn't
    respect .gitignore, so configure could find 8 files but
    ``_any_files_match`` could return False, producing the silent
    "0 chunks matched" pattern. Single source of truth now.

    Short-circuits on the first match — cheap even on large trees.
    """
    try:
        from mcp_server.gitignore import discover_source_files

        files = discover_source_files(
            _project_root(),
            config_overrides={
                "watched_dirs": watched_dirs,
                "file_extensions": file_extensions,
                "skip_dirs": skip_dirs,
            },
        )
        return len(files) > 0
    except Exception as exc:
        # P9 graceful degradation: fall back to the old rglob walker so
        # an import error in mcp_server.gitignore doesn't kill the whole
        # indexer. Logged as warning so the issue is visible.
        logger.warning(
            "_any_files_match: discover_source_files failed (%s) — falling back to rglob walker",
            exc,
        )
        root = _project_root()
        for wd in watched_dirs:
            base = root / wd
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix not in file_extensions:
                    continue
                if any(s in p.parts for s in skip_dirs):
                    continue
                return True
        return False


def _get_changed_files(db: SQLiteGraph) -> list[tuple[str, str]]:
    changed = []
    seen_paths = set()
    config = _load_config()
    watched_dirs = config.get("watched_dirs", ["src"])
    extensions = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
    skip_dirs = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])

    for watch_dir in watched_dirs:
        watch_path = _project_root() / watch_dir
        if not watch_path.exists():
            continue

        # v1.8.1: per-watch-dir try/except so transient OS errors (EINTR,
        # PermissionError, "directory changed during iteration") don't
        # take down the whole reindex. Per-watch-dir scope matches
        # watchdog.Observer's thread-per-watch model — each parallel
        # watcher thread recovers independently. See crash-log analysis
        # 2026-04-24: 41 InterruptedError crashes in this exact loop.
        try:
            for p in watch_path.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix not in extensions:
                    continue
                if any(skip in p.parts for skip in skip_dirs):
                    continue

                try:
                    rel_path = str(p.relative_to(_project_root()))
                    if rel_path in seen_paths:
                        continue
                    seen_paths.add(rel_path)
                    current_hash = _compute_hash(p)
                    stored_hash = db.get_file_hash(rel_path)

                    if current_hash != stored_hash:
                        changed.append((rel_path, current_hash))
                except Exception as e:
                    from mcp_server._safe_crash import safe_log_crash

                    safe_log_crash(e, context="get_changed_files: hash check")
        except (OSError, RuntimeError) as e:
            # OSError covers InterruptedError (EINTR), PermissionError, etc.
            # RuntimeError covers "directory changed during iteration".
            logger.warning(
                "Skipping watch_dir %s due to filesystem error: %s",
                watch_dir,
                e,
            )
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(
                e, context=f"_get_changed_files: walk of {watch_dir} aborted"
            )
            continue

    return changed


def _get_requested_files(file_paths: list[str]) -> list[tuple[str, str]]:
    requested = []
    seen_paths = set()
    config = _load_config()
    extensions = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
    skip_dirs = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])

    for raw_path in file_paths:
        candidate = Path(raw_path)
        abs_path = candidate if candidate.is_absolute() else _project_root() / candidate

        try:
            rel_path = str(abs_path.resolve().relative_to(_project_root()))
        except ValueError:
            continue

        if rel_path in seen_paths:
            continue
        if not abs_path.exists() or not abs_path.is_file():
            continue
        if abs_path.suffix not in extensions:
            continue
        if any(skip in abs_path.parts for skip in skip_dirs):
            continue

        seen_paths.add(rel_path)
        requested.append((rel_path, _compute_hash(abs_path)))

    return requested


def _chunk_to_document(chunk) -> tuple[str, str, dict]:
    doc_id = f"{chunk.file_path}::{chunk.chunk_type}::{chunk.name}::{chunk.start_line}"
    document = (
        f"{chunk.file_path} — {chunk.name}\n{chunk.docstring}\n\n{chunk.source_text}"
    )
    metadata = {
        "file_path": chunk.file_path,
        "name": chunk.name,
        "chunk_type": chunk.chunk_type,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "layer": chunk.layer,
    }
    return doc_id, document, metadata


def cmd_full_rebuild(verbose: bool = False):
    """Full rebuild from scratch.

    Args:
        verbose: emit per-file decisions (matched / skipped + reason) for
                 diagnosing silent 0-chunk results. (Bug H, 2026-05-17, P10.)
    """
    from indexer.chunker import chunk_project
    from indexer.graph_generator import generate_graph_sqlite
    from rich.console import Console
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
    )

    console = Console()
    _index_dir().mkdir(parents=True, exist_ok=True)
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")

    if not _check_search_deps():
        console.print(
            "[yellow]⚠[/yellow]  Semantic search unavailable — reinstall codevira: [bold]pip install --upgrade codevira[/bold]"
        )
        # Still build the graph even without search deps
        from indexer.graph_generator import generate_graph_sqlite

        result = generate_graph_sqlite(
            str(_project_root()), str(get_data_dir() / "graph" / "graph.db")
        )
        console.print(
            f"[green]✓[/green] Graph built: {result.get('nodes_added', 0)} nodes, {result.get('edges_added', 0)} edges."
        )
        db.close()
        return

    # P2 (self-diagnose on startup) + P5 (circuit-break before retry storm):
    # probe Chroma BEFORE the indexer touches the collection. If the store
    # is corrupted (the 2026-05-14 UDAP HNSW pattern), this raises
    # ChromaCorrupted with a clear remediation hint INSTEAD of hitting the
    # delete/add loop that produced 41 cascading crashes in production.
    try:
        client = _get_chroma_client(probe=True)
    except ChromaCorrupted as exc:
        console.print(f"[red]✗[/red] {exc}")
        # P9 (graceful degradation): graph indexing still works without
        # Chroma — fall through to the graph-only path that already exists.
        from indexer.graph_generator import generate_graph_sqlite

        result = generate_graph_sqlite(
            str(_project_root()), str(get_data_dir() / "graph" / "graph.db")
        )
        console.print(
            f"[yellow]⚠[/yellow] Skipped semantic index (corrupted). "
            f"Graph built: {result.get('nodes_added', 0)} nodes, "
            f"{result.get('edges_added', 0)} edges."
        )
        db.close()
        return
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    embed_fn = _get_embedding_fn()
    collection = client.create_collection(
        name=COLLECTION_NAME, embedding_function=embed_fn
    )

    config = _load_config()
    watched_dirs = config.get("watched_dirs", ["src"])
    extensions = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
    skip_dirs = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])

    all_chunks = []
    file_hashes = {}
    seen_chunk_ids = set()

    # 2026-05-17 Bug H fix (P10 observability): track per-file decisions
    # when verbose is on. Counters here surface as a summary even when
    # 0 chunks were matched — which is the failure mode the user hits
    # when discovery and indexing disagree on what counts as a source file.
    v_matched: int = 0
    v_skipped_extension: int = 0
    v_skipped_in_skip_dirs: int = 0
    v_skipped_other: int = 0
    if verbose:
        console.print(f"[dim cyan][verbose][/dim cyan] watched_dirs={watched_dirs}")
        console.print(f"[dim cyan][verbose][/dim cyan] file_extensions={extensions}")
        console.print(f"[dim cyan][verbose][/dim cyan] skip_dirs={skip_dirs}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task1 = progress.add_task(
            "[cyan]Parsing and chunking source files...", total=None
        )

        # Chunk the entire project once (not per watched_dir)
        all_project_chunks = chunk_project(str(_project_root()))
        if verbose:
            console.print(
                f"[dim cyan][verbose][/dim cyan] chunk_project produced "
                f"{len(all_project_chunks)} total chunk(s) across all files"
            )

        for watch_dir in watched_dirs:
            abs_dir = _project_root() / watch_dir
            if not abs_dir.exists():
                if verbose:
                    console.print(
                        f"[dim cyan][verbose][/dim cyan] skip watch_dir {watch_dir!r}: "
                        f"does not exist on disk"
                    )
                continue
            wd_str = str(watch_dir)
            for c in all_project_chunks:
                if c.file_path.startswith(wd_str) or wd_str == ".":
                    chunk_id = (
                        f"{c.file_path}::{c.chunk_type}::{c.name}::{c.start_line}"
                    )
                    if chunk_id not in seen_chunk_ids:
                        seen_chunk_ids.add(chunk_id)
                        all_chunks.append(c)

            # v1.8.1: tolerate per-watch-dir OS errors so a single bad
            # subtree (EINTR, PermissionError, etc.) doesn't abort the
            # full rebuild. Same pattern as _get_changed_files.
            try:
                for p in abs_dir.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.suffix not in extensions:
                        v_skipped_extension += 1
                        if verbose:
                            rel = str(p.relative_to(_project_root()))
                            console.print(
                                f"[dim cyan][verbose][/dim cyan] skip {rel}: "
                                f"extension {p.suffix!r} not in file_extensions"
                            )
                        continue
                    if any(s in p.parts for s in skip_dirs):
                        v_skipped_in_skip_dirs += 1
                        if verbose:
                            rel = str(p.relative_to(_project_root()))
                            matched_skip = next(s for s in skip_dirs if s in p.parts)
                            console.print(
                                f"[dim cyan][verbose][/dim cyan] skip {rel}: "
                                f"path contains skip_dir {matched_skip!r}"
                            )
                        continue
                    rel = str(p.relative_to(_project_root()))
                    file_hashes[rel] = _compute_hash(p)
                    v_matched += 1
                    if verbose:
                        console.print(f"[dim cyan][verbose][/dim cyan] match {rel}")
            except (OSError, RuntimeError) as e:
                v_skipped_other += 1
                logger.warning(
                    "cmd_full_rebuild: skipping watch_dir %s due to filesystem error: %s",
                    watch_dir,
                    e,
                )
                from mcp_server._safe_crash import safe_log_crash

                safe_log_crash(
                    e, context=f"cmd_full_rebuild: walk of {watch_dir} aborted"
                )
                continue

        progress.update(task1, completed=100)
        task2 = progress.add_task(
            f"[cyan]Embedding {len(all_chunks)} chunks into ChromaDB...",
            total=len(all_chunks),
        )

        ids, docs, metadatas = [], [], []
        for i, chunk in enumerate(all_chunks):
            doc_id, doc, meta = _chunk_to_document(chunk)
            ids.append(doc_id)
            docs.append(doc)
            metadatas.append(meta)

            if len(ids) >= 100 or i == len(all_chunks) - 1:
                if ids:
                    collection.add(ids=ids, documents=docs, metadatas=metadatas)
                    ids, docs, metadatas = [], [], []
            progress.update(task2, advance=1)

    # Bug H summary: always shown when verbose, regardless of outcome.
    # When 0 chunks resulted, this is the user's actionable diagnostic.
    if verbose:
        console.print(
            f"[dim cyan][verbose][/dim cyan] summary: "
            f"matched={v_matched} skipped_extension={v_skipped_extension} "
            f"skipped_in_skip_dirs={v_skipped_in_skip_dirs} fs_errors={v_skipped_other}"
        )

    # v1.8: safety hint BEFORE the success print so it's the last thing a user
    # sees when their config covers nothing. Logger fires unconditionally so
    # background (auto-init) invocations also leave a trace.
    if not all_chunks:
        _warn_zero_chunks(watched_dirs, extensions)

    console.print(
        f"[green]✓[/green] Full rebuild complete: {len(all_chunks)} chunks indexed."
    )

    for path, f_hash in file_hashes.items():
        db.update_file_hash(path, f_hash)

    console.print("[cyan]Generating auto-graph stubs...[/cyan]")
    generate_graph_sqlite(str(_project_root()), str(db.db_path))
    db.close()


def cmd_incremental(
    quiet: bool = False, file_paths: list[str] | None = None, verbose: bool = False
):
    """Incremental update.

    Args:
        quiet: suppress all output (git hook usage).
        file_paths: list of paths to re-index (caller-scoped). If None,
                    scans the whole project for changed files.
        verbose: emit per-file decisions for diagnosing why files were
                 or weren't matched. (Bug H, 2026-05-17, P10.)
    """
    from indexer.chunker import chunk_file
    from indexer.graph_generator import generate_graph_sqlite
    from rich.console import Console

    console = Console(quiet=quiet)

    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    explicit_files = file_paths or []
    changed_items = (
        _get_requested_files(explicit_files)
        if explicit_files
        else _get_changed_files(db)
    )

    if not changed_items:
        if explicit_files:
            # Caller-scoped incremental — zero matches is the caller's business,
            # not misconfiguration. Do NOT fire the zero-chunks hint.
            console.print("[green]✓[/green] No matching files found to re-index.")
        else:
            # Project-wide incremental scan: three distinct states must be
            # distinguished (Bug B, P1 fix — "up to date" was a lie when
            # the graph was empty):
            #
            #   1. Graph empty + config matches files →
            #      "graph not built yet, run `codevira index --full`"
            #   2. Graph empty + config matches nothing →
            #      _warn_zero_chunks (config misconfigured)
            #   3. Graph populated + nothing changed → "up to date"  (truthful)
            #
            # The old code conflated (1) with (3), telling users they were
            # "up to date" when nothing had ever been indexed.
            config = _load_config()
            wd = config.get("watched_dirs", ["src"])
            exts = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
            skip = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])
            config_matches = _any_files_match(wd, exts, skip)
            # Cheap graph-empty probe: count nodes via the graph DB.
            try:
                graph_node_count = (
                    db.count_nodes() if hasattr(db, "count_nodes") else None
                )
            except Exception:
                graph_node_count = None
            if graph_node_count == 0:
                if config_matches:
                    # State 1: config is fine but nothing has been indexed yet.
                    console.print(
                        "[yellow]⚠[/yellow]  Graph has 0 nodes but config matches files on disk."
                    )
                    console.print(
                        "  This project hasn't been indexed yet (or the graph was wiped)."
                    )
                    console.print("  Fix: run [bold]codevira index --full[/bold]")
                else:
                    # State 2: misconfigured.
                    _warn_zero_chunks(wd, exts, quiet=quiet)
            elif not config_matches:
                # Edge case: graph populated, but current config now matches
                # nothing (user trimmed watched_dirs to a dir that's empty).
                # Warn the user — they likely want to fix configure.
                _warn_zero_chunks(wd, exts, quiet=quiet)
            else:
                # State 3: legitimate steady-state.
                console.print("[green]✓[/green] No files changed. Index is up to date.")
        db.close()
        return 0

    file_label = "requested file(s)" if explicit_files else "changed file(s)"
    console.print(
        f"[bold cyan]Incremental update:[/bold cyan] {len(changed_items)} {file_label}"
    )

    # Check if semantic search deps are available
    has_search = _check_search_deps()
    collection = None

    if has_search:
        try:
            # 2026-05-17 fix for the 2026-05-16 UDAP/QuickCourier crash pattern:
            # probe Chroma BEFORE the per-file loop. If the store is corrupted
            # (HNSW writer error), this raises ChromaCorrupted with a clear
            # fix_command — fall through to graph-only mode INSTEAD of hitting
            # the 41-crash-per-file pattern that motivated the rate-limiter.
            client = _get_chroma_client(probe=True)
            embed_fn = _get_embedding_fn()
            collection = client.get_collection(
                COLLECTION_NAME, embedding_function=embed_fn
            )
        except ChromaCorrupted as exc:
            collection = None
            if not quiet:
                console.print(f"[red]✗[/red] {exc}")
                console.print(
                    "[yellow]⚠[/yellow]  Falling back to graph-only update for this incremental."
                )
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(exc, context="incremental index: ChromaCorrupted at startup")
        except Exception:
            # No existing collection — skip semantic indexing, still update graph
            collection = None
            if not quiet:
                console.print(
                    "[yellow]⚠[/yellow]  No semantic index found — updating graph only."
                )

    indexed_any = False

    if collection is not None:
        # 2026-05-17 P5 (bounded resources) circuit breaker: if the same
        # Chroma error fires N times in a row (the UDAP / QuickCourier
        # pattern), HALT the loop and fall through to graph-only mode.
        # Previously each per-file failure logged a crash and continued —
        # producing 41 identical entries for one root cause. The rate-
        # limiter coalesced log entries; the circuit breaker stops the
        # wasted work entirely.
        consecutive_chroma_failures = 0
        CHROMA_FAILURE_LIMIT = 5
        chroma_aborted = False

        # Semantic search + graph update
        with _chroma_write_lock:
            for fpath, fhash in changed_items:
                if chroma_aborted:
                    # P9 graceful: continue updating graph state for remaining
                    # files even though Chroma is broken; we'll fall through
                    # to graph-only path after the loop.
                    db.update_file_hash(fpath, fhash)
                    indexed_any = True
                    continue

                # Per-iteration error flag — RESET only fires when both
                # delete AND add succeed. Without this, a delete that
                # consistently fails + add that succeeds would re-zero the
                # counter every iteration, defeating the breaker entirely.
                # (Caught by test_per_file_chroma_failures_halt_after_limit
                # 2026-05-17 — a regression test wrote the wrong logic
                # twice in a row before this was right.)
                iter_had_chroma_error = False

                try:
                    collection.delete(where={"file_path": fpath})
                except Exception as e:
                    from mcp_server._safe_crash import safe_log_crash

                    safe_log_crash(
                        e, context=f"incremental index: delete old chunks for {fpath}"
                    )
                    if _looks_like_chroma_corruption(e):
                        consecutive_chroma_failures += 1
                        iter_had_chroma_error = True
                        if consecutive_chroma_failures >= CHROMA_FAILURE_LIMIT:
                            chroma_aborted = True
                            console.print(
                                f"[red]✗[/red] {CHROMA_FAILURE_LIMIT} consecutive Chroma "
                                f"errors — circuit broken. Run `codevira heal --vectors` "
                                f"and `codevira index --full` to recover. "
                                f"Continuing in graph-only mode for the rest of this batch."
                            )
                            continue

                try:
                    chunks = chunk_file(
                        str(_project_root() / fpath), str(_project_root())
                    )
                    if chunks:
                        ids, docs, metas = [], [], []
                        for chunk in chunks:
                            doc_id, doc, meta = _chunk_to_document(chunk)
                            ids.append(doc_id)
                            docs.append(doc)
                            metas.append(meta)
                        collection.add(ids=ids, documents=docs, metadatas=metas)

                    db.update_file_hash(fpath, fhash)
                    indexed_any = True
                    console.print(
                        f"  [green]+[/green] Re-indexed {len(chunks)} chunks for {fpath}"
                    )
                    # Only reset the breaker if THIS iteration had no Chroma
                    # error at all (neither delete nor add). Without this
                    # tighter check, a per-file delete that consistently
                    # fails would never trip the breaker because each
                    # successful add zeroed the counter.
                    if not iter_had_chroma_error:
                        consecutive_chroma_failures = 0

                except Exception as e:
                    console.print(f"[red]Error indexing {fpath}: {e}[/red]")
                    from mcp_server._safe_crash import safe_log_crash

                    safe_log_crash(e, context=f"incremental index: indexing {fpath}")
                    if _looks_like_chroma_corruption(e):
                        consecutive_chroma_failures += 1
                        iter_had_chroma_error = True
                        if consecutive_chroma_failures >= CHROMA_FAILURE_LIMIT:
                            chroma_aborted = True
                            console.print(
                                f"[red]✗[/red] {CHROMA_FAILURE_LIMIT} consecutive Chroma "
                                f"errors — circuit broken. Run `codevira heal --vectors` "
                                f"and `codevira index --full` to recover."
                            )
                    continue
    else:
        # Graph-only mode: update file hashes without semantic indexing
        for fpath, fhash in changed_items:
            db.update_file_hash(fpath, fhash)
            indexed_any = True

    if indexed_any:
        generate_graph_sqlite(str(_project_root()), str(db.db_path))

    db.close()
    return 0


_watcher_logger = logging.getLogger("codevira.watcher")

# Debounce delay: how long to wait after the last file change before reindexing.
# This prevents rapid saves (auto-formatters, IDE auto-save) from triggering
# dozens of reindex cycles.
DEBOUNCE_SECONDS = 2.0


def start_background_watcher(quiet: bool = True):
    """
    Start a non-blocking file watcher that auto-reindexes on source changes.

    Returns the watchdog Observer (already started) so the caller can stop it
    later if needed.  The watcher uses a debounce timer: after the last file
    event, it waits DEBOUNCE_SECONDS before running cmd_incremental().

    Called automatically by the MCP server on startup.

    v1.8.1: refuses to start when project_root is $HOME or a system top-level.
    Defense-in-depth — server.main() / run_http_server() / cmd_serve all
    pre-check this, but a programmatic caller (test harness, third-party
    integration) could bypass them. Without this guard, the watcher would
    walk ~/Library/Group Containers/... and crash on EINTR — exactly the
    v1.8.0 production failure mode.
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from mcp_server.paths import is_invalid_project_root

    rejection = is_invalid_project_root(_project_root())
    if rejection:
        _watcher_logger.warning(
            "Background watcher refusing to start: %s",
            rejection,
        )
        # Return None — callers that store the observer for later .stop()
        # must handle None (server.main does: `if watcher is not None`).
        return None

    config = _load_config()
    watched_dirs = config.get("watched_dirs", ["src"])
    extensions = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
    skip_dirs = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])

    class DebouncedHandler(FileSystemEventHandler):
        def __init__(self):
            super().__init__()
            self._timer: threading.Timer | None = None
            self._lock = threading.Lock()

        def _schedule_reindex(self, src_path: str):
            abs_path = Path(src_path)
            if not any(abs_path.suffix == ext for ext in extensions):
                return
            if any(skip in abs_path.parts for skip in skip_dirs):
                return

            with self._lock:
                # Cancel any pending timer and restart the debounce window
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(DEBOUNCE_SECONDS, self._do_reindex)
                self._timer.daemon = True
                self._timer.start()

        def _do_reindex(self):
            # Circuit-breaker gate (Pillar 3.2): if too many consecutive
            # failures, skip until the backoff window elapses.
            if not _watcher_circuit_should_run():
                status = watcher_circuit_status()
                _watcher_logger.warning(
                    "Background watcher circuit OPEN (%d failures, "
                    "%.0fs until retry); skipping reindex.",
                    status["consecutive_failures"],
                    status["seconds_until_retry"],
                )
                return
            try:
                _watcher_logger.debug(
                    "File change detected — running incremental reindex"
                )
                # Note: cmd_incremental acquires _chroma_write_lock internally,
                # so we don't need to acquire it here.
                cmd_incremental(quiet=quiet)
                _watcher_logger.debug("Incremental reindex complete")
                _watcher_circuit_record_success()
            except Exception as e:
                _watcher_circuit_record_failure(e)
                status = watcher_circuit_status()
                if status["open"]:
                    _watcher_logger.warning(
                        "Background reindex failed (failure #%d): %s — "
                        "circuit OPEN; backing off %.0fs",
                        status["consecutive_failures"],
                        e,
                        status["seconds_until_retry"],
                    )
                else:
                    _watcher_logger.warning(
                        "Background reindex failed (failure #%d): %s",
                        status["consecutive_failures"],
                        e,
                    )
                from mcp_server._safe_crash import safe_log_crash

                safe_log_crash(e, context="background watcher: incremental reindex")

        def on_modified(self, event):
            if not event.is_directory:
                self._schedule_reindex(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                self._schedule_reindex(event.src_path)

        def on_deleted(self, event):
            if not event.is_directory:
                self._schedule_reindex(event.src_path)

    observer = Observer()
    observer.daemon = True
    handler = DebouncedHandler()

    scheduled = 0
    for wd in watched_dirs:
        path = _project_root() / wd
        if path.exists():
            observer.schedule(handler, str(path), recursive=True)
            scheduled += 1

    if scheduled > 0:
        observer.start()
        _watcher_logger.info(
            "Background watcher started — monitoring %d dir(s): %s",
            scheduled,
            ", ".join(watched_dirs),
        )
    else:
        _watcher_logger.warning("No valid watched_dirs found — watcher not started")

    return observer


def cmd_watch():
    """Blocking CLI mode: start watcher and keep the process alive."""
    config = _load_config()
    watched_dirs = config.get("watched_dirs", ["src"])
    print(f"Watching for changes in: {', '.join(watched_dirs)}...")
    print("Press Ctrl+C to stop.\n")

    observer = start_background_watcher(quiet=False)
    if observer is None:
        # v1.8.1: start_background_watcher refused (project_root invalid).
        # The warning has already been logged; print a parallel CLI message
        # and exit cleanly so the user sees something on the terminal.
        print(
            "Watcher refused to start: project root is invalid. "
            "cd into a real project and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def get_indexing_status() -> dict:
    """Return the current background indexing progress. Thread-safe."""
    with _bg_lock:
        return {
            "status": _bg_status,
            "files_indexed": _bg_files_indexed,
            "total_files": _bg_total_files,
        }


def start_background_full_index(callback=None) -> "threading.Thread":
    """Start a full index rebuild in a background daemon thread.

    This is used by auto_init.py to build the index without blocking tool calls.
    The ChromaDB write lock (_chroma_write_lock) prevents concurrent writes
    with the file watcher.

    Args:
        callback: Optional callable invoked when indexing completes.
                  Called with (status: str) where status is 'done' or 'error'.

    Returns:
        The started Thread object.
    """
    global _bg_status, _bg_files_indexed, _bg_total_files

    def _run():
        global _bg_status, _bg_files_indexed, _bg_total_files
        with _bg_lock:
            _bg_status = "running"
            _bg_files_indexed = 0
            _bg_total_files = 0

        try:
            with _chroma_write_lock:
                cmd_full_rebuild()
            with _bg_lock:
                _bg_status = "done"
        except Exception as e:
            with _bg_lock:
                _bg_status = "error"
            _watcher_logger.error("Background full-index failed: %s", e)
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(e, context="background full-index")
        finally:
            if callback is not None:
                try:
                    callback(_bg_status)
                except Exception:
                    pass

    t = threading.Thread(target=_run, daemon=True, name="codevira-bg-index")
    t.start()
    return t


def cmd_status(check_stale: bool = False, show_global: bool = False):
    """Print index health summary.

    Fast by default (~100ms): queries SQLite counts only, skips ChromaDB
    embedding-function init and file-hash walk.

    Args:
        check_stale: Scan all source files and count how many have changed
                     since last index (slow: O(n) SHA256 hashes).
        show_global: Add a panel showing cross-project intelligence stats
                     and the macOS launchd service status (if running).
    """
    data_dir = get_data_dir()
    graph_db_path = data_dir / "graph" / "graph.db"

    # Fast path: project has no local index yet. Skip rich/sqlite/chromadb imports
    # entirely and print a plain-text one-liner.
    # P0-2 (rc.5): if the project IS registered in global.db (visible to the
    # cross-project layer), say so explicitly instead of "Not initialized" —
    # the latter was a lie for projects that had been registered via auto-init
    # without an in-progress local index build.
    if not graph_db_path.exists():
        registered_msg: str | None = None
        try:
            from mcp_server.paths import get_project_root, get_global_db_path
            import sqlite3 as _sqlite3

            project_root = get_project_root()
            db_path = get_global_db_path()
            if db_path.is_file():
                _conn = _sqlite3.connect(str(db_path))
                row = _conn.execute(
                    "SELECT name, last_synced_at FROM projects WHERE path = ?",
                    (str(project_root),),
                ).fetchone()
                _conn.close()
                if row:
                    registered_msg = (
                        f"  Codevira — Registered ({row[0]}) but no local index yet"
                    )
        except Exception:
            pass

        print()
        if registered_msg:
            print(registered_msg)
            print("  " + "─" * 44)
            print()
            print("  This project is in ~/.codevira/global.db but the local")
            print("  graph + semantic index haven't been built yet.")
            print("  Run `codevira index` to build them now.")
        else:
            print("  Codevira — Not initialized for this project")
            print("  " + "─" * 44)
            print()
            print("  Run `codevira init` to initialize, or use this project")
            print("  in an AI tool — auto-init triggers on first MCP tool call.")
        print()
        if show_global:
            from rich.console import Console
            from rich.table import Table
            from rich.panel import Panel

            _print_global_status(Console(), Table, Panel)
        return

    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    db = SQLiteGraph(graph_db_path)

    # Count graph nodes — fast SQLite query
    try:
        nodes = db.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    except Exception:
        nodes = 0

    # v2.2.0: ChromaDB / sentence-transformers / torch were deleted in
    # Phase E. There is no semantic chunk count to display. `chunk_count`
    # is retained at 0 because the explanation branches below still
    # reference it for backwards-compatible message logic.
    chunk_count = 0

    table = Table(show_header=False, box=None)
    table.add_row("[cyan]Graph Nodes:[/cyan]", str(nodes))
    # v2.2.0: ChromaDB / semantic code search was removed entirely
    # (Phase E). The "ChromaDB Chunks" / "Semantic Search" row is no
    # longer meaningful; the code graph IS the search surface now.
    # `chunk_count` is retained at 0 because the explanation logic
    # below still references it.

    # Stale file scan is slow (SHA256 every source file) — only run on demand
    if check_stale:
        stale_files = _get_changed_files(db)
        table.add_row("[cyan]Outdated Files:[/cyan]", str(len(stale_files)))
    else:
        table.add_row(
            "[cyan]Outdated Files:[/cyan]", "[dim]run with --check-stale[/dim]"
        )

    panel = Panel(
        table,
        title="[bold green]Codevira Index Status[/bold green]",
        expand=False,
        border_style="green",
    )
    console.print(panel)

    # 2026-05-17 Bug C fix (P1 + P10): when graph.db exists but is empty,
    # status was showing "Graph Nodes: 0 / Chunks: 0" with no actionable
    # signal — a silent failure. Now distinguishes three states (mirrors
    # the cmd_incremental fix for Bug B):
    #   1. Graph populated → legitimate steady-state, just show the table
    #   2. Graph empty + config matches files → "never indexed, run --full"
    #   3. Graph empty + config matches nothing → fire misconfig hint
    #
    # 2026-05-17 follow-up: extended to nodes == 0 even when chunks > 0.
    # The Bug E markdown chunker means a docs-only project can have
    # semantic chunks (markdown sections) yet zero graph nodes (no parseable
    # code). The old `nodes == 0 AND chunks == 0` predicate left that
    # state silent again — caught by the e2e gauntlet's
    # test_status_reflects_reality[docs_only].
    if nodes == 0:
        try:
            config = _load_config()
            wd = config.get("watched_dirs", ["src"])
            exts = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
            skip = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])
            config_matches = _any_files_match(wd, exts, skip)
        except Exception:
            # P9 (graceful): if config load fails, fall back to generic warning
            config_matches = None

        if config_matches is True:
            # State 2: project has matching files but graph is empty.
            # Two sub-cases: chunks==0 (never indexed) vs chunks>0 (only
            # docs / non-parseable files; graph empty by design).
            console.print()
            if chunk_count == 0:
                console.print(
                    "[yellow]⚠[/yellow]  Graph is empty. Either this project "
                    "hasn't been indexed yet, OR it has no parseable source "
                    "code in the configured extensions."
                )
                console.print(
                    "  codevira indexes code, not documentation — "
                    "markdown / YAML / text files don't produce graph nodes."
                )
                console.print(
                    "  Fix (if you expected nodes): run [bold]codevira index "
                    "--full[/bold] and check the per-file decisions."
                )
            else:
                # chunks exist (markdown/text) but no graph nodes — common
                # for docs-only repos. Explain so the user knows it's
                # not a bug.
                console.print(
                    "[yellow]⚠[/yellow]  Graph has 0 nodes "
                    f"(but {chunk_count} semantic chunks indexed)."
                )
                console.print(
                    "  This project may have no parseable source code — "
                    "only docs / configs were indexed. Semantic search works; "
                    "the symbol-level graph stays empty by design."
                )
                console.print(
                    "  If you expected graph nodes (e.g. Python files), run "
                    "[bold]codevira index --verbose --full[/bold] to see "
                    "per-file decisions."
                )
        elif config_matches is False:
            # State 3: config matches nothing — same hint as cmd_incremental.
            # We can't call _warn_zero_chunks here because it writes to
            # stderr; the user invoked `status` interactively and wants
            # the hint on stdout next to the table.
            console.print()
            console.print(
                "[yellow]⚠[/yellow]  Graph and semantic index are empty AND "
                "your config matches NO files on disk."
            )
            console.print(f"  watched_dirs:    {list(wd)}")
            console.print(f"  file_extensions: {list(exts)}")
            console.print(
                "  Fix: run [bold]codevira configure[/bold] to pick the right folders"
            )
        else:
            # Generic fallback if we couldn't load config.
            console.print()
            console.print(
                "[yellow]⚠[/yellow]  Graph and semantic index are empty. "
                "Run [bold]codevira index --full[/bold] or [bold]codevira configure[/bold] to diagnose."
            )

    # v2.2.0: removed "reinstall to enable semantic search" tip — there
    # is no version of codevira 2.2+ that has semantic code search. The
    # tip pointed users at a non-existent capability and confused
    # first-contact users.

    if check_stale and stale_files:
        console.print("\n[yellow]Files requiring re-indexing:[/yellow]")
        for fp, _ in stale_files[:10]:
            console.print(f"  - {fp}")
        if len(stale_files) > 10:
            console.print(f"  ... and {len(stale_files) - 10} more.")

    db.close()

    if show_global:
        _print_global_status(console, Table, Panel)


def _print_global_status(console, Table, Panel):
    """Print cross-project inventory + launchd service status.

    P0-3 + P2-9 (rc.5): "Projects Tracked" reads from the canonical
    inventory helper so the number agrees with `codevira projects`
    and `codevira clean --dry-run`. Ghost / orphan numbers shown
    alongside so the user has the full picture.

    v3.0.0 (2026-05-22 surface-cut audit): the "Global Preferences"
    and "Global Rules" rows were removed. The audit deleted the
    preferences + learned_rules surface; those counts were always
    zero, taking up a row each for no signal.
    """
    try:
        from mcp_server._project_inventory import enumerate_projects, summarize

        inventory = summarize(enumerate_projects())
        error: str | None = None
    except Exception as e:
        inventory = {"tracked": 0, "ghost": 0, "orphan": 0, "stale": 0, "total": 0}
        error = str(e)

    g_table = Table(show_header=False, box=None)
    if error is not None:
        g_table.add_row("[cyan]Project Inventory:[/cyan]", f"[dim]error: {error}[/dim]")
    else:
        proj_summary = (
            f"{inventory['tracked']} tracked"
            + (
                f" · [yellow]{inventory['ghost']} ghost[/yellow]"
                if inventory["ghost"]
                else ""
            )
            + (
                f" · [red]{inventory['orphan']} orphan[/red]"
                if inventory["orphan"]
                else ""
            )
        )
        g_table.add_row("[cyan]Projects Tracked:[/cyan]", proj_summary)

    # Launchd service status (macOS only)
    try:
        from mcp_server.launchd import launchd_status

        ls = launchd_status()
        if ls.get("platform") == "not_macos":
            g_table.add_row("[cyan]Launchd Service:[/cyan]", "[dim]macOS only[/dim]")
        elif not ls.get("installed"):
            g_table.add_row("[cyan]Launchd Service:[/cyan]", "[dim]not installed[/dim]")
        elif ls.get("running"):
            g_table.add_row("[cyan]Launchd Service:[/cyan]", "[green]running[/green]")
        else:
            g_table.add_row(
                "[cyan]Launchd Service:[/cyan]",
                "[yellow]installed (not running)[/yellow]",
            )
    except Exception:
        pass

    console.print(
        Panel(
            g_table,
            title="[bold blue]Global Status[/bold blue]",
            expand=False,
            border_style="blue",
        )
    )


def cmd_generate_graph():
    from indexer.graph_generator import generate_graph_sqlite

    db_path = str(get_data_dir() / "graph" / "graph.db")
    print(f"Generating context graph nodes from {_project_root()} into SQLite")
    result = generate_graph_sqlite(str(_project_root()), db_path)

    print(f"  Files scanned: {result['files_processed']}")
    print(f"  Nodes added:   {result['nodes_added']}")
    print(f"  Nodes skipped: {result['nodes_skipped']}")


def cmd_bootstrap_roadmap():
    from indexer.graph_generator import generate_roadmap_stub

    roadmap_file = get_data_dir() / "roadmap.yaml"
    if roadmap_file.exists():
        print(f"Roadmap already exists at {roadmap_file}")
        return
    generate_roadmap_stub(str(_project_root()), str(roadmap_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Codevira Codebase Indexer (SQLite + ChromaDB + SHA256)"
    )
    parser.add_argument(
        "--full", action="store_true", help="Perform a full rebuild of the index."
    )
    parser.add_argument(
        "--status", action="store_true", help="Show index status and outdated files."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch for file changes and update incrementally.",
    )
    parser.add_argument(
        "--generate-graph",
        action="store_true",
        help="Auto-generate SQLite graph stubs.",
    )
    parser.add_argument(
        "--bootstrap-roadmap",
        action="store_true",
        help="Create initial roadmap.yaml stub.",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress non-error output."
    )
    args = parser.parse_args()

    # v1.8.1 round-4 hardening: refuse $HOME / system root for the
    # `python -m indexer.index_codebase ...` direct entry. This bypasses
    # the `codevira index` CLI guard, so we add a parallel guard here.
    # `--status` is exempt — it's read-only and bails early on missing
    # graph.db without creating any state.
    if not args.status:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        _rejection = is_invalid_project_root(get_project_root())
        if _rejection:
            print(f"Error: {_rejection}", file=sys.stderr)
            sys.exit(1)

    if args.status:
        cmd_status()
    elif args.full:
        cmd_full_rebuild()
        if args.generate_graph:
            print()
            cmd_generate_graph()
        if args.bootstrap_roadmap:
            print()
            cmd_bootstrap_roadmap()
    elif args.watch:
        cmd_watch()
    elif args.generate_graph:
        cmd_generate_graph()
        if args.bootstrap_roadmap:
            print()
            cmd_bootstrap_roadmap()
    elif args.bootstrap_roadmap:
        cmd_bootstrap_roadmap()
    else:
        cmd_incremental(quiet=args.quiet)
