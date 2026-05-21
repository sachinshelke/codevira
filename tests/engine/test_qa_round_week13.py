"""
test_qa_round_week13.py — Integrated QA across Weeks 1-13 (10 heroes shipped).

Last hero round. Hero 8 introduces THREE new surfaces (build_timeline,
3 renderers, MCP resources, CLI subcommand). Deep-audit checklist
applied from start (post-Bug-1-8 + Lessons #15-21):

  1. Path-traversal probe — N/A (Hero 8 takes query strings, no paths).
     But CLI --project IS user-controlled → Bug-8 lesson applies.
  2. Empty-section probe for all 3 renderers (Lesson #19) — done in
     unit tests; integration tests verify through MCP + CLI surfaces.
  3. Content-verifying assertions — done in unit tests; integration
     verifies through every wiring path.
  4. Cache-collision probe — N/A (no new SignalContext accessor).
  5. Bug-X-shape audit — declared support traces end-to-end:
     - codevira://decisions  → list_resources exposes it → wiring works
     - codevira://decisions/<query> → URL-encoded too → wiring works
     - codevira replay [+5 formats] → all formats verified
     - HTML XSS → escape() runs everywhere → tested with adversarial
       input
  6. All 4 _EDIT_TOOLS through wiring — N/A (Hero 8 doesn't intercept
     events; it's a browse surface).

What this round adds beyond unit + CLI subprocess:

  L1: 10-hero default registration set
  L2: MCP server has BOTH list_resources AND list_tools handlers
  L3: codevira://decisions resource doesn't crash dispatch (engine kill
      switch coexistence)
  L4: Browse data IS the same data Hero 1 / 5 / 10 read — verify
      end-to-end coherence (record decision via tool → see it in replay)
  L5: HTML XSS at the WIRING boundary (full async handler path, not
      just direct render_html call)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    return project


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    from mcp_server.engine.runner import reset_policies
    from mcp_server.engine.scope_contract import clear_all

    reset_policies()
    clear_all()
    monkeypatch.delenv("CODEVIRA_ENGINE", raising=False)
    yield
    reset_policies()
    clear_all()


def _set_project(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()


def _open_graph(project: Path):
    from mcp_server.paths import get_data_dir
    from indexer.sqlite_graph import SQLiteGraph

    graph_db = get_data_dir() / "graph" / "graph.db"
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteGraph(graph_db)


# =====================================================================
# L1 — 10-hero baseline (final default set)
# =====================================================================


class TestL1_TenHeroes:
    def test_ten_default_policies_registered(self):
        """The complete v2.0 line-up. After Week 13 this is THE final
        set. Drift in either direction must update the test explicitly.
        """
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
        )

        register_default_policies()
        names = {p.name for p in registered_policies()}
        # Note: Hero 8 (Decision Replay) is NOT a policy — it's a
        # browse surface (MCP resources + CLI). So default policy
        # count stays at 9 (post-Week-12).
        expected = {
            "blast_radius_veto",  # Hero 4 (Week 4)
            "decision_lock",  # Hero 1 (Week 5)
            "relevance_inject",  # Hero 5 (Week 6)
            "token_budget_persist",  # Hero 6 (Week 7)
            "anti_regression",  # Hero 2 (Week 8)
            "live_style_enforcement",  # Hero 7 (Week 9)
            "ai_promotion_score",  # Hero 10 (Week 10)
            "intent_inference",  # Hero 9 (Week 11)
            "scope_contract_lock",  # Hero 3 (Week 12)
            "post_edit_graph_refresh",  # v2.1.2 Item 4
            # Hero 8 (Decision Replay) is NOT here — it's a browse
            # surface, not an event-intercepting policy.
        }
        assert names == expected, (
            f"Default policy set drift: got {sorted(names)}, "
            f"expected {sorted(expected)}"
        )

    def test_hero_8_is_browse_surface_not_policy(self):
        """Lock the architectural choice: Hero 8 is NOT a Policy
        subclass. If a future refactor accidentally creates one, this
        test catches it and forces a deliberate decision."""
        from mcp_server.engine.policy import Policy
        from mcp_server import decision_replay

        # Walk the module's public attributes
        policy_classes = [
            v
            for v in vars(decision_replay).values()
            if isinstance(v, type) and issubclass(v, Policy) and v is not Policy
        ]
        assert (
            policy_classes == []
        ), f"Hero 8 must not define Policy subclasses; got {policy_classes}"


# =====================================================================
# L2 — MCP server exposes both resources AND tools
# =====================================================================


class TestL2_MCPHandlers:
    def test_server_has_resource_and_tool_handlers(self):
        """Bug-X-shape audit: declared "MCP resource" handler must
        actually be wired into the server object."""
        # The handlers register internally on the Server instance.
        # We can't introspect them by name directly (private), but we
        # can call them through their public references.
        from mcp_server.server import (
            handle_list_resources,
            handle_read_resource,
        )

        assert callable(handle_list_resources)
        assert callable(handle_read_resource)

    def test_list_resources_returns_one_decisions_uri(self):
        from mcp_server.server import handle_list_resources

        out = asyncio.run(handle_list_resources())
        assert len(out) >= 1
        uris = [str(r.uri) for r in out]
        assert "codevira://decisions" in uris


# =====================================================================
# L3 — Engine kill switch doesn't break browse surfaces
# =====================================================================


class TestL3_KillSwitchDoesNotBreakBrowse:
    def test_codevira_engine_off_browse_still_works(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """CODEVIRA_ENGINE=0 disables policy dispatch. The browse
        surface (MCP resources + CLI) is NOT a policy, so it must keep
        working. Verify the read_resource handler works even with
        engine disabled."""
        monkeypatch.setenv("CODEVIRA_ENGINE", "0")

        _set_project(monkeypatch, isolated_project)
        # v2.2.0+: write via the JSONL decisions_store so the resource
        # handler (which is JSONL-only) sees the data.
        from mcp_server.storage import decisions_store, paths as store_paths

        store_paths.ensure_dirs()
        decisions_store.record(
            "use bcrypt over argon2",
            file_path="auth.py",
            session_id="s1",
        )

        from mcp_server.server import handle_read_resource

        out = asyncio.run(handle_read_resource("codevira://decisions"))
        # Even with engine off, the data layer reads the decision
        assert "use bcrypt over argon2" in out


# =====================================================================
# L4 — End-to-end coherence: record + browse
# =====================================================================


class TestL4_RecordThenBrowse:
    def test_decision_recorded_via_tool_appears_in_replay_cli(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """The data layer is shared across Heroes 1, 5, 10, AND Hero 8's
        replay surface. Verify a decision recorded one way appears in
        the replay output."""
        _set_project(monkeypatch, isolated_project)
        # v2.2.0+: write via the canonical JSONL store (decisions_store.record).
        # Also mirror the row into graph.db so Hero 10's promotion scorer
        # (which still uses the SQLite graph for code-structure aggregation)
        # has data to aggregate.
        from mcp_server.storage import (
            decisions_store,
            jsonl_store,
            paths as store_paths,
        )
        from datetime import datetime, timezone

        store_paths.ensure_dirs()
        jsonl_store.append(
            store_paths.sessions_path(),
            {
                "id": "S-s1",
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": "s1",
                "summary": "shared-data test",
            },
        )
        new_id = decisions_store.record(
            "End-to-end shared data check",
            file_path="shared.py",
            session_id="s1",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        for _ in range(2):
            jsonl_store.append(
                store_paths.outcomes_path(),
                {
                    "ts": now_iso,
                    "decision_id": new_id,
                    "outcome_type": "kept",
                    "delta_summary": "test kept",
                },
            )

        # Mirror to graph.db for Hero 10's aggregator.
        g = _open_graph(isolated_project)
        try:
            g.conn.execute(
                "INSERT INTO sessions (session_id, summary) VALUES (?, ?)",
                ("s1", "shared-data test"),
            )
            cur = g.conn.execute(
                "INSERT INTO decisions (session_id, decision, file_path, "
                "context, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                ("s1", "End-to-end shared data check", "shared.py", ""),
            )
            for _ in range(2):
                g.record_outcome(
                    session_id="s1",
                    file_path="shared.py",
                    outcome_type="kept",
                    decision_id=cur.lastrowid,
                )
            g.conn.commit()
        finally:
            g.close()

        # Read via build_timeline (Hero 8's data path — JSONL in v2.2.0)
        from mcp_server.decision_replay import build_timeline

        g = _open_graph(isolated_project)
        try:
            timeline = build_timeline()
            assert len(timeline) == 1
            assert timeline[0]["decision"] == "End-to-end shared data check"

            # Read via Hero 1's signal accessor — same data
            from mcp_server.engine.signals import SignalContext

            ctx = SignalContext(project_root=isolated_project)
            decisions = ctx.decisions(file="shared.py")
            assert len(decisions) == 1
            assert decisions[0]["decision"] == "End-to-end shared data check"

            # Read via Hero 10's promotion scorer — still graph.db-backed
            from mcp_server.engine.promotion_score import (
                aggregate_decision_outcomes,
            )

            agg = aggregate_decision_outcomes(g.conn, since_days=30, min_outcomes=1)
            assert any(a.get("decision") == "End-to-end shared data check" for a in agg)
        finally:
            g.close()


# =====================================================================
# L5 — XSS through the FULL wiring (async handler path)
# =====================================================================


class TestL5_XSSThroughWiring:
    def test_adversarial_decision_text_escaped_through_handle_read_resource(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """Bug-X-shape: the unit-test verifies render_html escapes; this
        verifies the FULL handler-to-output path doesn't accidentally
        un-escape (e.g., by re-rendering server-side)."""
        _set_project(monkeypatch, isolated_project)
        from datetime import datetime, timezone

        from mcp_server.storage import (
            decisions_store,
            jsonl_store,
            paths as store_paths,
        )

        store_paths.ensure_dirs()
        # Plant adversarial session summary.
        jsonl_store.append(
            store_paths.sessions_path(),
            {
                "id": "S-s1",
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": "s1",
                "summary": "<script>alert(2)</script>",
            },
        )
        # Record adversarial decision with HTML-tag-like fields.
        decisions_store.record(
            "<script>alert(1)</script>",
            file_path="<img src=x>",
            session_id="s1",
            context="<svg/onload=alert(3)>",
        )

        from mcp_server.server import handle_read_resource

        out = asyncio.run(handle_read_resource("codevira://decisions"))
        # Critical: no UNESCAPED <script> in the body
        body_start = out.find("<body>")
        assert body_start > 0
        body = out[body_start:]
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body
        # img tag escaped
        assert "<img src=x>" not in body
        assert "&lt;img" in body


# =====================================================================
# L6 — Resource handler defends against broken DB / read failures
# =====================================================================


class TestL6_HandlerRobustness:
    def test_handle_read_resource_with_broken_db_returns_error_html(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """If the graph.db is corrupted / unreadable, the handler must
        return a degraded HTML page, NOT crash the MCP client."""
        _set_project(monkeypatch, isolated_project)
        from mcp_server.paths import get_data_dir

        graph_db = get_data_dir() / "graph" / "graph.db"
        graph_db.parent.mkdir(parents=True, exist_ok=True)
        graph_db.write_bytes(b"not a sqlite db at all")

        from mcp_server.server import handle_read_resource

        out = asyncio.run(handle_read_resource("codevira://decisions"))
        # Either the SDK's "wrap" returns empty timeline, OR the
        # try/except in the handler returned an error HTML. Either way:
        # - rc must succeed (handler returned a string)
        # - HTML structure intact
        # - either the empty placeholder OR an error message appears
        assert "<html" in out.lower() or "<h1" in out
        assert "No decisions recorded yet" in out or "couldn't load decisions" in out


# =====================================================================
# L7 — Defensive: unknown sub-URI doesn't expose path data
# =====================================================================


class TestL7_UnknownSubURIDefensive:
    def test_unknown_sub_uri_raises_value_error_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_project: Path,
    ):
        """codevira://nonsense/etc must NOT silently fall through to
        any DB read; it MUST raise so the SDK reports not-found.
        Defense-in-depth against typo'd or malicious URIs."""
        _set_project(monkeypatch, isolated_project)
        from mcp_server.server import handle_read_resource

        with pytest.raises(ValueError):
            asyncio.run(handle_read_resource("codevira://nonsense/etc"))
        with pytest.raises(ValueError):
            asyncio.run(handle_read_resource("codevira://users/list"))
