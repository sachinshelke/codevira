"""
Tests for :mod:`mcp_server.cli_configure` — the v1.8.0 ``codevira configure``
command. Covers scan_project, prompt_multi_select, write_config_patch,
_normalize_extensions, and cmd_configure orchestrator paths.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from mcp_server import cli_configure
from mcp_server.cli_configure import (
    NonInteractiveError,
    _normalize_extensions,
    _split_csv,
    cmd_configure,
    prompt_multi_select,
    scan_project,
    write_config_patch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a tmp project tree with the given {relpath: content} files."""
    project = tmp_path / "proj"
    project.mkdir()
    for rel, content in files.items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return project


def _init_codevira(project: Path, config: dict) -> Path:
    """Write a .codevira/config.yaml under project. Returns data_dir."""
    data_dir = project / ".codevira"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "config.yaml").write_text(yaml.safe_dump({"project": config}, sort_keys=False))
    return data_dir


# ===========================================================================
# _normalize_extensions
# ===========================================================================

class TestNormalizeExtensions:
    def test_accepts_dotless_and_dotted(self):
        assert _normalize_extensions("py,ts") == [".py", ".ts"]
        assert _normalize_extensions(".py,.ts") == [".py", ".ts"]
        assert _normalize_extensions(["py", ".ts"]) == [".py", ".ts"]

    def test_deduplicates_and_sorts(self):
        assert _normalize_extensions(".ts,.py,.ts,py") == [".py", ".ts"]

    def test_lowercases(self):
        assert _normalize_extensions(".PY,.Ts") == [".py", ".ts"]

    def test_empty(self):
        assert _normalize_extensions("") == []
        assert _normalize_extensions([]) == []


class TestSplitCsv:
    def test_split_preserves_order(self):
        assert _split_csv("src, lib, app") == ["src", "lib", "app"]

    def test_split_dedupes(self):
        assert _split_csv("src,lib,src") == ["src", "lib"]

    def test_split_empty(self):
        assert _split_csv("") == []
        assert _split_csv(None) == []


# ===========================================================================
# scan_project
# ===========================================================================

class TestScanProject:
    def test_returns_discovered_dirs_and_counts(self, tmp_path):
        project = _make_project(tmp_path, {
            "src/a.py": "x=1",
            "src/b.py": "y=2",
            "lib/c.py": "z=3",
            "README.md": "# hi",
        })
        result = scan_project(project, {"watched_dirs": ["src"], "file_extensions": [".py"]})
        paths = {d["path"]: d for d in result["dirs_discovered"]}
        assert "src" in paths and paths["src"]["files"] == 2
        assert "lib" in paths and paths["lib"]["files"] == 1
        assert paths["src"]["on_disk"] is True

    def test_respects_gitignore(self, tmp_path):
        project = _make_project(tmp_path, {
            "src/a.py": "x=1",
            "excluded/b.py": "y=2",
            ".gitignore": "excluded/\n",
        })
        result = scan_project(project, {})
        paths = {d["path"]: d for d in result["dirs_discovered"]}
        # src should be discovered; excluded should NOT
        assert "src" in paths
        assert "excluded" not in paths

    def test_flags_missing_on_disk_dirs(self, tmp_path):
        project = _make_project(tmp_path, {"apps/a.ts": "x"})
        # config lists "src" which doesn't exist on disk
        result = scan_project(project, {"watched_dirs": ["src"], "file_extensions": [".ts"]})
        assert "src" in result["dirs_missing"]
        # And it should be injected into dirs_discovered with on_disk=False
        missing_entries = [d for d in result["dirs_discovered"] if not d["on_disk"]]
        assert any(d["path"] == "src" for d in missing_entries)

    def test_empty_project_returns_empty_lists(self, tmp_path):
        project = _make_project(tmp_path, {})
        result = scan_project(project, {})
        assert result["dirs_discovered"] == []
        assert result["exts_discovered"] == []
        assert result["dirs_missing"] == []

    def test_honors_user_skip_dirs_from_config(self, tmp_path):
        """User's skip_dirs config should be respected during scan. Users who
        explicitly set skip_dirs: [vendor] should NOT see vendor/ in the list."""
        project = _make_project(tmp_path, {
            "src/a.py": "x=1",
            "vendor/b.py": "y=2",
            "keep/c.py": "z=3",
        })
        result = scan_project(
            project,
            {"watched_dirs": ["src"], "file_extensions": [".py"],
             "skip_dirs": ["vendor"]},
        )
        paths_seen = {d["path"] for d in result["dirs_discovered"]}
        assert "vendor" not in paths_seen, (
            "scan_project must honor user's skip_dirs config — otherwise "
            "users see dirs they deliberately excluded every time they run configure"
        )
        assert "src" in paths_seen
        assert "keep" in paths_seen

    def test_deterministic_sort_order(self, tmp_path):
        # Two dirs with different file counts → sorted by -files, then path
        project = _make_project(tmp_path, {
            f"big/f{i}.py": "x" for i in range(5)
        } | {
            "small/only.py": "x",
            "aaa/one.py": "x",   # file count 1, path sorts before "small"
        })
        result = scan_project(project, {})
        dirs = [d["path"] for d in result["dirs_discovered"]]
        assert dirs[0] == "big"           # highest file count
        assert dirs.index("aaa") < dirs.index("small")  # tie-break by path asc


# ===========================================================================
# prompt_multi_select
# ===========================================================================

def _items(paths: list[str], on_disk_default: bool = True) -> list[dict]:
    return [{"path": p, "on_disk": on_disk_default} for p in paths]


class TestPromptMultiSelect:
    def test_parses_comma_list(self, monkeypatch, capsys):
        items = _items(["a", "b", "c", "d", "e"])
        monkeypatch.setattr("sys.stdin", io.StringIO("1,3,5\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, set(), "path", lambda x: x["path"])
        assert got == {"a", "c", "e"}

    def test_all_excludes_missing_on_disk(self, monkeypatch):
        items = _items(["a", "b"], on_disk_default=True) + _items(["z"], on_disk_default=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("all\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, set(), "path", lambda x: x["path"])
        assert got == {"a", "b"}  # "z" is missing-on-disk, excluded

    def test_none_returns_empty_set(self, monkeypatch):
        items = _items(["a", "b"])
        monkeypatch.setattr("sys.stdin", io.StringIO("none\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, {"a"}, "path", lambda x: x["path"])
        assert got == set()

    def test_empty_keeps_preselected(self, monkeypatch):
        items = _items(["a", "b"])
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, {"a"}, "path", lambda x: x["path"])
        assert got == {"a"}

    def test_q_aborts_returns_none(self, monkeypatch):
        items = _items(["a"])
        monkeypatch.setattr("sys.stdin", io.StringIO("q\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, set(), "path", lambda x: x["path"])
        assert got is None

    def test_out_of_range_reprompts(self, monkeypatch, capsys):
        items = _items(["a", "b"])
        # First: "99" (invalid). Second: "1" (valid).
        monkeypatch.setattr("sys.stdin", io.StringIO("99\n1\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            got = prompt_multi_select("T", items, set(), "path", lambda x: x["path"])
        assert got == {"a"}
        out = capsys.readouterr().out
        assert "Out of range" in out

    def test_non_tty_raises_noninteractive_error(self):
        items = _items(["a"])
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=False):
            with pytest.raises(NonInteractiveError):
                prompt_multi_select("T", items, set(), "path", lambda x: x["path"])


# ===========================================================================
# write_config_patch
# ===========================================================================

class TestWriteConfigPatch:
    def test_preserves_other_keys(self, tmp_path):
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        cfg = {
            "project": {"name": "proj", "watched_dirs": ["x"]},
            "logs": {"retention_days": 30},
        }
        (data_dir / "config.yaml").write_text(yaml.safe_dump(cfg))
        write_config_patch(data_dir, {"src", "lib"}, {".py"})
        reloaded = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert reloaded["logs"] == {"retention_days": 30}
        assert reloaded["project"]["name"] == "proj"

    def test_overwrites_watched_dirs_not_merged(self, tmp_path):
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({"project": {"watched_dirs": ["old"]}}))
        write_config_patch(data_dir, {"new"}, {".py"})
        reloaded = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert reloaded["project"]["watched_dirs"] == ["new"]
        assert "old" not in reloaded["project"]["watched_dirs"]

    def test_idempotent(self, tmp_path):
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({"project": {}}))
        write_config_patch(data_dir, {"src", "lib"}, {"py", "ts"})
        first = (data_dir / "config.yaml").read_text()
        write_config_patch(data_dir, {"lib", "src"}, {".ts", ".py"})  # same content, different order
        second = (data_dir / "config.yaml").read_text()
        assert first == second, "write_config_patch must be order-independent & idempotent"

    def test_handles_malformed_project_key(self, tmp_path):
        """If config.yaml has project as a list/scalar (malformed/hand-edited),
        write_config_patch must normalize it to a dict, not crash on
        `setdefault` returning the wrong-type value."""
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        # project as a list — would crash naive setdefault
        (data_dir / "config.yaml").write_text("project:\n  - a\n  - b\n")
        write_config_patch(data_dir, {"src"}, {".py"})
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]
        assert cfg["project"]["file_extensions"] == [".py"]

    def test_concurrent_writers_dont_collide_on_tmp(self, tmp_path):
        """Multiple writers using the same tmp name race: one renames its
        tmp onto config.yaml, the next finds tmp missing → FileNotFoundError.
        Fix: tempfile.mkstemp gives each writer a unique tmp name."""
        import threading
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({"project": {}}))

        errors = []
        def writer(wid):
            try:
                for i in range(20):
                    write_config_patch(data_dir, {f"w{wid}_d{i}"}, {f".e{wid}"})
            except Exception as e:
                errors.append((wid, type(e).__name__, str(e)))

        threads = [threading.Thread(target=writer, args=(w,)) for w in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert errors == [], (
            f"concurrent writers collided: {len(errors)} errors. "
            "tempfile.mkstemp must give each writer a unique tmp name."
        )
        # No leftover tmp files
        leftover = list(data_dir.glob("*.tmp"))
        assert leftover == [], f"stale tmp files: {leftover}"

    def test_write_is_atomic_under_concurrent_reads(self, tmp_path):
        """A concurrent reader (e.g. MCP server's periodic _load_config) must
        NEVER see an empty or partial config.yaml during write. Uses
        os.replace (atomic on POSIX) via _atomic_write_text."""
        import threading
        import time
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({
            "project": {"watched_dirs": ["old"], "file_extensions": [".py"]}
        }))

        bad_reads = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    raw = (data_dir / "config.yaml").read_text()
                    parsed = yaml.safe_load(raw)
                    if parsed is None or not isinstance(parsed, dict):
                        bad_reads.append("empty")
                    elif "project" not in parsed:
                        bad_reads.append("missing_project")
                except yaml.YAMLError:
                    bad_reads.append("yaml_error")

        r = threading.Thread(target=reader, daemon=True)
        r.start()
        try:
            for i in range(100):
                write_config_patch(data_dir, {f"dir{i}"}, {f".ext{i}"})
        finally:
            stop.set()
            r.join(timeout=1)

        assert bad_reads == [], (
            f"concurrent readers observed {len(bad_reads)} partial/empty states — "
            "config write is NOT atomic"
        )

    def test_handles_top_level_non_dict(self, tmp_path):
        """If the top-level YAML is a list/scalar, write_config_patch should
        normalize it to {} and proceed."""
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("- a\n- b\n")
        write_config_patch(data_dir, {"src"}, {".py"})
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]

    def test_does_not_touch_metadata_json(self, tmp_path):
        data_dir = tmp_path / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({"project": {}}))
        meta = data_dir / "metadata.json"
        meta.write_text('{"path_key": "before"}')
        mtime_before = meta.stat().st_mtime_ns
        write_config_patch(data_dir, {"src"}, {".py"})
        # metadata.json must be untouched
        assert meta.read_text() == '{"path_key": "before"}'
        assert meta.stat().st_mtime_ns == mtime_before


# ===========================================================================
# cmd_configure orchestrator
# ===========================================================================

@pytest.fixture
def configured_project(tmp_path, monkeypatch):
    """Centralized-mode: data_dir LIVES IN A DIFFERENT TREE from the project.

    This mirrors production (v1.6+) where data_dir = ~/.codevira/projects/<slug>/
    while the source files live at the actual project root. If we co-located
    them, cmd_configure could get away with ``data_dir.parent`` — which fails
    in real use.
    """
    project = _make_project(tmp_path, {
        "src/a.py": "x=1",
        "src/b.py": "y=2",
        "lib/c.py": "z=3",
    })
    # data_dir is at a SEPARATE location (simulates ~/.codevira/projects/<slug>/)
    data_dir = tmp_path / "centralized-home" / "projects" / "proj-key"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text(yaml.safe_dump(
        {"project": {"name": "proj", "watched_dirs": ["src"], "file_extensions": [".py"]}},
        sort_keys=False,
    ))
    monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
    # Crucial: also monkeypatch get_project_root — otherwise cmd_configure
    # would fall back to cwd or --project-dir which we haven't set.
    monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
    from mcp_server import paths
    paths._data_dir_cache.clear()
    return project, data_dir


class TestCmdConfigure:
    def test_bootstrap_creates_data_dir_when_missing(self, tmp_path, monkeypatch, capsys):
        """After `codevira register` (which only writes MCP client configs,
        not data_dir), running `codevira configure` must transparently
        create the data_dir + subdirs + config.yaml. Previously this errored
        out with a misleading 'run register first' message — fixed here."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        # data_dir path is computed but doesn't exist yet
        data_dir = tmp_path / "centralized" / "projects" / "new-slug"
        assert not data_dir.exists()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        # Bootstrap created the full directory structure
        assert data_dir.exists()
        assert (data_dir / "config.yaml").exists()
        assert (data_dir / "graph").exists()
        assert (data_dir / "codeindex").exists()

    def test_dry_run_does_not_create_data_dir(self, tmp_path, monkeypatch, capsys):
        """Dry-run must not create the data_dir skeleton either."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "new-slug"

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        rc = cmd_configure(interactive=False, dirs_arg=None, exts_arg=None,
                           reindex=False, dry_run=True)
        assert rc == 0
        # Dry-run shouldn't create anything on disk
        assert not (data_dir / "config.yaml").exists()

    def test_bootstrap_respects_dry_run(self, tmp_path, monkeypatch, capsys):
        """Dry-run must NOT write config.yaml even when bootstrapping."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)
        assert not (data_dir / "config.yaml").exists()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        rc = cmd_configure(interactive=False, dirs_arg=None, exts_arg=None,
                           reindex=False, dry_run=True)
        assert rc == 0
        assert not (data_dir / "config.yaml").exists(), \
            "dry-run must not touch disk, even during bootstrap"
        out = capsys.readouterr().out
        assert "would bootstrap" in out

    def test_bootstrap_also_writes_metadata_and_registers_globally(
        self, tmp_path, monkeypatch,
    ):
        """Parity with auto_init: bootstrap must also write metadata.json and
        register the project in global.db. Otherwise users who 'register'
        then 'configure' never get rename-resilient lookup and never appear
        in global intelligence until their first session log."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        # Use a mock for _register_global to verify it's called with the
        # right args without actually hitting a real global.db file.
        from unittest.mock import MagicMock
        mock_register = MagicMock()
        monkeypatch.setattr("mcp_server.auto_init._register_global", mock_register)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        # metadata.json must exist with identity fields
        meta_path = data_dir / "metadata.json"
        assert meta_path.exists(), "bootstrap must write metadata.json for rename resilience"
        import json as _json
        meta = _json.loads(meta_path.read_text())
        assert meta["original_path"] == str(project)
        assert meta["auto_initialized"] is True
        # Global registration must have been invoked
        assert mock_register.call_count == 1

    def test_legacy_in_project_layout_prints_migration_hint(
        self, tmp_path, monkeypatch, capsys,
    ):
        """v1.5-era user has project/.codevira/ (legacy layout). Running
        configure should print a hint about `codevira init` for migration."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        # Legacy: data_dir is INSIDE the project root
        data_dir = project / ".codevira"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text(yaml.safe_dump({
            "project": {"watched_dirs": ["src"], "file_extensions": [".py"]}
        }))

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        monkeypatch.setattr("mcp_server.auto_init._register_global", lambda *a, **k: None)

        rc = cmd_configure(interactive=False, dirs_arg="src,tests", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        err = capsys.readouterr().err
        assert "legacy in-project" in err
        assert "codevira init" in err

    def test_centralized_layout_does_not_print_migration_hint(
        self, configured_project, capsys,
    ):
        """Centralized data_dir (not inside project) → no migration hint."""
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        err = capsys.readouterr().err
        assert "legacy in-project" not in err

    def test_heals_partial_migration_state(
        self, tmp_path, monkeypatch,
    ):
        """Partial migration: centralized has config.yaml but no metadata.json.
        Configure should HEAL the metadata (round-9 heal logic extended)."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "slug"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_text(yaml.safe_dump({
            "project": {"watched_dirs": ["src"], "file_extensions": [".py"]}
        }))
        # No metadata.json — partial migration state
        assert not (data_dir / "metadata.json").exists()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        monkeypatch.setattr("mcp_server.auto_init._register_global", lambda *a, **k: None)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        # Heal logic (from round 9) should have created metadata.json
        assert (data_dir / "metadata.json").exists()

    def test_binary_garbage_config_yaml_exits_cleanly(
        self, tmp_path, monkeypatch, capsys,
    ):
        """config.yaml filled with binary junk (filesystem corruption,
        accidental `cp` from a binary) must not crash with
        UnicodeDecodeError. Caught and reported as malformed YAML."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_bytes(b"\x00\x01\x02\xff\xfe\xfd")

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "malformed" in err.lower()
        assert "utf-8" in err.lower() or "utf8" in err.lower()

    def test_config_yaml_is_directory_exits_cleanly(
        self, tmp_path, monkeypatch, capsys,
    ):
        """If config.yaml is a directory (weird state: user manually mkdir'd
        it, filesystem bug, etc.), error with a clear message — don't crash
        with IsADirectoryError."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj"
        data_dir.mkdir(parents=True)
        # Oops: config.yaml is a dir not a file
        (data_dir / "config.yaml").mkdir()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not a regular file" in err
        assert "Remove it by hand" in err

    def test_broken_symlink_triggers_bootstrap(
        self, tmp_path, monkeypatch, capsys,
    ):
        """If config.yaml is a dangling symlink, treat as missing and
        bootstrap. (is_file() returns False for broken symlinks.)"""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").symlink_to(tmp_path / "nonexistent")

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        monkeypatch.setattr("mcp_server.auto_init._register_global", lambda *a, **k: None)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=".py",
                           reindex=False, dry_run=False)
        assert rc == 0

    def test_reindex_exception_returns_exit_1_config_still_saved(
        self, configured_project, monkeypatch, capsys,
    ):
        """If cmd_full_rebuild raises during the reindex step, configure
        must NOT propagate a traceback. Config was already written — print
        a friendly error, let the config stand, exit 1."""
        project, data_dir = configured_project

        # TTY + user says 'y' to reindex
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True), \
             patch("indexer.index_codebase.cmd_full_rebuild",
                   side_effect=RuntimeError("ChromaDB disk full")):
            rc = cmd_configure(interactive=False, dirs_arg="src,lib",
                               exts_arg=None, reindex=True, dry_run=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Rebuild failed" in err
        assert "ChromaDB disk full" in err
        assert "Your config IS saved" in err
        # Config WAS saved
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["lib", "src"]

    def test_ctrl_c_during_scan_exits_via_outer_handler(
        self, configured_project, monkeypatch, capsys,
    ):
        """Ctrl+C during scan_project (before prompts) must NOT propagate a
        traceback. The dispatcher in cli.py wraps cmd_configure with a
        top-level KeyboardInterrupt handler that exits with POSIX 130."""
        project, data_dir = configured_project
        monkeypatch.setattr("mcp_server.cli_configure.scan_project",
                            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))

        # Simulate the cli.py dispatch branch
        import sys as _sys
        monkeypatch.setattr(_sys, "argv", ["codevira", "configure"])

        with pytest.raises(SystemExit) as exc_info:
            from mcp_server.cli import main
            main()
        assert exc_info.value.code == 130
        out = capsys.readouterr().out
        assert "Aborted." in out

    def test_reindex_keyboard_interrupt_exits_cleanly(
        self, configured_project, monkeypatch, capsys,
    ):
        """Ctrl+C during cmd_full_rebuild must exit cleanly with exit 0.
        Config was already saved; the rebuild is a recoverable follow-up."""
        project, data_dir = configured_project

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True), \
             patch("indexer.index_codebase.cmd_full_rebuild",
                   side_effect=KeyboardInterrupt):
            rc = cmd_configure(interactive=False, dirs_arg="src,lib",
                               exts_arg=None, reindex=True, dry_run=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Rebuild interrupted" in out
        assert "Config is saved" in out
        assert "codevira index --full" in out

    def test_bootstrap_tolerates_auto_detect_failure(self, tmp_path, monkeypatch, capsys):
        """If auto_detect_project itself raises (broken project layout, etc.),
        bootstrap must NOT crash the user with a traceback. Fall back to a
        minimal 'unknown' stub and let the user pick dirs/extensions."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        monkeypatch.setattr(
            "mcp_server.detect.auto_detect_project",
            lambda root: (_ for _ in ()).throw(RuntimeError("detect blew up")),
        )

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=".py",
                           reindex=False, dry_run=False)
        assert rc == 0
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]
        assert cfg["project"]["file_extensions"] == [".py"]
        # The fallback used "unknown" language since detection failed
        assert cfg["project"]["language"] == "unknown"

    def test_heals_missing_metadata_on_existing_config(
        self, tmp_path, monkeypatch,
    ):
        """Upgrade scenario: user has v1.7-era config.yaml but no metadata.json
        (post v1.5→v1.6 migration, or auto_init never fired). Running
        `codevira configure` must HEAL the missing metadata so rename-
        resilient lookup starts working."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_text(yaml.safe_dump({
            "project": {"name": "proj", "language": "python",
                        "watched_dirs": ["src"], "file_extensions": [".py"]}
        }))
        assert not (data_dir / "metadata.json").exists()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        # Don't actually touch global.db for this test
        monkeypatch.setattr("mcp_server.auto_init._register_global", lambda *a, **k: None)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        assert (data_dir / "metadata.json").exists(), "metadata.json must be healed"

    def test_does_not_clobber_existing_metadata(
        self, tmp_path, monkeypatch,
    ):
        """Existing metadata.json must NOT be rewritten (would clobber
        created_at and break any external tooling that relies on it)."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_text(yaml.safe_dump({
            "project": {"watched_dirs": ["src"], "file_extensions": [".py"]}
        }))
        meta = data_dir / "metadata.json"
        meta.write_text('{"path_key": "original", "version": "1.5.0"}')
        original_content = meta.read_text()
        original_mtime = meta.stat().st_mtime_ns

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        monkeypatch.setattr("mcp_server.auto_init._register_global", lambda *a, **k: None)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        assert meta.read_text() == original_content, "heal must not clobber existing metadata"
        assert meta.stat().st_mtime_ns == original_mtime, "heal must not touch existing metadata"

    def test_bootstrap_tolerates_metadata_write_failure(
        self, tmp_path, monkeypatch,
    ):
        """If metadata.json or global.db registration fail, bootstrap must
        still succeed — these are nice-to-haves, not blockers."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr("mcp_server.auto_init._write_metadata", _raise)
        monkeypatch.setattr("mcp_server.auto_init._register_global", _raise)

        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0, "bootstrap must survive helper failures"
        # Config was still written
        assert (data_dir / "config.yaml").exists()

    def test_bootstraps_config_when_missing(self, tmp_path, monkeypatch, capsys):
        """After `codevira register` the data_dir exists but config.yaml does NOT
        (register doesn't write it — only auto_init does, lazily on first MCP
        tool call). cmd_configure must bootstrap config.yaml itself using
        auto_detect_project, not error out."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        # Centralized data_dir EXISTS but has no config.yaml (register was run;
        # auto_init never fired because no MCP tool call happened yet).
        data_dir = tmp_path / "centralized" / "projects" / "proj-key"
        data_dir.mkdir(parents=True)
        assert not (data_dir / "config.yaml").exists()

        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        # Non-dry-run — bootstrap must actually write the file.
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        assert (data_dir / "config.yaml").exists(), "bootstrap must create config.yaml"
        out = capsys.readouterr().out
        assert "Bootstrapped default config.yaml" in out

    def test_errors_on_corrupt_config(self, configured_project, monkeypatch, capsys):
        _, data_dir = configured_project
        (data_dir / "config.yaml").write_text("this: is:\n  not: valid: yaml:")
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "malformed" in err.lower()

    def test_non_interactive_dirs_only(self, configured_project, capsys):
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src,lib", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["lib", "src"]  # sorted
        assert cfg["project"]["file_extensions"] == [".py"]  # unchanged

    def test_non_interactive_mixed_flags(self, configured_project):
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg="py,ts",
                           reindex=False, dry_run=False)
        assert rc == 0
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]
        assert cfg["project"]["file_extensions"] == [".py", ".ts"]

    def test_non_interactive_skips_reindex_on_non_tty(self, configured_project, monkeypatch, capsys):
        project, data_dir = configured_project
        # reindex=True but non-interactive path shouldn't prompt nor reindex
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg=None,
                           reindex=True, dry_run=False)
        assert rc == 0
        out = capsys.readouterr().out
        # Should tell user to run index --full themselves
        assert "index --full" in out

    def test_dry_run_does_not_write(self, configured_project):
        project, data_dir = configured_project
        cfg_path = data_dir / "config.yaml"
        mtime_before = cfg_path.stat().st_mtime_ns
        original = cfg_path.read_text()
        rc = cmd_configure(interactive=False, dirs_arg="lib", exts_arg=None,
                           reindex=False, dry_run=True)
        assert rc == 0
        assert cfg_path.read_text() == original, "dry-run must not modify file"
        assert cfg_path.stat().st_mtime_ns == mtime_before

    def test_removes_dirs_when_unchecked(self, configured_project, capsys):
        project, data_dir = configured_project
        # Current: watched_dirs=['src']. Pass --dirs lib only → src is removed.
        rc = cmd_configure(interactive=False, dirs_arg="lib", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["lib"]
        out = capsys.readouterr().out
        assert "Removing from watched_dirs" in out

    def test_warns_on_nonexistent_dir_in_flag_but_writes(self, configured_project, capsys):
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src,doesnotexist",
                           exts_arg=None, reindex=False, dry_run=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "doesnotexist" in out and "does not exist" in out.lower()
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert "doesnotexist" in cfg["project"]["watched_dirs"]

    def test_empty_selection_in_non_interactive_errors_exit_2(self, configured_project, capsys):
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="", exts_arg=None,
                           reindex=False, dry_run=False)
        # --dirs="" = empty input, must fail
        assert rc == 2
        err = capsys.readouterr().err
        assert "--dirs" in err

    def test_empty_extensions_non_interactive_errors_exit_2(self, configured_project, capsys):
        """--extensions '' must fail — writing file_extensions: [] silently
        would re-create the 0-chunks bug we're trying to fix."""
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src", exts_arg="",
                           reindex=False, dry_run=False)
        assert rc == 2
        err = capsys.readouterr().err
        assert "--extensions" in err

    def test_non_interactive_keeps_current_exts_when_flag_omitted(
        self, configured_project,
    ):
        """Omitting --extensions must preserve the current file_extensions
        (not silently go empty)."""
        project, data_dir = configured_project
        rc = cmd_configure(interactive=False, dirs_arg="src,lib", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["file_extensions"] == [".py"]  # unchanged

    def test_ctrl_c_in_prompt_returns_exit_0(self, configured_project, monkeypatch):
        """Ctrl+C during the interactive prompt must abort cleanly (exit 0,
        no traceback leak to terminal)."""
        project, data_dir = configured_project

        def _raise_ki(_prompt):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_ki)
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            rc = cmd_configure(interactive=True, dirs_arg=None, exts_arg=None,
                               reindex=False, dry_run=False)
        assert rc == 0
        # Config unchanged
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]

    def test_permission_error_on_write_exits_1(self, tmp_path, monkeypatch, capsys):
        """Cannot write config.yaml \u2192 exit 1 with friendly error, no
        traceback."""
        from mcp_server import paths
        paths._data_dir_cache.clear()
        project = _make_project(tmp_path, {"src/a.py": "x=1"})
        data_dir = _init_codevira(project, {
            "name": "proj", "watched_dirs": ["src"], "file_extensions": [".py"],
        })
        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)

        def _raise_permission(*args, **kwargs):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(
            "mcp_server.cli_configure.write_config_patch", _raise_permission,
        )
        rc = cmd_configure(interactive=False, dirs_arg="src,lib", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "cannot write" in err.lower()
        assert "read-only filesystem" in err

    def test_user_aborts_q_returns_exit_0(self, configured_project, monkeypatch, capsys):
        """Interactive: user types 'q' at the first prompt → clean abort (exit 0)."""
        project, data_dir = configured_project
        monkeypatch.setattr("sys.stdin", io.StringIO("q\n"))
        with patch.object(cli_configure.sys.stdin, "isatty", return_value=True):
            rc = cmd_configure(interactive=True, dirs_arg=None, exts_arg=None,
                               reindex=False, dry_run=False)
        assert rc == 0
        # Config must NOT have been overwritten
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["src"]  # original

    def test_centralized_mode_data_dir_and_project_root_decoupled(
        self, configured_project, capsys,
    ):
        """Regression guard: data_dir lives in a DIFFERENT tree from the project.

        The fixture simulates v1.6+ centralized mode (``data_dir`` under
        ``~/.codevira/projects/<slug>/``, project tree elsewhere). cmd_configure
        must resolve project_root via get_project_root(), NOT ``data_dir.parent``.
        If that regression creeps back in, scan_project walks the centralized
        home and finds nothing → this assertion fails.
        """
        project, data_dir = configured_project
        # Sanity check the fixture: they're in DIFFERENT subtrees
        assert project not in data_dir.parents
        assert data_dir not in project.parents

        rc = cmd_configure(interactive=False, dirs_arg="src,lib", exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 0
        # scan_project must have discovered the real project files — proof:
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text())
        assert cfg["project"]["watched_dirs"] == ["lib", "src"]
        out = capsys.readouterr().out
        # Banner must show the REAL project name, not the centralized slug dir
        assert project.name in out

    def test_empty_project_returns_exit_2(self, tmp_path, monkeypatch, capsys):
        project = _make_project(tmp_path, {})  # no source files at all
        data_dir = _init_codevira(project, {"watched_dirs": ["src"], "file_extensions": [".py"]})
        monkeypatch.setattr("mcp_server.cli_configure.get_data_dir", lambda: data_dir)
        monkeypatch.setattr("mcp_server.cli_configure.get_project_root", lambda: project)
        from mcp_server import paths
        paths._data_dir_cache.clear()
        rc = cmd_configure(interactive=False, dirs_arg=None, exts_arg=None,
                           reindex=False, dry_run=False)
        assert rc == 2
        err = capsys.readouterr().err
        assert "no source files" in err.lower()
