"""Tests for mcp_server/update_check.py — the "update available" notice.

Contract under test (v3.3.0):
  - command path is cache-read only: no network, never raises
  - notice prints to stderr iff cached latest > current version
  - serve / engine / bare invocations and CODEVIRA_NO_UPDATE_CHECK skip
  - stale cache spawns exactly one detached refresh per TTL window
  - refresh_cache() writes the cache atomically; failures record error
"""

from __future__ import annotations

import json
import time

import pytest

from mcp_server import __version__
from mcp_server import update_check as uc


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the update-check cache at a temp dir and clear the opt-out."""
    import mcp_server.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_global_home", lambda: tmp_path)
    monkeypatch.delenv("CODEVIRA_NO_UPDATE_CHECK", raising=False)
    return tmp_path


def _write_cache(cache_dir, **payload):
    (cache_dir / "update_check.json").write_text(json.dumps(payload))


# ----------------------------------------------------------------------
# Version comparison
# ----------------------------------------------------------------------


class TestIsNewer:
    def test_newer_patch(self):
        assert uc._is_newer("3.3.0", "3.2.0") is True

    def test_equal(self):
        assert uc._is_newer("3.2.0", "3.2.0") is False

    def test_older(self):
        assert uc._is_newer("3.1.9", "3.2.0") is False

    def test_length_padding(self):
        assert uc._is_newer("3.3", "3.3.0") is False
        assert uc._is_newer("3.3.0.1", "3.3.0") is True

    def test_prerelease_never_notifies(self):
        assert uc._is_newer("4.0.0rc1", "3.2.0") is False

    def test_garbage_never_notifies(self):
        assert uc._is_newer("not-a-version", "3.2.0") is False
        assert uc._is_newer("", "3.2.0") is False


# ----------------------------------------------------------------------
# maybe_notify — the command-path entry
# ----------------------------------------------------------------------


class TestMaybeNotify:
    def test_notice_printed_when_newer(self, cache_dir, capsys, monkeypatch):
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: None)
        _write_cache(cache_dir, latest="99.0.0", checked_at=time.time())
        uc.maybe_notify("status")
        err = capsys.readouterr().err
        assert "Update available" in err
        assert "99.0.0" in err
        assert __version__ in err
        assert "pipx upgrade codevira" in err

    def test_silent_when_current(self, cache_dir, capsys, monkeypatch):
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: None)
        _write_cache(cache_dir, latest=__version__, checked_at=time.time())
        uc.maybe_notify("status")
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("command", ["serve", "engine", None])
    def test_skip_commands_no_notice_no_spawn(
        self, cache_dir, capsys, monkeypatch, command
    ):
        spawned = []
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: spawned.append(1))
        _write_cache(cache_dir, latest="99.0.0", checked_at=0)
        uc.maybe_notify(command)
        assert capsys.readouterr().err == ""
        assert spawned == []

    def test_env_opt_out(self, cache_dir, capsys, monkeypatch):
        spawned = []
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: spawned.append(1))
        monkeypatch.setenv("CODEVIRA_NO_UPDATE_CHECK", "1")
        _write_cache(cache_dir, latest="99.0.0", checked_at=0)
        uc.maybe_notify("status")
        assert capsys.readouterr().err == ""
        assert spawned == []

    def test_stale_cache_spawns_refresh_once(self, cache_dir, monkeypatch):
        spawned = []
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: spawned.append(1))
        _write_cache(cache_dir, latest=__version__, checked_at=0)  # ancient
        uc.maybe_notify("status")
        # checked_at was stamped before the spawn → second call in the
        # same TTL window must NOT spawn again (P5: bounded).
        uc.maybe_notify("status")
        assert spawned == [1]

    def test_fresh_cache_no_spawn(self, cache_dir, monkeypatch):
        spawned = []
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: spawned.append(1))
        _write_cache(cache_dir, latest=__version__, checked_at=time.time())
        uc.maybe_notify("status")
        assert spawned == []

    def test_missing_cache_spawns_and_never_raises(self, cache_dir, monkeypatch):
        spawned = []
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: spawned.append(1))
        uc.maybe_notify("status")  # no cache file at all
        assert spawned == [1]

    def test_corrupt_cache_never_raises(self, cache_dir, capsys, monkeypatch):
        monkeypatch.setattr(uc, "_spawn_refresh", lambda: None)
        (cache_dir / "update_check.json").write_text("{not json!!")
        uc.maybe_notify("status")  # must not raise
        assert "Update available" not in capsys.readouterr().err


# ----------------------------------------------------------------------
# refresh_cache — the detached-subprocess worker
# ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRefreshCache:
    def test_success_writes_latest(self, cache_dir, monkeypatch):
        body = json.dumps({"info": {"version": "9.9.9"}}).encode()
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, context=None: _FakeResponse(body),
        )
        assert uc.refresh_cache() == 0
        cache = json.loads((cache_dir / "update_check.json").read_text())
        assert cache["latest"] == "9.9.9"
        assert cache["error"] is None
        assert isinstance(cache["checked_at"], float)

    def test_network_failure_records_error(self, cache_dir, monkeypatch):
        def boom(req, timeout, context=None):
            raise OSError("network down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert uc.refresh_cache() == 1
        cache = json.loads((cache_dir / "update_check.json").read_text())
        assert "network down" in cache["error"]
        assert "latest" not in cache  # no stale lie about availability

    def test_prerelease_from_pypi_rejected(self, cache_dir, monkeypatch):
        body = json.dumps({"info": {"version": "9.9.9rc1"}}).encode()
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, context=None: _FakeResponse(body),
        )
        assert uc.refresh_cache() == 1
        cache = json.loads((cache_dir / "update_check.json").read_text())
        assert "unrecognized version" in cache["error"]
