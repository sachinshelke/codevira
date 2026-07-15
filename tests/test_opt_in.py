"""Tests for the opt-in activation predicate (opt-in plan, Phase 1).

These assert the FOUNDATION only — the predicate + mode resolution + cache.
No creation vector is gated yet (that's Phases 2-6). A ghost project (one
codevira merely touched, no in-repo ``.codevira/config.yaml``) must classify
as NOT opted-in; an explicitly ``codevira init``-ed project must classify as
opted-in.
"""

from __future__ import annotations

import pytest

from mcp_server import opt_in


@pytest.fixture
def clean_cache():
    """Clear the opt-in cache before and after each test."""
    opt_in.invalidate_opt_in_cache()
    yield
    opt_in.invalidate_opt_in_cache()


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)
    return monkeypatch


@pytest.fixture
def empty_global_home(tmp_path, monkeypatch):
    """Point tracking_mode()'s global-config lookup at an empty dir.

    Makes mode-default assertions deterministic regardless of whether the
    developer's real ~/.codevira/config.yaml sets a tracking mode.
    """
    home = tmp_path / "globalhome"
    home.mkdir()
    monkeypatch.setattr(opt_in, "get_global_home", lambda: home)
    return home


def _make_project(tmp_path, *, opted_in: bool):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)  # a real project marker
    if opted_in:
        cv = root / ".codevira"
        cv.mkdir(parents=True)
        (cv / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return root


class TestIsProjectOptedIn:
    def test_opted_in_project_is_true(self, tmp_path, clean_cache):
        root = _make_project(tmp_path, opted_in=True)
        assert opt_in.is_project_opted_in(root) is True

    def test_ghost_project_is_false(self, tmp_path, clean_cache):
        root = _make_project(tmp_path, opted_in=False)
        assert opt_in.is_project_opted_in(root) is False

    def test_empty_codevira_dir_without_config_is_false(self, tmp_path, clean_cache):
        # A stray empty .codevira/ (dir exists, no config.yaml) must NOT count —
        # this is why we can't reuse storage.paths.is_initialized (dir-only check).
        root = _make_project(tmp_path, opted_in=False)
        (root / ".codevira").mkdir(parents=True)
        assert opt_in.is_project_opted_in(root) is False

    def test_fresh_init_is_seen_without_invalidate(self, tmp_path, clean_cache):
        # A negative result is NEVER cached, so a fresh `codevira init` (even
        # from another process — CLI init vs a long-lived MCP server) is seen on
        # the very next call, no invalidate needed.
        root = _make_project(tmp_path, opted_in=False)
        assert opt_in.is_project_opted_in(root) is False
        cv = root / ".codevira"
        cv.mkdir(parents=True)
        (cv / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        assert opt_in.is_project_opted_in(root) is True  # seen immediately

    def test_positive_is_cached_until_invalidate(self, tmp_path, clean_cache):
        # True IS cached (an opted-in project stays opted in). Un-tracking a
        # live project needs an explicit invalidate.
        root = _make_project(tmp_path, opted_in=True)
        assert opt_in.is_project_opted_in(root) is True  # caches True
        (root / ".codevira" / "config.yaml").unlink()
        assert opt_in.is_project_opted_in(root) is True  # cached, marker gone
        opt_in.invalidate_opt_in_cache(root)
        assert opt_in.is_project_opted_in(root) is False

    def test_opted_in_via_centralized_marker(self, tmp_path, monkeypatch, clean_cache):
        # After the v1.6 in-repo -> centralized migration, the marker lives in
        # the centralized store; the project is still opted in.
        from mcp_server import paths as paths_mod

        root = _make_project(tmp_path, opted_in=False)  # no in-repo marker
        fake_home = tmp_path / "gh"
        key = paths_mod._sanitize_path_key(root.resolve())
        central = fake_home / "projects" / key
        central.mkdir(parents=True)
        (central / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home)
        assert opt_in.is_project_opted_in(root) is True


class TestTrackingMode:
    def test_default_mode_is_hint(self, no_env, empty_global_home, clean_cache):
        assert opt_in.tracking_mode() == "hint"

    def test_env_1_forces_auto_adopt(self, no_env, empty_global_home, clean_cache):
        no_env.setenv("CODEVIRA_AUTO_ADOPT", "1")
        assert opt_in.tracking_mode() == "auto_adopt"

    def test_env_true_forces_auto_adopt(self, no_env, empty_global_home, clean_cache):
        no_env.setenv("CODEVIRA_AUTO_ADOPT", "true")
        assert opt_in.tracking_mode() == "auto_adopt"

    def test_env_0_forces_strict(self, no_env, empty_global_home, clean_cache):
        no_env.setenv("CODEVIRA_AUTO_ADOPT", "0")
        assert opt_in.tracking_mode() == "strict"

    def test_global_config_mode_used_when_no_env(
        self, tmp_path, no_env, monkeypatch, clean_cache
    ):
        home = tmp_path / "globalhome"
        home.mkdir()
        (home / "config.yaml").write_text(
            "tracking:\n  mode: strict\n", encoding="utf-8"
        )
        monkeypatch.setattr(opt_in, "get_global_home", lambda: home)
        assert opt_in.tracking_mode() == "strict"

    def test_env_overrides_global_config(
        self, tmp_path, no_env, monkeypatch, clean_cache
    ):
        home = tmp_path / "globalhome"
        home.mkdir()
        (home / "config.yaml").write_text(
            "tracking:\n  mode: strict\n", encoding="utf-8"
        )
        monkeypatch.setattr(opt_in, "get_global_home", lambda: home)
        no_env.setenv("CODEVIRA_AUTO_ADOPT", "1")
        assert opt_in.tracking_mode() == "auto_adopt"

    def test_malformed_global_config_falls_back_to_default(
        self, tmp_path, no_env, monkeypatch, clean_cache
    ):
        home = tmp_path / "globalhome"
        home.mkdir()
        (home / "config.yaml").write_text(
            "this: [is not: valid: yaml", encoding="utf-8"
        )
        monkeypatch.setattr(opt_in, "get_global_home", lambda: home)
        assert opt_in.tracking_mode() == "hint"


class TestActivationAllowed:
    def test_ghost_not_allowed_in_hint_mode(
        self, tmp_path, no_env, empty_global_home, clean_cache
    ):
        root = _make_project(tmp_path, opted_in=False)
        assert opt_in.activation_allowed(root) is False

    def test_opted_in_allowed(self, tmp_path, no_env, empty_global_home, clean_cache):
        root = _make_project(tmp_path, opted_in=True)
        assert opt_in.activation_allowed(root) is True

    def test_auto_adopt_allows_ghost(
        self, tmp_path, no_env, empty_global_home, clean_cache
    ):
        no_env.setenv("CODEVIRA_AUTO_ADOPT", "1")
        root = _make_project(tmp_path, opted_in=False)
        assert opt_in.activation_allowed(root) is True


# ---------------------------------------------------------------------------
# Phase 5 — dispatch gate: READ/WRITE classification + inert responses
# ---------------------------------------------------------------------------


def _dispatched_tool_names() -> set[str]:
    """Tool names the server's call_tool if/elif chain dispatches on.

    Parsed from the call_tool body only (prompt/resource handlers earlier in
    the module also use ``name == "..."`` and must be excluded).
    """
    import re
    from pathlib import Path

    import mcp_server.server as server_mod

    src = Path(server_mod.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def call_tool(") :]
    return set(re.findall(r'name == "([^"]+)"', body))


class TestOptInDispatchClassification:
    """Every dispatched tool must be classified — a new tool can't silently
    default to the wrong side of the opt-in gate."""

    def test_read_and_write_sets_are_disjoint(self):
        assert opt_in.READ_TOOLS.isdisjoint(opt_in.WRITE_TOOLS)

    def test_every_dispatched_tool_is_classified(self):
        dispatched = _dispatched_tool_names()
        assert dispatched, "parser found no dispatched tools — regex broke"
        classified = opt_in.READ_TOOLS | opt_in.WRITE_TOOLS
        missing = dispatched - classified
        assert not missing, f"Unclassified dispatched tools: {sorted(missing)}"

    def test_no_stale_classifications(self):
        # Every classified name should still be dispatched (catch typos/removals).
        dispatched = _dispatched_tool_names()
        classified = opt_in.READ_TOOLS | opt_in.WRITE_TOOLS
        stale = classified - dispatched
        assert not stale, f"Classified but not dispatched: {sorted(stale)}"


class TestOptInHintPayload:
    def test_read_tool_returns_inert_hint_not_error(self):
        p = opt_in.opt_in_hint_payload("get_session_context")
        assert p["not_opted_in"] is True
        assert "codevira init" in p["fix_command"]
        assert "error" not in p

    def test_write_tool_refuses(self):
        p = opt_in.opt_in_hint_payload("record_decision")
        assert p["not_opted_in"] is True
        assert p["error"] == "refused"
        assert "codevira init" in p["fix_command"]


class TestDispatchGateEndToEnd:
    """call_tool itself must return the inert/refuse payload for a non-opted
    project (the primary chokepoint), per D1/D2."""

    def _ghost_cwd(self, tmp_path, monkeypatch):
        from mcp_server import paths

        monkeypatch.delenv("CODEVIRA_AUTO_ADOPT", raising=False)
        project = tmp_path / "ghost"
        project.mkdir()  # no in-repo .codevira/config.yaml -> not opted in
        monkeypatch.setattr(paths, "_project_dir_override", None)
        paths.reset_pinned_root()
        monkeypatch.chdir(project.resolve())
        opt_in.invalidate_opt_in_cache()
        return project

    def test_read_tool_returns_hint(self, tmp_path, monkeypatch):
        import asyncio
        import json as _json

        from mcp_server.server import call_tool

        self._ghost_cwd(tmp_path, monkeypatch)
        result = asyncio.run(call_tool("get_session_context", {}))
        payload = _json.loads(result[0].text)
        assert payload["not_opted_in"] is True
        assert "error" not in payload  # read -> inert, not a refusal

    def test_write_tool_refuses(self, tmp_path, monkeypatch):
        import asyncio
        import json as _json

        from mcp_server.server import call_tool

        project = self._ghost_cwd(tmp_path, monkeypatch)
        result = asyncio.run(
            call_tool("record_decision", {"decision": "should not persist"})
        )
        payload = _json.loads(result[0].text)
        assert payload["error"] == "refused"
        assert payload["not_opted_in"] is True
        # And nothing was written to an in-repo store.
        assert not (project / ".codevira").exists()
