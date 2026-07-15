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

    def test_cache_refreshes_after_invalidate(self, tmp_path, clean_cache):
        root = _make_project(tmp_path, opted_in=False)
        assert opt_in.is_project_opted_in(root) is False  # caches False
        cv = root / ".codevira"
        cv.mkdir(parents=True)
        (cv / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        # Still cached until invalidated (proves the cache is real).
        assert opt_in.is_project_opted_in(root) is False
        opt_in.invalidate_opt_in_cache(root)
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
