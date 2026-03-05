"""
Codebase Indexer — builds/updates ChromaDB code index for semantic search.

Usage:
  python index_codebase.py              # incremental (files changed since last index build)
  python index_codebase.py --full       # full rebuild from scratch
  python index_codebase.py --status     # show current index stats
  python index_codebase.py --watch      # watch for file changes and auto-reindex (dev mode)

Change detection:
  Incremental mode tracks changes using .agents/codeindex/.last_indexed timestamp file.
  Any configured file (default: .py) in watched_dirs modified after that timestamp gets re-indexed.
  This catches ALL changes: saved edits, staged files, and committed diffs alike.

Configuration:
  Copy config.example.yaml → .agents/config.yaml to set watched_dirs, language, etc.
  The index lives at .agents/codeindex/ and is git-ignored (auto-regenerated).
"""
import argparse
import os
import sys
import time
from pathlib import Path

# Locate project root (two levels up from this script: .agents/indexer/ → .agents/ → project root)
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
INDEX_DIR = SCRIPT_DIR.parent / "codeindex"
LAST_INDEXED_FILE = INDEX_DIR / ".last_indexed"


def _load_config() -> dict:
    """Load .agents/config.yaml if present, otherwise return empty dict."""
    config_path = SCRIPT_DIR.parent / "config.yaml"
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
        print("ERROR: chromadb not installed. Run: pip install -r .agents/requirements.txt")
        sys.exit(1)
    return chromadb.PersistentClient(path=str(INDEX_DIR))


def _get_embedding_fn():
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    except ImportError:
        print("ERROR: sentence-transformers not installed. Run: pip install -r .agents/requirements.txt")
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
    sys.path.insert(0, str(SCRIPT_DIR))
    from chunker import chunk_project

    print(f"Full rebuild of '{COLLECTION_NAME}' from {PROJECT_ROOT}")
    print(f"  Watching: {WATCHED_DIRS}")
    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()

    # Delete existing collection if present
    try:
        client.delete_collection(COLLECTION_NAME)
        print("  Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    chunks = chunk_project(str(PROJECT_ROOT))
    print(f"  Found {len(chunks)} chunks across project.")

    # Batch upsert (ChromaDB handles batches of ~5000)
    batch_size = 500
    ids, docs, metas = [], [], []
    for chunk in chunks:
        doc_id, document, metadata = _chunk_to_document(chunk)
        ids.append(doc_id)
        docs.append(document)
        metas.append(metadata)

    for i in range(0, len(ids), batch_size):
        collection.upsert(
            ids=ids[i:i + batch_size],
            documents=docs[i:i + batch_size],
            metadatas=metas[i:i + batch_size],
        )
        print(f"  Indexed {min(i + batch_size, len(ids))}/{len(ids)} chunks...")

    _write_timestamp()
    print(f"Full rebuild complete. {len(ids)} chunks indexed to {INDEX_DIR}")
    print(f"\nTo commit the updated index:")
    print(f"  git add .agents/codeindex/")
    print(f"  git commit -m 'chore(agents): refresh codebase index'")


def cmd_incremental(since: float | None = None, quiet: bool = False):
    """
    Incremental update: re-index files modified since last index build.

    Uses .last_indexed timestamp — catches all file saves, not just committed changes.
    Called automatically by the post-commit hook and watch mode.
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    from chunker import chunk_file

    baseline = since if since is not None else _read_timestamp()
    if baseline is None:
        print("No baseline found. Run --full to create the initial index.")
        sys.exit(1)

    changed = _get_changed_files(since=baseline)
    if not changed:
        if not quiet:
            print("No files changed since last index build. Index is up to date.")
        return 0

    if not quiet:
        print(f"Incremental update: {len(changed)} changed file(s)")

    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()

    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    except Exception:
        print("No existing index found. Run --full to create one.")
        sys.exit(1)

    updated = 0
    for rel_path in changed:
        abs_path = PROJECT_ROOT / rel_path
        if not abs_path.exists():
            # File deleted — remove its chunks
            results = collection.get(where={"file_path": rel_path})
            if results["ids"]:
                collection.delete(ids=results["ids"])
                if not quiet:
                    print(f"  Removed {len(results['ids'])} chunks for deleted {rel_path}")
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
            if not quiet:
                print(f"  Updated {len(chunks)} chunks for {rel_path}")

    _write_timestamp()
    if not quiet:
        print(f"Incremental update complete. {updated} file(s) re-indexed.")
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

    sys.path.insert(0, str(SCRIPT_DIR))
    from chunker import chunk_file

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
    client = _get_chroma_client()
    embed_fn = _get_embedding_fn()
    try:
        collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
        count = collection.count()
        print(f"Index: {COLLECTION_NAME}")
        print(f"Location: {INDEX_DIR}")
        print(f"Total chunks: {count}")

        ts = _read_timestamp()
        if ts:
            from datetime import datetime
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"Last indexed: {dt}")

            # Show files changed since last build
            changed = _get_changed_files()
            if changed:
                print(f"\nFiles changed since last index ({len(changed)} file(s) — run without flags to update):")
                for f in changed[:10]:
                    print(f"  {f}")
                if len(changed) > 10:
                    print(f"  ... and {len(changed) - 10} more")
            else:
                print("Index is up to date.")
        else:
            print("Last indexed: unknown (no .last_indexed file)")
    except Exception:
        print(f"No index found at {INDEX_DIR}. Run --full to create one.")


def cmd_generate_graph():
    """
    Auto-generate context graph YAML nodes for all source files.

    Safe merge: existing enriched nodes are NEVER overwritten.
    New files get auto-generated stubs marked with auto_generated: true.
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    from graph_generator import generate_graph_yaml

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
    sys.path.insert(0, str(SCRIPT_DIR))
    from graph_generator import generate_roadmap_stub

    print(f"Bootstrapping roadmap stub from git history at {PROJECT_ROOT}")
    result = generate_roadmap_stub(str(PROJECT_ROOT))

    if result["created"]:
        print(f"  Created: {result['path']}")
        print(f"  Phases from git history: {result['completed_phases_from_git']}")
        print(f"  Current phase stub: Phase {result['current_phase']} — Getting Started")
        print(f"\nNext steps:")
        print("  1. Edit .agents/roadmap.yaml — fill in actual phase names and decisions")
        print("  2. Run: get_roadmap() in MCP to verify")
    else:
        print(f"  Skipped: {result['reason']}")


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
