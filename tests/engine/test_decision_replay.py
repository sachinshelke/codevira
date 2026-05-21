"""
test_decision_replay.py — Hero 8 acceptance + behavioral + mutation tests.

Tier-0 + deep-audit from start (lessons #15-21):
  - Real DB integration via record_decision-equivalent + record_outcome
  - End-to-end through MCP read_resource handler (Bug-4 lesson)
  - HTML XSS probe (declared "renders decision text as HTML" must
    trace through html.escape — Bug-X-shape audit)
  - Empty-section probe for all 3 renderers (Lesson #19)
  - Content-verifying assertions on output (decision text appears,
    not just headers)
  - 10+ mutations from start
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from mcp_server.decision_replay import (
    build_timeline,
    render_terminal,
    render_markdown,
    render_html,
    _score,
    _truncate,
    _clamp_since_days,
    _clamp_limit,
    _EMPTY_PLACEHOLDER,
)


# =====================================================================
# Helpers + fixtures
# =====================================================================


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a fresh project with an initialised .codevira/ JSONL store."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)

    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()

    # Bootstrap the .codevira/ JSONL store so decisions_store can write to it.
    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()
    return project


def _plant_decision_with_outcomes(
    _ignored=None,
    *,
    file_path: str,
    decision: str,
    session_id: str = "s-test",
    session_summary: str = "test session",
    kept: int = 0,
    modified: int = 0,
    reverted: int = 0,
    locked: bool = False,
) -> str:
    """v2.2.0 JSONL planter (mirrors the v2.1.x SQL planter signature).

    The first positional arg is retained for back-compat with tests that
    used to pass a SQLiteGraph instance — it's ignored. Decisions land in
    .codevira/decisions.jsonl via the canonical decisions_store.record;
    outcomes are appended to outcomes.jsonl; sessions to sessions.jsonl.
    """
    from mcp_server.storage import decisions_store, jsonl_store, paths

    # Plant the session summary (writes once per session_id).
    sessions_path = paths.sessions_path()
    existing_sessions = jsonl_store.read_all(sessions_path)
    if not any(s.get("session_id") == session_id for s in existing_sessions):
        jsonl_store.append(
            sessions_path,
            {
                "id": f"S-{session_id}",
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "summary": session_summary,
            },
        )

    decision_id = decisions_store.record(
        decision,
        file_path=file_path,
        do_not_revert=locked,
        session_id=session_id,
    )

    # Append outcome rows so build_timeline aggregates them.
    outcomes_path = paths.outcomes_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    for outcome_type, count in (
        ("kept", kept),
        ("modified", modified),
        ("reverted", reverted),
    ):
        for _ in range(count):
            jsonl_store.append(
                outcomes_path,
                {
                    "ts": now_iso,
                    "decision_id": decision_id,
                    "outcome_type": outcome_type,
                    "delta_summary": f"test {outcome_type}",
                },
            )
    return decision_id


def _open_graph(project: Path):
    """Compatibility shim — returns a no-op context object for tests
    that still use the `with g: ...` pattern. The JSONL store needs no
    explicit open/close; this exists only to minimise diff in the
    planter call sites.
    """

    class _NoOpGraph:
        def close(self) -> None:
            pass

    return _NoOpGraph()


# =====================================================================
# Pure-function unit tests
# =====================================================================


class TestPureFunctions:
    def test_score_formula(self):
        assert _score(5, 0, 0) == 1.0
        assert _score(0, 0, 5) == 0.0
        assert _score(0, 5, 0) == 0.5
        assert _score(0, 0, 0) == 0.0  # safe div
        assert _score(-1, 0, 0) == 0.0  # negative clamps

    def test_truncate(self):
        assert _truncate("hello", 100) == "hello"
        assert _truncate(None) == ""
        assert _truncate("a\nb\nc") == "a b c"
        out = _truncate("x" * 500, 80)
        assert len(out) == 80
        assert out.endswith("…")

    def test_clamp_since_days(self):
        assert _clamp_since_days(None) == 30
        assert _clamp_since_days(7) == 7
        assert _clamp_since_days(0) == 1
        assert _clamp_since_days(99999) == 365
        assert _clamp_since_days("garbage") == 30  # type: ignore[arg-type]

    def test_clamp_limit(self):
        assert _clamp_limit(None) == 20
        assert _clamp_limit(0) == 1
        assert _clamp_limit(9999) == 200


# =====================================================================
# build_timeline against real DB
# =====================================================================


class TestBuildTimeline:
    def test_empty_db_returns_empty_list(self, isolated_project: Path):
        g = _open_graph(isolated_project)
        try:
            out = build_timeline()
            assert out == []
        finally:
            g.close()

    def test_single_decision_with_outcomes(self, isolated_project: Path):
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt over argon2",
                kept=3,
                reverted=1,
            )
            out = build_timeline()
            assert len(out) == 1
            d = out[0]
            assert d["decision"] == "use bcrypt over argon2"
            assert d["file_path"] == "auth.py"
            assert d["kept"] == 3
            assert d["reverted"] == 1
            assert d["total"] == 4
            # 3 kept + 0 modified + 1 reverted → 3/4 = 0.75
            assert abs(d["score"] - 0.75) < 1e-9
            assert d["session_id"] == "s-test"
            assert d["locked"] is False
        finally:
            g.close()

    def test_query_filter_substring(self, isolated_project: Path):
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt",
                kept=1,
            )
            _plant_decision_with_outcomes(
                g,
                file_path="db.py",
                decision="use postgres",
                kept=1,
            )
            out = build_timeline(query="bcrypt")
            assert len(out) == 1
            assert out[0]["decision"] == "use bcrypt"
        finally:
            g.close()

    def test_query_matches_file_path(self, isolated_project: Path):
        """Query also searches file_path and context — verify."""
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="x",
                kept=1,
            )
            _plant_decision_with_outcomes(
                g,
                file_path="db.py",
                decision="y",
                kept=1,
            )
            out = build_timeline(query="auth")
            assert len(out) == 1
            assert out[0]["file_path"] == "auth.py"
        finally:
            g.close()

    def test_locked_flag_propagates(self, isolated_project: Path):
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="x",
                kept=2,
                locked=True,
            )
            out = build_timeline()
            assert len(out) == 1
            assert out[0]["locked"] is True
        finally:
            g.close()

    def test_decision_with_no_outcomes_still_listed(
        self,
        isolated_project: Path,
    ):
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="a.py",
                decision="ancient decision",
            )
            out = build_timeline()
            assert len(out) == 1
            assert out[0]["total"] == 0
            assert out[0]["score"] == 0.0
        finally:
            g.close()

    def test_limit_clamping_in_build(self, isolated_project: Path):
        g = _open_graph(isolated_project)
        try:
            for i in range(5):
                _plant_decision_with_outcomes(
                    g,
                    file_path=f"f{i}.py",
                    decision=f"d{i}",
                    kept=1,
                )
            # limit=2 → 2 rows
            out = build_timeline(limit=2)
            assert len(out) == 2
        finally:
            g.close()

    def test_uninitialized_project_returns_empty_not_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Bug-X defense: build_timeline never raises on missing store.

        v2.2.0+: replaces the legacy "bad SQL DB" test. Behaviour is the
        same — point at a project with no .codevira/ and verify
        build_timeline returns [] instead of crashing.
        """
        import mcp_server.paths as paths_mod

        bare_project = tmp_path / "bare"
        bare_project.mkdir()
        (bare_project / "pyproject.toml").write_text("")
        monkeypatch.setattr(
            "mcp_server.paths.get_global_home", lambda: tmp_path / "fake-home"
        )
        paths_mod.set_project_dir(bare_project)
        paths_mod.invalidate_data_dir_cache()
        out = build_timeline()
        assert out == []


# =====================================================================
# Renderers — empty case (Lesson #19)
# =====================================================================


class TestRenderersEmptyCase:
    def test_terminal_empty_shows_placeholder(self):
        out = render_terminal([])
        joined = "\n".join(out)
        # Lesson #19 lock-in: NO empty section header. Always a body.
        assert (
            _EMPTY_PLACEHOLDER in joined
        ), f"Empty terminal must show placeholder, got: {joined!r}"

    def test_markdown_empty_shows_placeholder(self):
        out = render_markdown([])
        assert _EMPTY_PLACEHOLDER in out

    def test_html_empty_shows_placeholder(self):
        out = render_html([])
        assert _EMPTY_PLACEHOLDER in out
        # HTML structure intact
        assert "<h1>" in out
        assert "</html>" in out


# =====================================================================
# Renderers — populated case (content-verifying)
# =====================================================================


class TestRenderersPopulated:
    @pytest.fixture
    def sample_timeline(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "decision": "use bcrypt over argon2",
                "file_path": "auth.py",
                "context": None,
                "created_at": "2026-04-13 12:00:00",
                "session_id": "s_4f2a",
                "session_summary": "Fix login flow for special-char emails",
                "locked": True,
                "kept": 6,
                "modified": 0,
                "reverted": 0,
                "total": 6,
                "score": 1.0,
            },
            {
                "id": 2,
                "decision": "Bootstrap not Tailwind",
                "file_path": "style.css",
                "context": None,
                "created_at": "2026-02-08 09:30:00",
                "session_id": "s_1e7b",
                "session_summary": "",
                "locked": False,
                "kept": 0,
                "modified": 0,
                "reverted": 4,
                "total": 4,
                "score": 0.0,
            },
        ]

    def test_terminal_includes_content_not_just_header(
        self,
        sample_timeline,
    ):
        """Lesson #19: don't just check the title appears — check the
        decision text + file + score appear too."""
        out = render_terminal(sample_timeline, ascii_mode=True)
        joined = "\n".join(out)
        assert "use bcrypt over argon2" in joined
        assert "auth.py" in joined
        assert "1.00" in joined  # score
        assert "6 outcome" in joined or "6 kept" in joined
        assert "Bootstrap not Tailwind" in joined
        assert "style.css" in joined
        # The reverted decision shows score 0
        assert "0.00" in joined
        assert "4 reverted" in joined or "reverted" in joined.lower()
        # Locked marker for the bcrypt decision
        assert "[locked]" in joined

    def test_markdown_includes_decision_and_session(
        self,
        sample_timeline,
    ):
        out = render_markdown(sample_timeline)
        assert "use bcrypt over argon2" in out
        assert "auth.py" in out
        assert "Fix login flow for special-char emails" in out
        assert "s_4f2a" in out
        # Headings are present
        assert "## " in out
        # Score in markdown
        assert "1.00" in out

    def test_html_includes_decision_and_session(
        self,
        sample_timeline,
    ):
        out = render_html(sample_timeline)
        assert "use bcrypt over argon2" in out
        assert "auth.py" in out
        assert "Fix login flow for special-char emails" in out
        assert "s_4f2a" in out
        # CSS classes for locked / reverted
        assert "locked" in out
        assert "reverted" in out

    def test_html_xss_escaping(self):
        """Bug-X-shape audit: declared "renders decision text" must
        trace through html.escape. Adversarial decision text must NOT
        produce an executable <script> tag."""
        adversarial = [
            {
                "id": 1,
                "decision": "<script>alert(1)</script>",
                "file_path": "<img src=x onerror=alert(1)>",
                "context": None,
                "created_at": "2026-01-01",
                "session_id": "<b>session</b>",
                "session_summary": "</article><script>x</script>",
                "locked": False,
                "kept": 0,
                "modified": 0,
                "reverted": 0,
                "total": 0,
                "score": 0.0,
            }
        ]
        out = render_html(adversarial)
        # Raw <script> tag must NOT appear as an executable tag.
        # The escaped form ("&lt;script&gt;") IS in the output (as text).
        # Critical assertion: there must be no UNESCAPED `<script>`
        # inside the body section.
        # We do this by counting raw <script> openings — only the ones
        # in the <head><style> are legit.
        body_start = out.find("<body>")
        assert body_start > 0, "HTML must have <body>"
        body = out[body_start:]
        assert "<script>" not in body, (
            f"XSS bug: unescaped <script> in body. Body excerpt:\n" f"{body[:500]}"
        )
        assert (
            "&lt;script&gt;" in body
        ), "Adversarial input should appear as ESCAPED text"
        # Adversarial img tag should also be escaped
        assert "<img src=x" not in body
        assert "&lt;img" in body
        # Session escaping
        assert "<b>session</b>" not in body
        assert "&lt;b&gt;session&lt;/b&gt;" in body
        # </article> in summary mustn't break the article boundary
        assert "&lt;/article&gt;" in body


# =====================================================================
# MCP read_resource handler end-to-end
# =====================================================================


class TestMCPResourceHandler:
    def test_list_resources_exposes_decisions_uri(self):
        from mcp_server.server import handle_list_resources
        import asyncio

        resources = asyncio.run(handle_list_resources())
        uris = [str(r.uri) for r in resources]
        assert any(
            "codevira://decisions" in u for u in uris
        ), f"list_resources must expose decisions URI; got {uris}"

    def test_read_resource_unknown_uri_raises(self):
        from mcp_server.server import handle_read_resource
        import asyncio

        with pytest.raises(ValueError):
            asyncio.run(handle_read_resource("codevira://nonsense/xxx"))

    def test_read_resource_decisions_returns_html(
        self,
        isolated_project: Path,
    ):
        """End-to-end: real DB, real handler, real HTML."""
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt over argon2",
                kept=3,
            )
        finally:
            g.close()

        from mcp_server.server import handle_read_resource
        import asyncio

        out = asyncio.run(handle_read_resource("codevira://decisions"))
        assert "use bcrypt over argon2" in out
        assert "auth.py" in out
        assert "<html" in out

    def test_read_resource_with_query_filters(
        self,
        isolated_project: Path,
    ):
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt",
                kept=1,
            )
            _plant_decision_with_outcomes(
                g,
                file_path="db.py",
                decision="use postgres",
                kept=1,
            )
        finally:
            g.close()

        from mcp_server.server import handle_read_resource
        import asyncio

        out = asyncio.run(handle_read_resource("codevira://decisions/bcrypt"))
        assert "use bcrypt" in out
        assert "use postgres" not in out

    def test_read_resource_with_url_encoded_query(
        self,
        isolated_project: Path,
    ):
        """Spaces / special chars in query should be URL-decoded."""
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt over argon2",
                kept=1,
            )
        finally:
            g.close()

        from mcp_server.server import handle_read_resource
        import asyncio

        out = asyncio.run(
            handle_read_resource("codevira://decisions/bcrypt%20over%20argon2")
        )
        assert "use bcrypt over argon2" in out

    def test_read_resource_no_db_returns_empty_html(
        self,
        isolated_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If graph.db doesn't exist (cold project), return empty
        timeline HTML — NOT crash the MCP client."""
        import mcp_server.paths as paths_mod

        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()
        # No graph.db created

        from mcp_server.server import handle_read_resource
        import asyncio

        out = asyncio.run(handle_read_resource("codevira://decisions"))
        assert _EMPTY_PLACEHOLDER in out


# =====================================================================
# Deep-audit probes (Week-13 user challenge: "have you done QA?")
# =====================================================================


class TestDeepAuditProbes:
    """Probes added in response to user's "have you done QA?" challenge.
    Three areas I wasn't 100% sure about; all probed clean but locked
    in here so future regressions get caught."""

    def test_sql_injection_via_query_param_is_safe(
        self,
        isolated_project: Path,
    ):
        """Parameterized queries must defeat SQL injection through the
        --query / URI argument. The query goes into a LIKE clause via
        a `?` placeholder; concatenation would be a vuln, but we don't.
        """
        g = _open_graph(isolated_project)
        try:
            _plant_decision_with_outcomes(
                g,
                file_path="auth.py",
                decision="use bcrypt",
                kept=1,
            )
            # Assorted malicious queries — ALL must return safe results
            # AND leave the underlying JSONL + FTS5 stores intact.
            # v2.2.0+: substring filter happens in Python (over JSONL),
            # so SQL injection isn't structurally possible here. FTS5
            # ingestion of the query is also parameterized.
            for malicious in [
                "'; DROP TABLE decisions; --",
                "' OR 1=1 --",
                "%' OR '1'='1",
                "'; DELETE FROM outcomes WHERE 1=1; --",
            ]:
                out = build_timeline(query=malicious)
                assert isinstance(out, list)
                # The decision is still there — count via the canonical
                # JSONL read path (decisions_store._read_merged).
                from mcp_server.storage import decisions_store

                merged = decisions_store._read_merged()
                row_count = sum(
                    1
                    for d in merged
                    if not d.get("is_superseded") and not d.get("superseded_by")
                )
                assert row_count == 1, (
                    f"Injection attempt got through with query={malicious!r}: "
                    f"decisions store now has {row_count} active rows (expected 1)"
                )
        finally:
            g.close()

    def test_truncate_boundary_at_exactly_n(self):
        """Exactly N chars: NO ellipsis (text fits). N+1 chars: ellipsis."""
        # exactly 80 → no truncation
        assert _truncate("x" * 80, 80) == "x" * 80
        # 81 → truncated with ellipsis (80 chars total)
        out = _truncate("x" * 81, 80)
        assert len(out) == 80
        assert out.endswith("…")
        # Empty → empty
        assert _truncate("", 80) == ""
        # Single char → unchanged
        assert _truncate("a", 80) == "a"

    def test_html_locked_and_reverted_both_classes_render(self):
        """A decision can be BOTH locked (do_not_revert=1) AND reverted
        (every outcome is type=reverted). The HTML article must carry
        BOTH CSS classes so users see both visual signals."""
        weird = [
            {
                "id": 1,
                "decision": "locked decision that ALSO got reverted",
                "file_path": "weird.py",
                "context": None,
                "created_at": "2026-01-01",
                "session_id": "s1",
                "session_summary": "test",
                "locked": True,
                "kept": 0,
                "modified": 0,
                "reverted": 3,
                "total": 3,
                "score": 0.0,
            }
        ]
        out = render_html(weird)
        import re

        m = re.search(r'<article class="([^"]+)"', out)
        assert m, "expected article element"
        cls = m.group(1)
        assert "locked" in cls
        assert "reverted" in cls
        assert "🔒" in out


# =====================================================================
# Performance
# =====================================================================


class TestPerformance:
    def test_build_timeline_100_decisions_under_50ms(
        self,
        isolated_project: Path,
    ):
        g = _open_graph(isolated_project)
        try:
            for i in range(100):
                _plant_decision_with_outcomes(
                    g,
                    file_path=f"f{i}.py",
                    decision=f"d{i}",
                    kept=2,
                    modified=1,
                )
            durations = []
            for _ in range(20):
                t = time.perf_counter()
                build_timeline(limit=20)
                durations.append((time.perf_counter() - t) * 1000)
            durations.sort()
            p50 = durations[10]
            assert p50 < 50.0, f"p50={p50:.2f}ms exceeds 50ms"
        finally:
            g.close()
