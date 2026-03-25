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

PROJECT_ROOT = get_project_root()
INDEX_DIR = get_data_dir() / "codeindex"
COLLECTION_NAME = "codebase_index"

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

def _get_chroma_client():
    try:
        import chromadb
    except ImportError:
        print("ERROR: chromadb not installed.")
        sys.exit(1)
    db_dir = str(INDEX_DIR)
    return chromadb.PersistentClient(path=db_dir)

def _get_embedding_fn():
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    except ImportError:
        print("ERROR: sentence-transformers not installed.")
        sys.exit(1)

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
        watch_path = PROJECT_ROOT / watch_dir
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
                rel_path = str(p.relative_to(PROJECT_ROOT))
                if rel_path in seen_paths:
                    continue
                seen_paths.add(rel_path)
                current_hash = _compute_hash(p)
                stored_hash = db.get_file_hash(rel_path)
                
                if current_hash != stored_hash:
                    changed.append((rel_path, current_hash))
            except Exception:
                pass
                
    return changed

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
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")

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
        all_project_chunks = chunk_project(str(PROJECT_ROOT))
        
        for watch_dir in watched_dirs:
            abs_dir = PROJECT_ROOT / watch_dir
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
                    rel = str(p.relative_to(PROJECT_ROOT))
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
    generate_graph_sqlite(str(PROJECT_ROOT), str(db.db_path))
    db.close()

def cmd_incremental(quiet: bool = False):
    from indexer.chunker import chunk_file
    from indexer.graph_generator import generate_graph_sqlite
    from rich.console import Console
    console = Console(quiet=quiet)

    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    changed_items = _get_changed_files(db)
    
    if not changed_items:
        console.print("[green]✓[/green] No files changed. Index is up to date.")
        db.close()
        return 0

    console.print(f"[bold cyan]Incremental update:[/bold cyan] {len(changed_items)} changed file(s)")

    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()
    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    except Exception:
        console.print("[red]No existing index found.[/red] Run codevira index --full first.")
        db.close()
        sys.exit(1)

    for fpath, fhash in changed_items:
        try:
            collection.delete(where={"file_path": fpath})
        except Exception:
            pass

        try:
            chunks = chunk_file(str(PROJECT_ROOT / fpath), str(PROJECT_ROOT))
            if chunks:
                ids, docs, metas = [], [], []
                for chunk in chunks:
                    doc_id, doc, meta = _chunk_to_document(chunk)
                    ids.append(doc_id)
                    docs.append(doc)
                    metas.append(meta)
                collection.add(ids=ids, documents=docs, metadatas=metas)

            generate_graph_sqlite(str(PROJECT_ROOT), str(db.db_path))
            db.update_file_hash(fpath, fhash)
            console.print(f"  [green]+[/green] Re-indexed {len(chunks)} chunks for {fpath}")
            
        except Exception as e:
            console.print(f"[red]Error indexing {fpath}: {e}[/red]")
            continue
            
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
                cmd_incremental(quiet=quiet)
                _watcher_logger.debug("Incremental reindex complete")
            except Exception as e:
                _watcher_logger.warning("Background reindex failed: %s", e)

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
        path = PROJECT_ROOT / wd
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

def cmd_status():
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    
    try:
        client = _get_chroma_client()
        embed_fn = _get_embedding_fn()
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
        chunk_count = collection.count()
    except Exception:
        chunk_count = 0

    stale_files = _get_changed_files(db)

    table = Table(show_header=False, box=None)
    table.add_row("[cyan]ChromaDB Chunks:[/cyan]", str(chunk_count))
    table.add_row("[cyan]Outdated Files:[/cyan]", str(len(stale_files)))

    panel = Panel(
        table,
        title="[bold green]Codevira Index Status[/bold green]",
        expand=False,
        border_style="green"
    )
    console.print(panel)

    if stale_files:
        console.print("\n[yellow]Files requiring re-indexing:[/yellow]")
        for fp, _ in stale_files[:10]:
            console.print(f"  - {fp}")
        if len(stale_files) > 10:
            console.print(f"  ... and {len(stale_files) - 10} more.")
            
    db.close()

def cmd_generate_graph():
    from indexer.graph_generator import generate_graph_sqlite
    db_path = str(get_data_dir() / "graph" / "graph.db")
    print(f"Generating context graph nodes from {PROJECT_ROOT} into SQLite")
    result = generate_graph_sqlite(str(PROJECT_ROOT), db_path)

    print(f"  Files scanned: {result['files_processed']}")
    print(f"  Nodes added:   {result['nodes_added']}")
    print(f"  Nodes skipped: {result['nodes_skipped']}")

def cmd_bootstrap_roadmap():
    from indexer.graph_generator import generate_roadmap_stub
    roadmap_file = get_data_dir() / "roadmap.yaml"
    if roadmap_file.exists():
        print(f"Roadmap already exists at {roadmap_file}")
        return
    generate_roadmap_stub(str(PROJECT_ROOT), str(roadmap_file))

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
