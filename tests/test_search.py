import pytest
from mcp_server.tools.search import search_decisions, write_session_log
from indexer.sqlite_graph import SQLiteGraph
from mcp_server.paths import get_data_dir

@pytest.fixture
def mock_db():
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    db.log_session("test-1", "Test summary", "phase 1", [{"file_path": "a.py", "decision": "Made a decision"}])
    yield db
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = 'test-1'")
    db.close()

def test_search_decisions(mock_db):
    res = search_decisions("Made a decision")
    assert len(res["results"]) > 0
    assert "Made a decision" in [r["decision"] for r in res["results"]]
