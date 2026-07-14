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
    choose_binding,
    is_initialized_codevira_project,
    pick_project_root,
    resolve_project_from_file_path,
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


class TestAmbiguousMultiRoot:
    """v3.7.0 Lane-A safety net: 2+ .codevira roots is ambiguous. We keep the
    deterministic first-match (no behavior change) but surface it."""

    def _two_codevira(self, tmp_path: Path):
        a = tmp_path / "a"
        (a / ".codevira").mkdir(parents=True)
        b = tmp_path / "b"
        (b / ".codevira").mkdir(parents=True)
        return a, b

    def test_pick_is_unchanged_first_codevira_wins(self, tmp_path: Path) -> None:
        a, b = self._two_codevira(tmp_path)
        assert pick_project_root([a, b]) == a
        assert pick_project_root([b, a]) == b  # deterministic on input order

    def test_ambiguity_is_detected(self, tmp_path: Path) -> None:
        from mcp_server.project_binding import ambiguous_codevira_roots

        a, b = self._two_codevira(tmp_path)
        assert set(ambiguous_codevira_roots([a, b])) == {a, b}

    def test_single_codevira_is_not_ambiguous(self, tmp_path: Path) -> None:
        from mcp_server.project_binding import ambiguous_codevira_roots

        a = tmp_path / "a"
        (a / ".codevira").mkdir(parents=True)
        b = tmp_path / "b"
        (b / ".git").mkdir(parents=True)
        assert ambiguous_codevira_roots([a, b]) == []

    def test_ambiguity_is_logged(self, tmp_path: Path, caplog) -> None:
        import logging

        a, b = self._two_codevira(tmp_path)
        with caplog.at_level(logging.WARNING):
            pick_project_root([a, b])
        assert any("ambiguous" in r.message for r in caplog.records)


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

    def test_resolve_returns_raw_workspace_root(self, tmp_path: Path) -> None:
        """resolve returns the RAW best workspace root (even a .git-only
        repo); the bind/no-bind gating is choose_binding's job now."""
        repo = tmp_path / "monorepo"
        (repo / ".git").mkdir(parents=True)
        session = _session([_root(f"file://{repo}")])
        assert asyncio.run(resolve_project_root_from_roots(session)) == repo


class TestResolveProjectFromFilePath:
    """Per-call resolution: the file in a tool call points at its project."""

    def test_finds_enclosing_codevira_project(self, tmp_path: Path) -> None:
        proj = tmp_path / "projA"
        (proj / ".codevira").mkdir(parents=True)
        (proj / "src").mkdir()
        f = proj / "src" / "main.py"
        f.write_text("x=1\n")
        assert resolve_project_from_file_path(str(f)) == proj

    def test_finds_project_for_nonexistent_file(self, tmp_path: Path) -> None:
        # A new file that doesn't exist yet still resolves via its ancestors.
        proj = tmp_path / "projB"
        (proj / ".codevira").mkdir(parents=True)
        ghost = proj / "src" / "newmodule" / "thing.py"  # not created
        assert resolve_project_from_file_path(str(ghost)) == proj

    def test_directory_path_resolves(self, tmp_path: Path) -> None:
        proj = tmp_path / "projC"
        (proj / ".codevira").mkdir(parents=True)
        sub = proj / "pkg"
        sub.mkdir()
        assert resolve_project_from_file_path(str(sub)) == proj

    def test_no_codevira_ancestor_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        (plain / "src").mkdir(parents=True)
        assert resolve_project_from_file_path(str(plain / "src" / "f.py")) is None

    def test_none_and_empty(self) -> None:
        assert resolve_project_from_file_path(None) is None
        assert resolve_project_from_file_path("") is None


class TestChooseBinding:
    """H3: the bind decision weighs the client's workspace root against
    what cwd discovery resolves to."""

    def test_codevira_workspace_binds(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / ".codevira").mkdir(parents=True)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        assert choose_binding(ws, cwd) == ws

    def test_fresh_git_workspace_binds_when_cwd_uninitialized(
        self, tmp_path: Path
    ) -> None:
        # Brand-new project: bind to the workspace so auto-init doesn't land
        # in the wrong inherited cwd.
        ws = tmp_path / "fresh"
        (ws / ".git").mkdir(parents=True)
        cwd = tmp_path / "cwd"
        cwd.mkdir()  # not a codevira project
        assert choose_binding(ws, cwd) == ws

    def test_git_workspace_does_not_override_initialized_cwd(
        self, tmp_path: Path
    ) -> None:
        # Monorepo protection: cwd already points at a real .codevira
        # subproject — never hijack it with the (uninitialized) repo root.
        ws = tmp_path / "monorepo"
        (ws / ".git").mkdir(parents=True)
        cwd = tmp_path / "sub"
        (cwd / ".codevira").mkdir(parents=True)
        assert choose_binding(ws, cwd) is None

    def test_non_repo_workspace_keeps_cwd(self, tmp_path: Path) -> None:
        ws = tmp_path / "plain"
        ws.mkdir()  # neither .git nor .codevira
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        assert choose_binding(ws, cwd) is None

    def test_none_workspace_keeps_cwd(self, tmp_path: Path) -> None:
        assert choose_binding(None, tmp_path) is None


class TestIsInitialized:
    def test_true_for_codevira_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".codevira").mkdir()
        assert is_initialized_codevira_project(tmp_path) is True

    def test_false_for_plain_dir(self, tmp_path: Path) -> None:
        assert is_initialized_codevira_project(tmp_path) is False

    def test_false_for_none(self) -> None:
        assert is_initialized_codevira_project(None) is False
