"""
Tests for mcp_server/tools/search.py -- search, session logs, history, and indexing.

Covers:
  - search_codebase: ChromaDB not installed, ChromaDB available, auto-init indexing, empty results
  - write_session_log: logs to SQLite, triggers global export
  - search_decisions: matching decisions, session_id filter, empty results
  - get_history: file-specific decision history, file with no history
  - refresh_index: targeted mode, incremental mode

Uses the `project_env` fixture from conftest.py which provides
(project_root, data_dir, db).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from mcp_server.tools.search import (
    search_codebase,
    write_session_log,
    search_decisions,
    get_history,
    refresh_index,
)


# ---------------------------------------------------------------------------
# search_codebase
# ---------------------------------------------------------------------------

class TestSearchCodebase:
    def test_chromadb_unavailable_falls_back_gracefully(self, project_env):
        """P0-A (rc.5): when ChromaDB returns (None, None), search_codebase
        no longer errors with a misleading 'reinstall' hint. It returns a
        structural-fallback shape with `matches` (possibly empty), a
        `warning` explaining the fallback, and a `fix_command` with the
        ACTUAL fix (`codevira index`), not a reinstall instruction.
        """
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(None, None)):
            result = search_codebase("find auth logic")
        # No 'error' key — graceful fallback.
        assert "error" not in result
        # Matches may be empty (graph DB doesn't exist in project_env) but
        # the SHAPE must be search-result-compatible.
        assert "matches" in result
        assert isinstance(result["matches"], list)
        # Warning + correct fix command.
        assert "warning" in result
        assert "codevira index" in result.get("fix_command", "")
        # The bad rc.4 hint about reinstalling MUST NOT appear.
        all_text = " ".join(str(v) for v in result.values())
        assert "Reinstall codevira" not in all_text
        assert "pip install --upgrade codevira" not in all_text

    def test_chromadb_available_returns_matches(self, project_env):
        """When ChromaDB is available and has results, returns matches with scores."""
        mock_client = MagicMock()
        mock_embed = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_collection.return_value = mock_collection
        mock_collection.query.return_value = {
            "documents": [["def authenticate(user):\n    pass"]],
            "metadatas": [[{
                "file_path": "src/auth.py",
                "chunk_type": "function",
                "name": "authenticate",
            }]],
            "distances": [[0.15]],
        }
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(mock_client, mock_embed)):
            result = search_codebase("authentication", top_k=3)
        assert "matches" in result
        assert len(result["matches"]) == 1
        match = result["matches"][0]
        assert match["file_path"] == "src/auth.py"
        assert match["name"] == "authenticate"
        assert match["relevance_score"] == 0.85  # 1.0 - 0.15
        mock_collection.query.assert_called_once_with(query_texts=["authentication"], n_results=3)

    def test_auto_init_indexing_returns_status(self, project_env):
        """When ChromaDB is unavailable but auto-init is running, returns indexing status."""
        mock_progress = {"status": "indexing", "progress": 42}
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(None, None)), \
             patch("mcp_server.auto_init.get_init_progress", return_value=mock_progress):
            result = search_codebase("anything")
        assert result.get("status") == "indexing"
        assert "background" in result.get("message", "").lower()

    def test_auto_init_initializing_returns_status(self, project_env):
        """When auto-init status is 'initializing', returns indexing message."""
        mock_progress = {"status": "initializing", "progress": 0}
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(None, None)), \
             patch("mcp_server.auto_init.get_init_progress", return_value=mock_progress):
            result = search_codebase("something")
        assert result.get("status") == "indexing"

    def test_empty_results_returns_empty_matches(self, project_env):
        """When ChromaDB returns no documents, returns empty matches list."""
        mock_client = MagicMock()
        mock_embed = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_collection.return_value = mock_collection
        mock_collection.query.return_value = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(mock_client, mock_embed)):
            result = search_codebase("nonexistent concept")
        assert result["matches"] == []
        assert result["query"] == "nonexistent concept"

    def test_chromadb_exception_returns_error(self, project_env):
        """When ChromaDB query raises, returns error instead of crashing."""
        mock_client = MagicMock()
        mock_embed = MagicMock()
        mock_client.get_collection.side_effect = Exception("collection missing")
        with patch("mcp_server.tools.search._get_chroma_client", return_value=(mock_client, mock_embed)):
            result = search_codebase("anything")
        assert "error" in result
        assert "collection missing" in result["error"]


# ---------------------------------------------------------------------------
# write_session_log
# ---------------------------------------------------------------------------

class TestWriteSessionLog:
    def test_logs_to_sqlite(self, project_env):
        """write_session_log saves session data to the SQLite database."""
        _project, data_dir, db = project_env
        db_path = data_dir / "graph" / "graph.db"
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = write_session_log(
                session_id="sess-001",
                task="Implement feature X",
                phase="3",
                files_changed=["src/x.py"],
                decisions=[{"file_path": "src/x.py", "decision": "Use factory pattern", "context": "design"}],
                next_steps=["Add tests"],
            )
        assert "status" in result
        assert "sess-001" in result["status"]
        # Verify the session was actually stored by opening a fresh connection
        # (the original db was closed by write_session_log)
        from indexer.sqlite_graph import SQLiteGraph
        verify_db = SQLiteGraph(db_path)
        cur = verify_db.conn.execute("SELECT summary FROM sessions WHERE session_id = ?", ("sess-001",))
        row = cur.fetchone()
        assert row is not None
        verify_db.close()

    def test_triggers_global_export(self, project_env):
        """write_session_log calls export_project_to_global after logging."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db), \
             patch("mcp_server.global_sync.export_project_to_global") as mock_export:
            write_session_log(
                session_id="sess-export",
                task="Test export",
                phase="1",
                files_changed=[],
                decisions=[],
                next_steps=[],
            )
        mock_export.assert_called_once()

    def test_global_export_failure_does_not_crash(self, project_env):
        """If global export fails, write_session_log still succeeds."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db), \
             patch("mcp_server.global_sync.export_project_to_global", side_effect=RuntimeError("export boom")):
            result = write_session_log(
                session_id="sess-safe",
                task="Test safety",
                phase="1",
                files_changed=[],
                decisions=[],
                next_steps=[],
            )
        assert "status" in result
        assert "sess-safe" in result["status"]


# ---------------------------------------------------------------------------
# search_decisions
# ---------------------------------------------------------------------------

class TestSearchDecisions:
    def test_returns_matching_decisions(self, project_env):
        """search_decisions finds decisions matching the query."""
        _project, _data_dir, db = project_env
        # Seed some decisions
        db.log_session("sd-1", "Auth work", "phase-1", [
            {"file_path": "src/auth.py", "decision": "Use JWT tokens", "context": "auth design"},
        ])
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = search_decisions("JWT tokens")
        assert "results" in result
        assert len(result["results"]) > 0
        decisions = [r["decision"] for r in result["results"]]
        assert any("JWT" in d for d in decisions)

    def test_with_session_id_filter(self, project_env):
        """search_decisions with session_id filters to only that session's decisions."""
        _project, data_dir, db = project_env
        db_path = data_dir / "graph" / "graph.db"
        db.log_session("sd-a", "Task A", "1", [
            {"file_path": "a.py", "decision": "Use retry logic", "context": "reliability"},
        ])
        db.log_session("sd-b", "Task B", "2", [
            {"file_path": "b.py", "decision": "Use retry mechanism", "context": "reliability"},
        ])

        from indexer.sqlite_graph import SQLiteGraph

        # Fetch all retry decisions (should get 2)
        # search_decisions calls db.close(), so provide fresh instances each time
        db1 = SQLiteGraph(db_path)
        with patch("mcp_server.tools.search._get_db", return_value=db1):
            result_all = search_decisions("retry")
        assert len(result_all["results"]) == 2

        # Fetch with session_id filter (should get 1)
        db2 = SQLiteGraph(db_path)
        with patch("mcp_server.tools.search._get_db", return_value=db2):
            result_filtered = search_decisions("retry", session_id="sd-a")
        assert len(result_filtered["results"]) == 1
        assert result_filtered["results"][0]["decision"] == "Use retry logic"

    def test_no_results_returns_empty_list(self, project_env):
        """search_decisions with no matches returns empty results."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = search_decisions("zzz_nonexistent_query_zzz")
        assert result["results"] == []

    def test_returns_query_in_response(self, project_env):
        """Response includes the original query string."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = search_decisions("architecture")
        assert result["query"] == "architecture"

    def test_returns_hint(self, project_env):
        """Response includes a helpful hint."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = search_decisions("anything")
        assert "hint" in result


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------

class TestGetHistory:
    def test_returns_file_specific_history(self, project_env):
        """get_history returns decisions related to the specified file."""
        _project, _data_dir, db = project_env
        db.log_session("hist-1", "Build API", "1", [
            {"file_path": "src/api.py", "decision": "Use REST pattern", "context": "design"},
            {"file_path": "src/db.py", "decision": "Use SQLite", "context": "storage"},
        ])
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = get_history("src/api.py")
        assert result["file_path"] == "src/api.py"
        assert len(result["history"]) > 0
        # At least one decision should relate to src/api.py
        found = any("REST" in h["decision"] for h in result["history"])
        assert found

    def test_file_with_no_history_returns_empty(self, project_env):
        """get_history for a file with no decisions returns empty history."""
        _project, _data_dir, db = project_env
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = get_history("nonexistent/file.py")
        assert result["file_path"] == "nonexistent/file.py"
        assert result["history"] == []


# ---------------------------------------------------------------------------
# refresh_index
# ---------------------------------------------------------------------------

class TestRefreshIndex:
    """refresh_index returns immediately; actual work runs in background thread."""

    def test_targeted_mode_with_specific_files(self, project_env):
        """refresh_index with file_paths returns targeted mode result."""
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            result = refresh_index(["src/api.py", "src/db.py"])
        assert result["mode"] == "targeted"
        assert result["file_paths"] == ["src/api.py", "src/db.py"]
        assert "started" in result["status"].lower() or "background" in result["status"].lower()

    def test_incremental_mode_no_files(self, project_env):
        """refresh_index with empty file list uses incremental mode."""
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            result = refresh_index([])
        assert result["mode"] == "incremental"
        assert "started" in result["status"].lower() or "background" in result["status"].lower()

    def test_targeted_mode_count_matches_files(self, project_env):
        """In targeted mode, result includes file_paths."""
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            result = refresh_index(["a.py", "b.py", "c.py"])
        assert len(result["file_paths"]) == 3

    def test_returns_immediately(self, project_env):
        """refresh_index should return fast — not block on actual indexing."""
        import time
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            start = time.monotonic()
            result = refresh_index([])
            elapsed = time.monotonic() - start
        # Should return in well under 1 second even though background work may take minutes
        assert elapsed < 1.0
        assert "mode" in result

    def test_graph_only_when_no_chromadb(self, project_env):
        """Without chromadb, note mentions graph-only mode."""
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            result = refresh_index(["src/app.py"])
        assert "not installed" in result["note"].lower() or "graph only" in result["note"].lower()


# ---------------------------------------------------------------------------
# Ported from tests/test_search.py: search_decisions via get_data_dir
# ---------------------------------------------------------------------------

class TestSearchDecisionsPorted:
    def test_search_decisions_via_direct_db(self, project_env):
        """Ported from test_search.py: search_decisions finds seeded decisions."""
        _project, data_dir, db = project_env
        db.log_session(
            "test-1", "Test summary", "phase 1",
            [{"file_path": "a.py", "decision": "Made a decision"}],
        )
        with patch("mcp_server.tools.search._get_db", return_value=db):
            res = search_decisions("Made a decision")
        assert len(res["results"]) > 0
        assert "Made a decision" in [r["decision"] for r in res["results"]]


# ---------------------------------------------------------------------------
# New: search_codebase with layer filter parameter
# ---------------------------------------------------------------------------

class TestSearchCodebaseLayerFilter:
    def test_search_codebase_with_layer_filter(self, project_env):
        """search_codebase passes query to ChromaDB; layer filtering is client-side."""
        mock_client = MagicMock()
        mock_embed = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_collection.return_value = mock_collection
        mock_collection.query.return_value = {
            "documents": [["def handler():\n    pass", "class Util:\n    pass"]],
            "metadatas": [[
                {"file_path": "src/api/handler.py", "chunk_type": "function",
                 "name": "handler"},
                {"file_path": "src/utils/util.py", "chunk_type": "class",
                 "name": "Util"},
            ]],
            "distances": [[0.1, 0.3]],
        }
        with patch("mcp_server.tools.search._get_chroma_client",
                    return_value=(mock_client, mock_embed)):
            result = search_codebase("handler", top_k=5)
        assert "matches" in result
        assert len(result["matches"]) == 2
        # Verify the first match has a higher relevance score
        assert result["matches"][0]["relevance_score"] > result["matches"][1]["relevance_score"]


# ---------------------------------------------------------------------------
# New: get_history with SQL injection attempt
# ---------------------------------------------------------------------------

class TestGetHistorySQLInjection:
    def test_sql_injection_in_file_path_is_safe(self, project_env):
        """get_history uses parameterized queries, so SQL injection strings are harmless."""
        _project, _data_dir, db = project_env
        malicious_path = "'; DROP TABLE sessions; --"
        with patch("mcp_server.tools.search._get_db", return_value=db):
            result = get_history(malicious_path)
        # Should not crash, should just return empty history
        assert result["file_path"] == malicious_path
        assert result["history"] == []
        # Verify the sessions table still exists by querying it
        from indexer.sqlite_graph import SQLiteGraph
        verify_db = SQLiteGraph(_data_dir / "graph" / "graph.db")
        cur = verify_db.conn.execute("SELECT COUNT(*) FROM sessions")
        row = cur.fetchone()
        assert row is not None
        verify_db.close()


# ---------------------------------------------------------------------------
# New: refresh_index with empty file list -> incremental mode
# ---------------------------------------------------------------------------

class TestRefreshIndexEmptyList:
    def test_empty_file_list_uses_incremental_mode(self, project_env):
        """refresh_index([]) should use incremental mode."""
        with patch("indexer.index_codebase._check_search_deps", return_value=False):
            result = refresh_index([])
        assert result["mode"] == "incremental"
        assert "started" in result["status"].lower() or "background" in result["status"].lower()


# ---------------------------------------------------------------------------
# _get_chroma_client (lines 16-31 of tools/search.py)
# ---------------------------------------------------------------------------

class TestGetChromaClientLines:
    def test_codeindex_dir_missing_returns_none(self, project_env):
        """When chromadb is importable but codeindex dir is missing, returns (None, None)."""
        import sys
        _project, data_dir, _db = project_env
        # Ensure codeindex dir does NOT exist
        codeindex = data_dir / "codeindex"
        if codeindex.exists():
            import shutil
            shutil.rmtree(str(codeindex))

        mock_chromadb = MagicMock()
        mock_utils = MagicMock()
        mock_embed_fns = MagicMock()

        original_mods = {
            "chromadb": sys.modules.get("chromadb"),
            "chromadb.utils": sys.modules.get("chromadb.utils"),
            "chromadb.utils.embedding_functions": sys.modules.get("chromadb.utils.embedding_functions"),
        }
        sys.modules["chromadb"] = mock_chromadb
        sys.modules["chromadb.utils"] = mock_utils
        sys.modules["chromadb.utils.embedding_functions"] = mock_embed_fns
        try:
            from mcp_server.tools.search import _get_chroma_client
            client, embed_fn = _get_chroma_client()
            assert client is None
            assert embed_fn is None
        finally:
            for k, v in original_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_with_codeindex_returns_client(self, project_env):
        """When chromadb and codeindex dir both exist, returns (client, embed_fn)."""
        import sys
        _project, data_dir, _db = project_env
        codeindex = data_dir / "codeindex"
        codeindex.mkdir(exist_ok=True)

        mock_client = MagicMock()
        mock_embed_fn = MagicMock()
        mock_embed_fn_cls = MagicMock(return_value=mock_embed_fn)

        mock_embed_fns = MagicMock()
        mock_embed_fns.SentenceTransformerEmbeddingFunction = mock_embed_fn_cls

        mock_chromadb = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client

        mock_utils = MagicMock()
        mock_utils.embedding_functions = mock_embed_fns

        original_mods = {
            "chromadb": sys.modules.get("chromadb"),
            "chromadb.utils": sys.modules.get("chromadb.utils"),
            "chromadb.utils.embedding_functions": sys.modules.get("chromadb.utils.embedding_functions"),
        }
        sys.modules["chromadb"] = mock_chromadb
        sys.modules["chromadb.utils"] = mock_utils
        sys.modules["chromadb.utils.embedding_functions"] = mock_embed_fns
        try:
            from mcp_server.tools.search import _get_chroma_client
            client, embed_fn = _get_chroma_client()
            assert client is mock_client
            assert embed_fn is mock_embed_fn
        finally:
            for k, v in original_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
