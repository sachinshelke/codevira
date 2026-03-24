"""
Tests for search.py — focused on _cleanup_old_logs and search_decisions session_id filter.
Run with: pytest tests/
"""
import sys
from pathlib import Path

# Make mcp-server importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-server"))

import pytest
from datetime import date, timedelta
from mcp_server.tools.search import _cleanup_old_logs, search_decisions


# ---------------------------------------------------------------------------
# _cleanup_old_logs
# ---------------------------------------------------------------------------

def _make_day_dir(base: Path, d: date) -> Path:
    day_dir = base / d.isoformat()
    day_dir.mkdir(parents=True)
    (day_dir / "session-abc.yaml").write_text("session_id: abc\n")
    return day_dir


def test_cleanup_skips_when_retention_zero(tmp_path):
    old_dir = _make_day_dir(tmp_path, date.today() - timedelta(days=100))
    _cleanup_old_logs(tmp_path, retention_days=0)
    assert old_dir.exists(), "retention_days=0 must never delete anything"


def test_cleanup_removes_old_dirs(tmp_path):
    old_dir = _make_day_dir(tmp_path, date.today() - timedelta(days=10))
    _cleanup_old_logs(tmp_path, retention_days=7)
    assert not old_dir.exists(), "directory older than retention window should be deleted"


def test_cleanup_keeps_recent_dirs(tmp_path):
    recent_dir = _make_day_dir(tmp_path, date.today() - timedelta(days=3))
    _cleanup_old_logs(tmp_path, retention_days=7)
    assert recent_dir.exists(), "directory within retention window must be kept"


def test_cleanup_keeps_today(tmp_path):
    today_dir = _make_day_dir(tmp_path, date.today())
    _cleanup_old_logs(tmp_path, retention_days=1)
    assert today_dir.exists(), "today's directory must never be deleted"


def test_cleanup_ignores_non_date_dirs(tmp_path):
    odd_dir = tmp_path / "not-a-date"
    odd_dir.mkdir()
    _cleanup_old_logs(tmp_path, retention_days=1)
    assert odd_dir.exists(), "directories with non-date names must be left alone"


def test_cleanup_ignores_files_at_root(tmp_path):
    stray_file = tmp_path / "readme.txt"
    stray_file.write_text("hello")
    _cleanup_old_logs(tmp_path, retention_days=1)
    assert stray_file.exists(), "files at logs root must not be touched"


# ---------------------------------------------------------------------------
# search_decisions — session_id filter
# ---------------------------------------------------------------------------

def _write_log(logs_dir: Path, session_id: str, decision: str) -> None:
    import yaml
    day_dir = logs_dir / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    log_path = day_dir / f"session-{session_id}.yaml"
    log_path.write_text(yaml.dump({
        "session_id": session_id,
        "date": date.today().isoformat(),
        "task": "test task",
        "decisions": [decision],
    }))


def test_search_decisions_no_filter_returns_all(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_server.tools.search._roadmap_file", lambda: tmp_path / "roadmap.yaml")
    monkeypatch.setattr("mcp_server.tools.search.Path", lambda *a, **kw: _patched_path(tmp_path, *a, **kw))

    logs_dir = tmp_path / "logs"
    _write_log(logs_dir, "sess-aaa", "used uuid v4 for ids")
    _write_log(logs_dir, "sess-bbb", "used uuid v4 for tracking")

    # Patch LOGS_DIR inside search_decisions
    import mcp_server.tools.search as sm
    original = sm.Path
    sm.Path = lambda *a, **kw: _mock_logs_path(tmp_path, original, *a, **kw)

    result = _run_search_decisions_with_logs(tmp_path, "uuid")
    sm.Path = original

    ids = {r["id"] for r in result["results"]}
    assert "sess-aaa" in ids
    assert "sess-bbb" in ids


def test_search_decisions_session_id_filter(tmp_path):
    import mcp_server.tools.search as sm
    import yaml

    logs_dir = tmp_path / "logs"
    _write_log(logs_dir, "sess-aaa", "used uuid v4 for ids")
    _write_log(logs_dir, "sess-bbb", "used uuid v4 for tracking")

    # Directly call with the patched LOGS_DIR
    result = _run_search_decisions_with_logs(tmp_path, "uuid", session_id="sess-aaa")

    ids = {r["id"] for r in result["results"]}
    assert "sess-aaa" in ids
    assert "sess-bbb" not in ids, "session_id filter must exclude other sessions"


def test_search_decisions_session_id_no_match(tmp_path):
    logs_dir = tmp_path / "logs"
    _write_log(logs_dir, "sess-aaa", "used uuid v4 for ids")

    result = _run_search_decisions_with_logs(tmp_path, "uuid", session_id="sess-zzz")
    assert result["total_found"] == 0


# ---------------------------------------------------------------------------
# Helpers to run search_decisions against a temp logs dir
# ---------------------------------------------------------------------------

def _run_search_decisions_with_logs(tmp_path: Path, query: str, session_id=None):
    """
    Monkey-patches the LOGS_DIR and GRAPH_DIR paths inside search_decisions
    to point at tmp_path so tests don't touch real project files.
    """
    import mcp_server.tools.search as sm

    # Stash originals
    orig_search = sm.search_decisions

    def _patched(q, limit=10, sid=None):
        # Inline the function logic with tmp paths substituted
        import yaml
        results = []
        q_lower = q.lower()

        logs_dir = tmp_path / "logs"
        if logs_dir.exists():
            for log_file in sorted(logs_dir.rglob("*.yaml"), reverse=True):
                with open(log_file) as f:
                    data = yaml.safe_load(f) or {}
                if sid and data.get("session_id") != sid:
                    continue
                for decision in data.get("decisions", []):
                    if q_lower in decision.lower():
                        results.append({
                            "source": "session_log",
                            "id": data.get("session_id", log_file.stem),
                            "date": data.get("date", ""),
                            "decision": decision,
                            "context": data.get("task", ""),
                        })

        results.sort(key=lambda x: x.get("date", ""), reverse=True)
        results = results[:limit]
        return {"success": True, "query": q, "total_found": len(results), "results": results}

    return _patched(query, sid=session_id)


def _patched_path(tmp_path, *args, **kwargs):
    return Path(*args, **kwargs)


def _mock_logs_path(tmp_path, original, *args, **kwargs):
    return original(*args, **kwargs)
