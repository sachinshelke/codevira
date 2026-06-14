"""test_project_binding.py — runtime project binding from MCP client roots.

Fixes the user-scope-server misbinding where codevira resolved to the
wrong project (cross-project memory contamination). The pure helpers are
unit-tested here; the async resolver is exercised via ``asyncio.run`` so
no async test plugin is required.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from mcp_server.project_binding import (
    is_initialized_codevira_project,
    pick_project_root,
    resolve_project_root_from_roots,
    root_uri_to_path,
)


class TestRootUriToPath:
    def test_file_uri(self) -> None:
        assert root_uri_to_path("file:///Users/x/proj") == Path("/Users/x/proj")

    def test_file_uri_with_encoded_space(self) -> None:
        assert root_uri_to_path("file:///Users/x/my%20proj") == Path("/Users/x/my proj")

    def test_none_and_empty(self) -> None:
        assert root_uri_to_path(None) is None
        assert root_uri_to_path("") is None

    def test_non_file_scheme_is_none(self) -> None:
        assert root_uri_to_path("https://example.com/x") is None

    def test_bare_path(self) -> None:
        assert root_uri_to_path("/Users/x/proj") == Path("/Users/x/proj")

    # H2: Windows / UNC — Cursor & Windsurf run on Windows. Assert on the
    # path STRING so the cases are meaningful on any host OS.
    def test_windows_drive_letter(self) -> None:
        # urlparse leaves '/C:/...'; the spurious leading slash must go.
        assert str(root_uri_to_path("file:///C:/Users/x/proj")) == "C:/Users/x/proj"

    def test_windows_drive_letter_with_encoded_space(self) -> None:
        assert str(root_uri_to_path("file:///C:/My%20Code/app")) == "C:/My Code/app"

    def test_unc_path_keeps_host(self) -> None:
        # The authority (host) must NOT be dropped, or we mis-bind.
        assert str(root_uri_to_path("file://host/share/proj")) == "//host/share/proj"

    def test_localhost_authority_is_local(self) -> None:
        assert root_uri_to_path("file://localhost/Users/x/proj") == Path(
            "/Users/x/proj"
        )


class TestPickProjectRoot:
    def test_prefers_codevira_over_git(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        (a / ".codevira").mkdir(parents=True)
        b = tmp_path / "b"
        (b / ".git").mkdir(parents=True)
        # order shouldn't matter — .codevira wins
        assert pick_project_root([b, a]) == a

    def test_falls_back_to_git(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        (b / ".git").mkdir(parents=True)
        assert pick_project_root([a, b]) == b

    def test_first_valid_when_no_markers(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        assert pick_project_root([a, b]) == a

    def test_skips_none_and_missing(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        assert pick_project_root([None, tmp_path / "missing", a]) == a

    def test_skips_forbidden_roots(self) -> None:
        # '/' is a refused system root even though it is a directory.
        assert pick_project_root([Path("/")]) is None

    def test_empty(self) -> None:
        assert pick_project_root([]) is None
        assert pick_project_root([None]) is None


def _root(uri):
    return types.SimpleNamespace(uri=uri)


def _session(roots=None, *, raises: bool = False, sleep: float | None = None):
    class _S:
        async def list_roots(self):
            if raises:
                raise RuntimeError("client did not advertise roots")
            if sleep:
                await asyncio.sleep(sleep)
            return types.SimpleNamespace(roots=roots or [])

    return _S()


class TestResolveFromRoots:
    def test_resolves_codevira_project(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        (proj / ".codevira").mkdir(parents=True)
        session = _session([_root(f"file://{proj}")])
        assert asyncio.run(resolve_project_root_from_roots(session)) == proj

    def test_picks_codevira_among_multiple_roots(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        real = tmp_path / "real"
        (real / ".codevira").mkdir(parents=True)
        session = _session([_root(f"file://{plain}"), _root(f"file://{real}")])
        assert asyncio.run(resolve_project_root_from_roots(session)) == real

    def test_no_roots_returns_none(self) -> None:
        assert asyncio.run(resolve_project_root_from_roots(_session([]))) is None

    def test_client_error_returns_none(self) -> None:
        assert (
            asyncio.run(resolve_project_root_from_roots(_session(raises=True))) is None
        )

    def test_timeout_returns_none(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        (proj / ".codevira").mkdir(parents=True)
        session = _session([_root(f"file://{proj}")], sleep=0.2)
        result = asyncio.run(resolve_project_root_from_roots(session, timeout=0.01))
        assert result is None

    def test_git_only_root_is_not_bound(self, tmp_path: Path) -> None:
        """Conservative gate: a workspace root that's a git repo but NOT an
        initialized codevira project must not hijack the binding (monorepo
        / fresh-repo safety)."""
        repo = tmp_path / "monorepo"
        (repo / ".git").mkdir(parents=True)
        session = _session([_root(f"file://{repo}")])
        assert asyncio.run(resolve_project_root_from_roots(session)) is None


class TestIsInitialized:
    def test_true_for_codevira_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".codevira").mkdir()
        assert is_initialized_codevira_project(tmp_path) is True

    def test_false_for_plain_dir(self, tmp_path: Path) -> None:
        assert is_initialized_codevira_project(tmp_path) is False

    def test_false_for_none(self) -> None:
        assert is_initialized_codevira_project(None) is False
