from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import hashlib
import threading
from pathlib import Path

from mcp_server.paths import get_data_dir, get_project_root
from indexer.sqlite_graph import SQLiteGraph

COLLECTION_NAME = "codebase_index"

# Global lock — prevents the background watcher and background full-index from
# writing to ChromaDB simultaneously. Both operations must acquire this lock
# before any ChromaDB write (add/delete/recreate collection).
_chroma_write_lock = threading.Lock()

# Atomic counters for background indexing progress
_bg_files_indexed: int = 0
_bg_total_files: int = 0
_bg_status: str = "idle"   # idle | running | done | error
_bg_lock = threading.Lock()


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
    """Return True if chromadb + sentence-transformers are available."""
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False

def _get_chroma_client():
    try:
        import chromadb
    except ImportError:
        raise ImportError(
            "Semantic search requires chromadb. "
            "Install it with: pip install 'codevira[search]'"
        )
    db_dir = str(_index_dir())
    return chromadb.PersistentClient(path=db_dir)

def _get_embedding_fn():
    # chromadb's SentenceTransformerEmbeddingFunction raises ValueError
    # (not ImportError) when sentence_transformers isn't installed, because
    # it catches the ImportError internally and re-raises. We catch both.
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    except (ImportError, ValueError) as e:
        raise ImportError(
            f"Semantic search requires sentence-transformers. "
            f"Install it with: pip install 'codevira[search]'. Details: {e}"
        )

def _compute_hash(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

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
                try:
                    from mcp_server.crash_logger import log_crash
                    log_crash(e, context="get_changed_files: hash check")
                except Exception: pass
                
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
    document = f"{chunk.file_path} — {chunk.name}\n{chunk.docstring}\n\n{chunk.source_text}"
    metadata = {
        "file_path": chunk.file_path,
        "name": chunk.name,
        "chunk_type": chunk.chunk_type,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "layer": chunk.layer,
    }
    return doc_id, document, metadata

def cmd_full_rebuild():
    from indexer.chunker import chunk_project
    from indexer.graph_generator import generate_graph_sqlite
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    console = Console()
    _index_dir().mkdir(parents=True, exist_ok=True)
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")

    if not _check_search_deps():
        console.print("[yellow]⚠[/yellow]  Semantic search skipped — install with: [bold]pip install 'codevira\\[search]'[/bold]")
        # Still build the graph even without search deps
        from indexer.graph_generator import generate_graph_sqlite
        result = generate_graph_sqlite(str(_project_root()), str(get_data_dir() / "graph" / "graph.db"))
        console.print(f"[green]✓[/green] Graph built: {result.get('nodes_added', 0)} nodes, {result.get('edges_added', 0)} edges.")
        db.close()
        return

    client = _get_chroma_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    embed_fn = _get_embedding_fn()
    collection = client.create_collection(name=COLLECTION_NAME, embedding_function=embed_fn)

    config = _load_config()
    watched_dirs = config.get("watched_dirs", ["src"])
    extensions = config.get("file_extensions", [".py", ".ts", ".tsx", ".go", ".rs"])
    skip_dirs = config.get("skip_dirs", ["node_modules", ".venv", "__pycache__"])

    all_chunks = []
    file_hashes = {}
    seen_chunk_ids = set()
    
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), console=console) as progress:
        task1 = progress.add_task("[cyan]Parsing and chunking source files...", total=None)
        
        # Chunk the entire project once (not per watched_dir)
        all_project_chunks = chunk_project(str(_project_root()))

        for watch_dir in watched_dirs:
            abs_dir = _project_root() / watch_dir
            if not abs_dir.exists():
                continue
            wd_str = str(watch_dir)
            for c in all_project_chunks:
                if c.file_path.startswith(wd_str) or wd_str == ".":
                    chunk_id = f"{c.file_path}::{c.chunk_type}::{c.name}::{c.start_line}"
                    if chunk_id not in seen_chunk_ids:
                        seen_chunk_ids.add(chunk_id)
                        all_chunks.append(c)
                
            for p in abs_dir.rglob("*"):
                if p.is_file() and p.suffix in extensions and not any(s in p.parts for s in skip_dirs):
                    rel = str(p.relative_to(_project_root()))
                    file_hashes[rel] = _compute_hash(p)
                        
        progress.update(task1, completed=100)
        task2 = progress.add_task(f"[cyan]Embedding {len(all_chunks)} chunks into ChromaDB...", total=len(all_chunks))

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

    console.print(f"[green]✓[/green] Full rebuild complete: {len(all_chunks)} chunks indexed.")
    
    for path, f_hash in file_hashes.items():
        db.update_file_hash(path, f_hash)
        
    console.print(f"[cyan]Generating auto-graph stubs...[/cyan]")
    generate_graph_sqlite(str(_project_root()), str(db.db_path))
    db.close()

def cmd_incremental(quiet: bool = False, file_paths: list[str] | None = None):
    from indexer.chunker import chunk_file
    from indexer.graph_generator import generate_graph_sqlite
    from rich.console import Console
    console = Console(quiet=quiet)

    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    explicit_files = file_paths or []
    changed_items = _get_requested_files(explicit_files) if explicit_files else _get_changed_files(db)

    if not changed_items:
        if explicit_files:
            console.print("[green]✓[/green] No matching files found to re-index.")
        else:
            console.print("[green]✓[/green] No files changed. Index is up to date.")
        db.close()
        return 0

    file_label = "requested file(s)" if explicit_files else "changed file(s)"
    console.print(f"[bold cyan]Incremental update:[/bold cyan] {len(changed_items)} {file_label}")

    # Check if semantic search deps are available
    has_search = _check_search_deps()
    collection = None

    if has_search:
        try:
            client = _get_chroma_client()
            embed_fn = _get_embedding_fn()
            collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
        except Exception:
            # No existing collection — skip semantic indexing, still update graph
            collection = None
            if not quiet:
                console.print("[yellow]⚠[/yellow]  No semantic index found — updating graph only.")

    indexed_any = False

    if collection is not None:
        # Semantic search + graph update
        with _chroma_write_lock:
            for fpath, fhash in changed_items:
                try:
                    collection.delete(where={"file_path": fpath})
                except Exception as e:
                    try:
                        from mcp_server.crash_logger import log_crash
                        log_crash(e, context=f"incremental index: delete old chunks for {fpath}")
                    except Exception: pass

                try:
                    chunks = chunk_file(str(_project_root() / fpath), str(_project_root()))
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
                    console.print(f"  [green]+[/green] Re-indexed {len(chunks)} chunks for {fpath}")

                except Exception as e:
                    console.print(f"[red]Error indexing {fpath}: {e}[/red]")
                    try:
                        from mcp_server.crash_logger import log_crash
                        log_crash(e, context=f"incremental index: indexing {fpath}")
                    except Exception: pass
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
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

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
            try:
                _watcher_logger.debug("File change detected — running incremental reindex")
                # Note: cmd_incremental acquires _chroma_write_lock internally,
                # so we don't need to acquire it here.
                cmd_incremental(quiet=quiet)
                _watcher_logger.debug("Incremental reindex complete")
            except Exception as e:
                _watcher_logger.warning("Background reindex failed: %s", e)
                try:
                    from mcp_server.crash_logger import log_crash
                    log_crash(e, context="background watcher: incremental reindex")
                except Exception: pass

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
            scheduled, ", ".join(watched_dirs),
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
            try:
                from mcp_server.crash_logger import log_crash
                log_crash(e, context="background full-index")
            except Exception:
                pass
        finally:
            if callback is not None:
                try:
                    callback(_bg_status)
                except Exception:
                    pass

    t = threading.Thread(target=_run, daemon=True, name="codevira-bg-index")
    t.start()
    return t


def cmd_status(check_stale: bool = False):
    """Print index health summary.

    Fast by default (~100ms): queries SQLite counts only, skips ChromaDB
    embedding-function init and file-hash walk.

    Pass check_stale=True to scan all source files and count how many
    have changed since last index (slow: O(n) SHA256 hashes).
    """
    data_dir = get_data_dir()
    graph_db_path = data_dir / "graph" / "graph.db"

    # Fast path: project not initialized. Skip rich/sqlite/chromadb imports
    # entirely and print a plain-text one-liner.
    if not graph_db_path.exists():
        print()
        print("  Codevira — Not initialized for this project")
        print("  " + "─" * 44)
        print()
        print("  Run `codevira init` to initialize, or use this project")
        print("  in an AI tool — auto-init triggers on first MCP tool call.")
        print()
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

    # Count chromadb chunks — but avoid importing chromadb if no index exists.
    # chromadb's import alone takes ~700ms cold; we don't want that cost for
    # `codevira status` on a project that hasn't been indexed yet.
    chunk_count = 0
    search_available = True
    index_dir = _index_dir()
    chroma_db_file = index_dir / "chroma.sqlite3"
    if chroma_db_file.exists():
        # Index exists — read chunk count (triggers chromadb import)
        try:
            client = _get_chroma_client()
            try:
                collection = client.get_collection(COLLECTION_NAME)
                chunk_count = collection.count()
            except Exception:
                chunk_count = 0
        except ImportError:
            search_available = False
    else:
        # No index yet — check if chromadb is installed (cheap — just checks
        # for package metadata, no real import). If not, show "not installed".
        try:
            import importlib.util
            if importlib.util.find_spec("chromadb") is None:
                search_available = False
        except Exception:
            pass

    table = Table(show_header=False, box=None)
    table.add_row("[cyan]Graph Nodes:[/cyan]", str(nodes))
    if search_available:
        table.add_row("[cyan]ChromaDB Chunks:[/cyan]", str(chunk_count))
    else:
        table.add_row("[cyan]Semantic Search:[/cyan]", "[dim]not installed[/dim]")

    # Stale file scan is slow (SHA256 every source file) — only run on demand
    if check_stale:
        stale_files = _get_changed_files(db)
        table.add_row("[cyan]Outdated Files:[/cyan]", str(len(stale_files)))
    else:
        table.add_row("[cyan]Outdated Files:[/cyan]", "[dim]run with --check-stale[/dim]")

    panel = Panel(
        table,
        title="[bold green]Codevira Index Status[/bold green]",
        expand=False,
        border_style="green",
    )
    console.print(panel)

    if not search_available:
        console.print("\n[dim]  Tip: pip install 'codevira\\[search]' to enable semantic code search[/dim]")

    if check_stale and stale_files:
        console.print("\n[yellow]Files requiring re-indexing:[/yellow]")
        for fp, _ in stale_files[:10]:
            console.print(f"  - {fp}")
        if len(stale_files) > 10:
            console.print(f"  ... and {len(stale_files) - 10} more.")

    db.close()

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
    parser = argparse.ArgumentParser(description="Codevira Codebase Indexer (SQLite + ChromaDB + SHA256)")
    parser.add_argument("--full", action="store_true", help="Perform a full rebuild of the index.")
    parser.add_argument("--status", action="store_true", help="Show index status and outdated files.")
    parser.add_argument("--watch", action="store_true", help="Watch for file changes and update incrementally.")
    parser.add_argument("--generate-graph", action="store_true", help="Auto-generate SQLite graph stubs.")
    parser.add_argument("--bootstrap-roadmap", action="store_true", help="Create initial roadmap.yaml stub.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-error output.")
    args = parser.parse_args()

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
