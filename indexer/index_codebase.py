"""
Codebase Indexer — builds/updates ChromaDB code index for semantic search.

Usage (via CLI):
  codevira-mcp index          # incremental (files changed since last index build)
  codevira-mcp index --full   # full rebuild from scratch
  codevira-mcp status         # show current index stats

Change detection:
  Incremental mode tracks changes using .codevira/codeindex/.last_indexed timestamp file.
  Any configured file (default: .py) in watched_dirs modified after that timestamp gets re-indexed.
  This catches ALL changes: saved edits, staged files, and committed diffs alike.

Configuration:
  Run codevira-mcp init to create .codevira/config.yaml in your project.
  The index lives at .codevira/codeindex/ and is git-ignored (auto-regenerated).
"""
import argparse
import os
import sys
import time
from pathlib import Path

from mcp_server.paths import get_data_dir, get_project_root

PROJECT_ROOT = get_project_root()
INDEX_DIR = get_data_dir() / "codeindex"
LAST_INDEXED_FILE = INDEX_DIR / ".last_indexed"


def _load_config() -> dict:
    """Load .codevira/config.yaml if present, otherwise return empty dict."""
    config_path = get_data_dir() / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


_config = _load_config()
_project_cfg = _config.get("project", {})

COLLECTION_NAME: str = _project_cfg.get("collection_name", "agent_codebase")
WATCHED_DIRS: list[str] = _project_cfg.get("watched_dirs", ["src"])
FILE_EXTENSIONS: list[str] = _project_cfg.get("file_extensions", [".py"])

# Directories to skip inside watched dirs
SKIP_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git", "migrations"}


def _get_chroma_client():
    try:
        import chromadb
    except ImportError:
        print("ERROR: chromadb not installed. Run: pip install codevira-mcp[dev]")
        sys.exit(1)
    try:
        return chromadb.PersistentClient(path=str(INDEX_DIR))
    except Exception as e:
        from rich.console import Console
        console = Console()
        console.print(f"\n[bold red]Database Corruption or OS Error Detected![/bold red]")
        console.print(f"ChromaDB failed to initialize at: {INDEX_DIR}")
        console.print(f"Error details: {e}\n")
        console.print("To recover, please delete the index directly and rebuild:")
        console.print(f"  [bold]rm -rf {INDEX_DIR}[/bold]")
        console.print("  [bold]codevira index --full[/bold]\n")
        sys.exit(1)


def _get_embedding_fn():
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    except ImportError:
        print("ERROR: sentence-transformers not installed. Run: pip install codevira-mcp")
        sys.exit(1)


def _write_timestamp():
    """Write current time to .last_indexed so future incremental runs know the baseline."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    LAST_INDEXED_FILE.write_text(str(time.time()))


def _read_timestamp() -> float | None:
    """Return the last indexed timestamp, or None if no index exists yet."""
    if LAST_INDEXED_FILE.exists():
        try:
            return float(LAST_INDEXED_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _get_changed_files(since: float | None = None) -> list[str]:
    """
    Return relative paths of configured files modified since the last index build.

    Uses file mtime comparison against .last_indexed timestamp — catches ALL changes:
    unsaved edits, staged changes, and committed diffs. No git dependency.

    Args:
        since: Unix timestamp override. Defaults to .last_indexed file.

    Returns:
        List of relative paths (e.g. 'src/services/generator.py')
    """
    baseline = since if since is not None else _read_timestamp()
    if baseline is None:
        return []  # No baseline → caller should do full rebuild

    changed = []
    for watched in WATCHED_DIRS:
        base = PROJECT_ROOT / watched
        if not base.exists():
            continue
        for ext in FILE_EXTENSIONS:
            for src_file in base.rglob(f"*{ext}"):
                if any(skip in src_file.parts for skip in SKIP_DIRS):
                    continue
                if src_file.stat().st_mtime > baseline:
                    rel = str(src_file.relative_to(PROJECT_ROOT))
                    changed.append(rel)

    return changed


def _chunk_to_document(chunk) -> tuple[str, str, dict]:
    """Convert a CodeChunk to (id, document, metadata) for ChromaDB."""
    doc_id = f"{chunk.file_path}::{chunk.chunk_type}::{chunk.name}::{chunk.start_line}"
    document = f"{chunk.file_path} — {chunk.name}\n{chunk.docstring}\n\n{chunk.source_text}"
    metadata = {
        "file_path": chunk.file_path,
        "chunk_type": chunk.chunk_type,
        "chunk_name": chunk.name,
        "layer": chunk.layer,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "docstring": chunk.docstring[:500] if chunk.docstring else "",
    }
    return doc_id, document, metadata


def cmd_full_rebuild():
    """Full rebuild: delete existing collection and re-index everything."""
    from indexer.chunker import chunk_project
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    console = Console()
    console.print(f"[bold cyan]Full rebuild[/bold cyan] of '[bold]{COLLECTION_NAME}[/bold]' from {PROJECT_ROOT}")
    console.print(f"  Watching: [yellow]{WATCHED_DIRS}[/yellow]")
    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()

    # Delete existing collection if present
    try:
        client.delete_collection(COLLECTION_NAME)
        console.print("  [green]✓[/green] Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    chunks = chunk_project(str(PROJECT_ROOT))
    console.print(f"  [green]✓[/green] Found [bold]{len(chunks)}[/bold] chunks across project.")

    # Batch upsert (ChromaDB handles batches of ~5000)
    batch_size = 500
    ids, docs, metas = [], [], []
    for chunk in chunks:
        doc_id, document, metadata = _chunk_to_document(chunk)
        ids.append(doc_id)
        docs.append(document)
        metas.append(metadata)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Indexing to ChromaDB...", total=len(ids))
        for i in range(0, len(ids), batch_size):
            collection.upsert(
                ids=ids[i:i + batch_size],
                documents=docs[i:i + batch_size],
                metadatas=metas[i:i + batch_size],
            )
            progress.update(task, advance=min(batch_size, len(ids) - i))

    _write_timestamp()
    console.print(f"\n[bold green]Full rebuild complete.[/bold green] {len(ids)} chunks indexed to [magenta]{INDEX_DIR}[/magenta]")
    console.print("\nTo commit the updated index:")
    console.print("  [dim]Note: .codevira/codeindex/ is git-ignored (auto-regenerated on each dev machine)[/dim]")
    console.print("  [cyan]git commit -m 'chore(agents): refresh codebase index'[/cyan]")


def cmd_incremental(since: float | None = None, quiet: bool = False):
    """
    Incremental update: re-index files modified since last index build.

    Uses .last_indexed timestamp — catches all file saves, not just committed changes.
    Called automatically by the post-commit hook and watch mode.
    """
    from indexer.chunker import chunk_file
    from rich.console import Console
    console = Console(quiet=quiet)

    baseline = since if since is not None else _read_timestamp()
    if baseline is None:
        console.print("[red]No baseline found.[/red] Run --full to create the initial index.")
        sys.exit(1)

    changed = _get_changed_files(since=baseline)
    if not changed:
        console.print("[green]✓[/green] No files changed since last index build. Index is up to date.")
        return 0

    console.print(f"[bold cyan]Incremental update:[/bold cyan] {len(changed)} changed file(s)")

    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()

    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    except Exception:
        console.print("[red]No existing index found.[/red] Run [cyan]codevira index --full[/cyan] to create one.")
        sys.exit(1)

    updated = 0
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=quiet,
    ) as progress:
        task = progress.add_task("[cyan]Re-indexing changed files...", total=len(changed))
        for rel_path in changed:
            abs_path = PROJECT_ROOT / rel_path
            if not abs_path.exists():
                # File deleted — remove its chunks
                results = collection.get(where={"file_path": rel_path})
                if results["ids"]:
                    collection.delete(ids=results["ids"])
                    console.print(f"  [red]-[/red] Removed {len(results['ids'])} chunks for deleted {rel_path}")
                progress.update(task, advance=1)
                continue

            # Remove old chunks for this file
            results = collection.get(where={"file_path": rel_path})
            if results["ids"]:
                collection.delete(ids=results["ids"])

            # Add new chunks
            chunks = chunk_file(str(abs_path), str(PROJECT_ROOT))
            if chunks:
                ids, docs, metas = [], [], []
                for chunk in chunks:
                    doc_id, document, metadata = _chunk_to_document(chunk)
                    ids.append(doc_id)
                    docs.append(document)
                    metas.append(metadata)
                collection.upsert(ids=ids, documents=docs, metadatas=metas)
                updated += 1
                console.print(f"  [green]+[/green] Updated {len(chunks)} chunks for {rel_path}")
            progress.update(task, advance=1)

    _write_timestamp()
    console.print(f"[bold green]Incremental update complete.[/bold green] {updated} file(s) re-indexed.")
    return updated


def cmd_watch():
    """
    Watch mode: monitor watched_dirs for file changes, auto-reindex on save.

    Starts a file system watcher. Any configured file saved triggers an incremental
    reindex of just that file. Ctrl+C to stop.

    Requires: pip install watchdog
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
    except ImportError:
        print("ERROR: watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    from indexer.chunker import chunk_file

    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()

    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    except Exception:
        print("No existing index found. Run --full first.")
        sys.exit(1)

    class SourceFileHandler(FileSystemEventHandler):
        def _reindex(self, src_path: str):
            abs_path = Path(src_path)
            if not any(abs_path.suffix == ext for ext in FILE_EXTENSIONS):
                return
            if any(skip in abs_path.parts for skip in SKIP_DIRS):
                return

            rel_path = str(abs_path.relative_to(PROJECT_ROOT))
            # Remove old chunks
            results = collection.get(where={"file_path": rel_path})
            if results["ids"]:
                collection.delete(ids=results["ids"])

            # Add new chunks
            chunks = chunk_file(str(abs_path), str(PROJECT_ROOT))
            if chunks:
                ids, docs, metas = [], [], []
                for chunk in chunks:
                    doc_id, document, metadata = _chunk_to_document(chunk)
                    ids.append(doc_id)
                    docs.append(document)
                    metas.append(metadata)
                collection.upsert(ids=ids, documents=docs, metadatas=metas)

            _write_timestamp()
            print(f"[watch] Re-indexed {len(chunks)} chunks for {rel_path}")

        def on_modified(self, event):
            if not event.is_directory:
                self._reindex(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                self._reindex(event.src_path)

    observer = Observer()
    handler = SourceFileHandler()
    for watched in WATCHED_DIRS:
        watch_path = PROJECT_ROOT / watched
        if watch_path.exists():
            observer.schedule(handler, str(watch_path), recursive=True)
            print(f"Watching {watch_path}/")

    observer.start()
    print("Index watch mode active. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nWatch mode stopped.")
    observer.join()


def cmd_status():
    """Show current index statistics."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()
    
    console.print("\n[bold]Codevira Context Engine — Health Dashboard[/bold]\n")

    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
        count = collection.count()
        
        table = Table(show_header=False, box=None)
        table.add_column("Property", style="bold cyan")
        table.add_column("Value")
        
        table.add_row("Collection", COLLECTION_NAME)
        table.add_row("Location", str(INDEX_DIR))
        table.add_row("Total chunks", f"[bold green]{count}[/bold green]")

        ts = _read_timestamp()
        if ts:
            from datetime import datetime
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            table.add_row("Last indexed", dt)
            
            console.print(Panel(table, title="Index Information", expand=False, border_style="cyan"))

            changed = _get_changed_files()
            if changed:
                console.print(f"\n[bold yellow]Files changed since last index[/bold yellow] ({len(changed)} file(s) pending):")
                for f in changed[:10]:
                    console.print(f"  [dim]•[/dim] {f}")
                if len(changed) > 10:
                    console.print(f"  [dim]... and {len(changed) - 10} more[/dim]")
                console.print("\n[dim]Run `codevira index` to sync changes.[/dim]")
            else:
                console.print("\n[bold green]✓ Index is fully up to date.[/bold green]")
        else:
            table.add_row("Last indexed", "[yellow]unknown (no .last_indexed file)[/yellow]")
            console.print(Panel(table, title="Index Information", expand=False, border_style="cyan"))
    except Exception:
        console.print(f"[red]No index found at {INDEX_DIR}[/red]")
        console.print("Run [bold cyan]`codevira index --full`[/bold cyan] to create one.")


def cmd_generate_graph():
    """
    Auto-generate context graph YAML nodes for all source files.

    Safe merge: existing enriched nodes are NEVER overwritten.
    New files get auto-generated stubs marked with auto_generated: true.
    """
    from indexer.graph_generator import generate_graph_yaml

    print(f"Generating context graph nodes from {PROJECT_ROOT}")
    result = generate_graph_yaml(str(PROJECT_ROOT))

    print(f"  Files scanned: {result['files_processed']}")
    print(f"  Nodes added:   {result['nodes_added']}")
    print(f"  Nodes skipped (already exist): {result['nodes_skipped']}")
    if result["files_added"]:
        print(f"\n  New nodes added to graph:")
        for fp in result["files_added"][:20]:
            print(f"    + {fp}")
        if len(result["files_added"]) > 20:
            print(f"    ... and {len(result['files_added']) - 20} more")
    print(f"\nGraph generation complete. Review auto_generated: true nodes and enrich with:")
    print("  - rules (business invariants)")
    print("  - do_not_revert: true (protected decisions)")
    print("  - refined edge types (depends_on → consumed_by where appropriate)")


def cmd_bootstrap_roadmap():
    """
    Bootstrap a roadmap.yaml stub from git history.
    Only creates the file if it does not already exist — never overwrites.
    """
    from indexer.graph_generator import generate_roadmap_stub

    print(f"Bootstrapping roadmap stub from git history at {PROJECT_ROOT}")
    result = generate_roadmap_stub(str(PROJECT_ROOT))

    if result["created"]:
        print(f"  Created: {result['path']}")
        print(f"  Phases from git history: {result['completed_phases_from_git']}")
        print(f"  Current phase stub: Phase {result['current_phase']} — Getting Started")
        print(f"\nNext steps:")
        print("  1. Edit .codevira/roadmap.yaml — fill in actual phase names and decisions")
        print("  2. Run: get_roadmap() in MCP to verify")
    else:
        print(f"  Skipped: {result.get('reason', 'already exists')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Codebase Indexer")
    parser.add_argument("--full", action="store_true", help="Full rebuild from scratch")
    parser.add_argument("--status", action="store_true", help="Show index stats and stale files")
    parser.add_argument("--watch", action="store_true", help="Watch for file changes and auto-reindex")
    parser.add_argument("--quiet", action="store_true", help="Suppress output (used by git hook)")
    parser.add_argument(
        "--generate-graph",
        action="store_true",
        help="Auto-generate graph YAML nodes for all source files (safe merge, never overwrites)",
    )
    parser.add_argument(
        "--bootstrap-roadmap",
        action="store_true",
        help="Bootstrap roadmap.yaml stub from git history (only if file does not exist)",
    )
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
