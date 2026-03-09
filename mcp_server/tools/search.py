"""
MCP tool for semantic code search via local ChromaDB.
Searches .codevira/codeindex/ — no server, no credentials needed.
"""
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp_server.paths import get_data_dir


def _index_dir() -> Path:
    return get_data_dir() / "codeindex"


# Load collection name from config (default: agent_codebase)
def _get_collection_name() -> str:
    config_path = get_data_dir() / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("project", {}).get("collection_name", "agent_codebase")
        except Exception:
            pass
    return "agent_codebase"


def _roadmap_file() -> Path:
    return get_data_dir() / "roadmap.yaml"


def search_codebase(query: str, limit: int = 5, layer: str | None = None) -> dict[str, Any]:
    """
    Semantic search over the codebase index.
    Returns relevant code chunks — functions, classes, or module docs.

    Args:
        query: Natural language or code query
        limit: Number of results (default 5, max 10)
        layer: Optional filter by layer

    Returns chunks with file_path, function_name, source snippet, and relevance score.
    """
    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except ImportError:
        return {
            "success": False,
            "error": "chromadb not installed. Run: pip install codevira-mcp",
        }

    index_dir = _index_dir()
    collection_name = _get_collection_name()
    if not index_dir.exists() or not any(index_dir.iterdir()):
        return {
            "success": False,
            "error": "Code index not found. Run: codevira-mcp index --full",
        }

    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    client = chromadb.PersistentClient(path=str(index_dir))

    try:
        collection = client.get_collection(collection_name, embedding_function=embed_fn)
    except Exception:
        return {
            "success": False,
            "error": f"Collection '{collection_name}' not found. Run: codevira-mcp index --full",
        }

    limit = min(limit, 10)
    where = {"layer": layer} if layer else None

    results = collection.query(
        query_texts=[query],
        n_results=limit,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    )):
        # Convert cosine distance to similarity score
        score = round(1 - dist, 3)
        # Extract source snippet (first 300 chars of document after header)
        lines = doc.split("\n")
        snippet = "\n".join(lines[2:12]) if len(lines) > 2 else doc[:300]

        chunks.append({
            "rank": i + 1,
            "score": score,
            "file_path": meta.get("file_path", ""),
            "chunk_type": meta.get("chunk_type", ""),
            "name": meta.get("chunk_name", ""),
            "layer": meta.get("layer", ""),
            "line_range": f"{meta.get('start_line', '?')}–{meta.get('end_line', '?')}",
            "docstring": meta.get("docstring", "")[:200],
            "snippet": snippet,
        })

    return {
        "success": True,
        "query": query,
        "results": chunks,
        "total_in_index": collection.count(),
    }


def search_decisions(query: str, limit: int = 10, session_id: str | None = None) -> dict[str, Any]:
    """
    Search past decisions across all completed changesets, roadmap phases, and session logs.
    Gives agents institutional memory — answers "has anyone decided this before?"

    Args:
        query: Keywords to search for (e.g. "threshold", "uuid", "retry")
        limit: Max results to return (default 10)
        session_id: Optional — filter session log results to a specific session only

    Returns:
        Matching decisions with source (changeset/phase/log), date, context.
    """
    GRAPH_DIR = get_data_dir() / "graph"
    LOGS_DIR = get_data_dir() / "logs"

    try:
        import yaml
    except ImportError:
        return {"success": False, "error": "pyyaml not installed"}

    q = query.lower()
    results = []

    # Search completed changesets
    for cs_file in sorted((GRAPH_DIR / "changesets").glob("*.yaml")):
        try:
            with open(cs_file) as f:
                data = yaml.safe_load(f) or {}
            if data.get("status") != "complete":
                continue
            for decision in data.get("decisions", []):
                if q in decision.lower():
                    results.append({
                        "source": "changeset",
                        "id": data.get("id"),
                        "date": data.get("completed", data.get("created", "")),
                        "decision": decision,
                        "context": data.get("description", ""),
                    })
        except Exception:
            pass

    # Search completed roadmap phases
    if _roadmap_file().exists():
        try:
            with open(_roadmap_file()) as f:
                roadmap = yaml.safe_load(f) or {}
            for phase in roadmap.get("completed_phases", []):
                for decision in phase.get("key_decisions", []):
                    if q in decision.lower():
                        results.append({
                            "source": "phase",
                            "id": f"Phase {phase.get('phase')} — {phase.get('name')}",
                            "date": phase.get("completed", ""),
                            "decision": decision,
                            "context": phase.get("name", ""),
                        })
        except Exception:
            pass

    # Search session logs
    if LOGS_DIR.exists():
        for log_file in sorted(LOGS_DIR.rglob("*.yaml"), reverse=True):
            try:
                with open(log_file) as f:
                    data = yaml.safe_load(f) or {}
                if session_id and data.get("session_id") != session_id:
                    continue
                for decision in data.get("decisions", []):
                    if q in decision.lower():
                        results.append({
                            "source": "session_log",
                            "id": data.get("session_id", log_file.stem),
                            "date": data.get("date", ""),
                            "decision": decision,
                            "context": data.get("task", ""),
                        })
            except Exception:
                pass

    # Sort by date descending, apply limit
    results.sort(key=lambda x: x.get("date", ""), reverse=True)
    results = results[:limit]

    return {
        "success": True,
        "query": query,
        "total_found": len(results),
        "results": results,
        "hint": "No results? Try broader keywords — decisions are stored as written by agents." if not results else None,
    }


def get_history(file_path: str, n: int = 5) -> dict[str, Any]:
    """
    Get the last N git commits that touched a file.
    Links graph node's last_changed_by to actual commits with diffs summary.

    Args:
        file_path: Relative file path
        n: Number of commits to return (default 5, max 20)

    Returns:
        List of commits: hash, date, author, message.
    """
    PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
    n = min(n, 20)

    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--follow",
             "--pretty=format:%H|%ai|%an|%s", "--", file_path],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr or "git log failed"}

        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                short_hash, date, author, message = parts
                commits.append({
                    "hash": short_hash[:8],
                    "date": date[:19],
                    "author": author,
                    "message": message,
                })

        if not commits:
            return {
                "success": True,
                "file_path": file_path,
                "commits": [],
                "hint": "No git history found — file may be new or untracked.",
            }

        return {
            "success": True,
            "file_path": file_path,
            "commits": commits,
            "total_shown": len(commits),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_retention_days() -> int:
    config_path = Path(__file__).parent.parent.parent / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return int(cfg.get("logs", {}).get("retention_days", 0))
        except Exception:
            pass
    return 0


def _cleanup_old_logs(logs_dir: Path, retention_days: int) -> None:
    if retention_days <= 0:
        return
    from datetime import date, timedelta
    import shutil
    cutoff = date.today() - timedelta(days=retention_days)
    for day_dir in logs_dir.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            dir_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dir_date < cutoff:
            shutil.rmtree(day_dir)


def write_session_log(
    session_id: str,
    task: str,
    task_type: str,
    files_changed: list[str],
    decisions: list[str],
    phase: int | str,
    next_action: str,
    agents_invoked: list[str] | None = None,
    tests_run: list[str] | None = None,
    tests_passed: bool | None = None,
    build_clean: bool | None = None,
    changeset_id: str | None = None,
) -> dict[str, Any]:
    """
    Write a structured session log to .agents/logs/YYYY-MM-DD/session-{id}.yaml.
    Called by the Documenter agent at the end of every session.

    Args:
        session_id: Short ID (first 8 chars of a UUID or descriptive slug)
        task: The original developer prompt
        task_type: small_fix | medium_change | large_change
        files_changed: List of files modified in this session
        decisions: Key decisions made (1 sentence each)
        phase: Current phase number
        next_action: What the next agent should do
        agents_invoked: Which agents ran (optional)
        tests_run: Test files executed (optional)
        tests_passed: Overall test result (optional)
        build_clean: Whether linter/type-checker passed (optional)
        changeset_id: Associated changeset ID (optional)

    Returns:
        success, log_path written.
    """
    from datetime import date

    try:
        import yaml
    except ImportError:
        return {"success": False, "error": "pyyaml not installed"}

    LOGS_DIR = get_data_dir() / "logs"
    today = date.today().isoformat()
    log_dir = LOGS_DIR / today
    log_dir.mkdir(parents=True, exist_ok=True)

    log_data: dict[str, Any] = {
        "session_id": session_id,
        "date": today,
        "phase": phase,
        "task": task,
        "task_type": task_type,
        "files_changed": files_changed,
        "decisions": decisions,
        "next_action": next_action,
    }
    if agents_invoked:
        log_data["agents_invoked"] = agents_invoked
    if tests_run:
        log_data["tests_run"] = tests_run
    if tests_passed is not None:
        log_data["tests_passed"] = tests_passed
    if build_clean is not None:
        log_data["build_clean"] = build_clean
    if changeset_id:
        log_data["changeset_id"] = changeset_id

    log_path = log_dir / f"session-{session_id}.yaml"
    with open(log_path, "w") as f:
        yaml.dump(log_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    _cleanup_old_logs(LOGS_DIR, _get_retention_days())

    from mcp_server.paths import get_project_root
    return {
        "success": True,
        "log_path": str(log_path.relative_to(get_project_root())),
        "session_id": session_id,
    }


def refresh_index(file_paths: list[str] | None = None) -> dict[str, Any]:
    """
    Trigger an incremental reindex of changed files.

    Call this when get_node() returns index_status.stale=true, or before
    running search_codebase() on files you know have changed.

    Args:
        file_paths: Optional list of specific files to reindex. If omitted,
                    reindexes all files modified since the last index build.

    Returns:
        success, files_reindexed count, message.
    """
    if file_paths:
        try:
            from indexer.index_codebase import (
                _write_timestamp, _chunk_to_document,
                _get_chroma_client, _get_embedding_fn,
            )
            from indexer.chunker import chunk_file
            from mcp_server.paths import get_project_root

            try:
                import chromadb
                from chromadb.utils import embedding_functions
            except ImportError:
                return {"success": False, "error": "chromadb not installed"}

            client = _get_chroma_client()
            embed_fn = _get_embedding_fn()
            collection_name = _get_collection_name()
            project_root = get_project_root()

            try:
                collection = client.get_collection(collection_name, embedding_function=embed_fn)
            except Exception:
                return {"success": False, "error": "No index found. Run codevira-mcp index --full first."}

            updated = 0
            for rel_path in file_paths:
                abs_path = project_root / rel_path
                results = collection.get(where={"file_path": rel_path})
                if results["ids"]:
                    collection.delete(ids=results["ids"])
                if abs_path.exists():
                    chunks = chunk_file(str(abs_path), str(project_root))
                    if chunks:
                        ids, docs, metas = [], [], []
                        for chunk in chunks:
                            doc_id, document, metadata = _chunk_to_document(chunk)
                            ids.append(doc_id)
                            docs.append(document)
                            metas.append(metadata)
                        collection.upsert(ids=ids, documents=docs, metadatas=metas)
                        updated += 1

            _write_timestamp()
            return {
                "success": True,
                "files_reindexed": updated,
                "message": f"Reindexed {updated} file(s). Index is now current.",
            }

        except Exception as e:
            return {"success": False, "error": f"Targeted reindex failed: {e}"}

    else:
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "index", "--quiet"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr or result.stdout or "Indexer exited with non-zero status",
            }
        return {
            "success": True,
            "message": "Incremental reindex complete. Changed files are now indexed.",
            "detail": result.stdout.strip() or "No output (all up to date or --quiet mode)",
        }
