"""
test_global_sync.py — v3.0.0 ``mcp_server.global_sync`` coverage.

v3.0.0 (2026-05-22 surface-cut audit) gutted ``global_sync.py`` from
a 187-line preference / learned-rule bidirectional sync to a ~90-line
project-registry helper. The 320-line v2.x test file was rewritten
in the same audit because the features it tested no longer exist
(``export_project_to_global``, ``get_global_stats``,
``TestImportGlobalToProject`` cross-project preference + rule
copies — all gone with the MCP tools that consumed them).

What's kept in v3.0.0:
  * ``register_current_project()`` — best-effort registration in
    ``~/.codevira/global.db`` so ``codevira projects`` can list
    every project on the machine
  * ``import_global_to_project()`` — backwards-compat alias for the
    above so external callers / mocks keep working

What this file verifies:
  * register_current_project succeeds against an isolated tmp HOME
  * register_current_project never raises when the global DB init
    fails (best-effort contract)
  * The legacy ``import_global_to_project`` alias returns the same
    shape and routes to register_current_project
  * ``_get_project_language`` reads config.yaml + degrades gracefully
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.global_sync import (
    _get_project_language,
    import_global_to_project,
    register_current_project,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ~/.codevira/ under tmp_path so the real one stays clean."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # These tests intentionally register a tmp_path project (which the
    # v3.4.0 ephemeral-path guard would otherwise skip). Opt back in.
    monkeypatch.setenv("CODEVIRA_ALLOW_EPHEMERAL_PROJECT", "1")

    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='roundtrip'\n")

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home / ".codevira")
    monkeypatch.setattr(
        paths_mod,
        "get_global_db_path",
        lambda: fake_home / ".codevira" / "global.db",
    )
    # Make sure the global home exists before the test runs.
    (fake_home / ".codevira").mkdir()
    return fake_home


class TestRegisterCurrentProject:
    def test_returns_registered_true_on_happy_path(self, isolated_home: Path) -> None:
        """A normal project with a resolvable root registers cleanly."""
        result = register_current_project()
        assert result["registered"] is True
        assert "project_root" in result
        assert Path(result["project_root"]).is_dir()

    def test_project_row_appears_in_global_db(self, isolated_home: Path) -> None:
        """After register, the project should be queryable by name."""
        register_current_project()

        from indexer.global_db import GlobalDB
        from mcp_server.paths import get_global_db_path

        gdb = GlobalDB(get_global_db_path())
        try:
            rows = gdb.conn.execute("SELECT path, name FROM projects").fetchall()
        finally:
            gdb.close()
        names = {r["name"] for r in rows}
        assert "myproject" in names

    def test_never_raises_when_global_db_init_fails(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Best-effort contract — any error inside is logged + reported,
        never raised. The server's startup path depends on this."""

        def boom(*args, **kwargs):
            raise RuntimeError("simulated global.db corruption")

        monkeypatch.setattr("indexer.global_db.GlobalDB.__init__", boom)
        result = register_current_project()
        assert result["registered"] is False
        assert "error" in result
        assert "simulated" in result["error"]


class TestBackwardsCompatAlias:
    """The v2.x function name ``import_global_to_project`` is preserved
    so external code (tests mocking it, third-party scripts) keeps
    working through v3.0.0. It must route through to the new
    register_current_project and return the same dict shape."""

    def test_alias_returns_same_shape_as_register(self, isolated_home: Path) -> None:
        legacy = import_global_to_project()
        canonical = register_current_project()
        assert set(legacy.keys()) == set(canonical.keys())
        assert legacy["registered"] is True
        assert canonical["registered"] is True


class TestGetProjectLanguage:
    def test_returns_language_from_config_yaml(
        self,
        isolated_home: Path,
    ) -> None:
        """When config.yaml has a `language` field, return it."""
        import yaml

        from mcp_server.paths import get_data_dir

        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "config.yaml"
        config_path.write_text(yaml.safe_dump({"project": {"language": "python"}}))
        assert _get_project_language() == "python"

    def test_returns_none_when_config_missing(self, isolated_home: Path) -> None:
        """No config → no language → None. Best-effort, no raise."""
        # config.yaml deliberately absent under this isolated_home
        assert _get_project_language() is None

    def test_returns_none_on_malformed_yaml(self, isolated_home: Path) -> None:
        """Malformed config → None, no raise. The startup path can't
        crash because the user's yaml has a typo."""
        from mcp_server.paths import get_data_dir

        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text("this: is: : not valid yaml ]]]]")
        assert _get_project_language() is None
