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


class TestSubdirectoryRoots:
    """A workspace root can be a SUBDIRECTORY of the project.

    Measured against Claude Code 2.1.221 (D00013U): ``roots/list`` returns the
    literal session cwd, so opening ``~/repo/packages/web`` advertises that
    path and not ``~/repo``. Such a root has neither ``.codevira`` nor ``.git``,
    so before the walk-up it matched no branch of pick_project_root, fell
    through to ``valid[0]``, and choose_binding then kept the inherited cwd —
    i.e. the wrong project, which is the whole bug roots-binding exists to fix.
    """

    def test_subdir_resolves_to_enclosing_codevira_project(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "repo"
        (proj / ".codevira").mkdir(parents=True)
        web = proj / "packages" / "web"
        web.mkdir(parents=True)
        assert pick_project_root([web]) == proj

    def test_two_subdirs_of_one_project_are_not_ambiguous(
        self, tmp_path: Path, caplog
    ) -> None:
        """--add-dir of two subdirs of ONE repo is not a multi-project
        workspace; de-duping after the walk-up keeps the ambiguity warning
        for the case it was written for."""
        import logging

        proj = tmp_path / "repo"
        (proj / ".codevira").mkdir(parents=True)
        a, b = proj / "packages" / "a", proj / "packages" / "b"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            assert pick_project_root([a, b]) == proj
        assert not [r for r in caplog.records if "ambiguous" in r.message]

    def test_walkup_never_escapes_to_home(self, tmp_path: Path, monkeypatch) -> None:
        """$HOME ALWAYS has a .codevira (codevira's own global home lives at
        ~/.codevira), so an unguarded walk-up would bind every marker-less
        folder to $HOME — the v1.8.0 rogue-$HOME-project crash."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codevira").mkdir()
        plain = tmp_path / "scratch"
        plain.mkdir()
        assert pick_project_root([plain]) == plain

    def test_git_only_subdir_resolves_to_its_repo_root(self, tmp_path: Path) -> None:
        """A subdirectory of a bare repo resolves to the REPO ROOT.

        An earlier cut left it as-is, reasoning that walking to a bare ``.git``
        might auto-init at a monorepo root nobody opted into. That reasoning
        does not survive the sibling-repo case: stopping the walk without
        returning the repo means ``~/Projects/myapp/src`` binds to neither
        ``myapp`` nor anything useful, while continuing past ``myapp/.git``
        binds every sibling repo to one shared store.

        The repo root is also what ``paths._discover_project_root`` returns for
        the same path — it has always treated ``.git`` as a project marker — so
        the two layers now agree rather than answering "what project is this
        path in?" differently. ``choose_binding`` rule 2 remains the guard
        against actually initializing anything: it will not bind a bare repo
        when cwd is already an initialized project.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "packages" / "web"
        sub.mkdir(parents=True)
        assert pick_project_root([sub]) == repo

    def test_end_to_end_through_resolve_from_roots(self, tmp_path: Path) -> None:
        proj = tmp_path / "repo"
        (proj / ".codevira").mkdir(parents=True)
        web = proj / "packages" / "web"
        web.mkdir(parents=True)
        session = _session([_root(f"file://{web}")])
        assert asyncio.run(resolve_project_root_from_roots(session)) == proj

    def test_a_root_that_is_its_own_repo_is_never_walked_up(
        self, tmp_path: Path
    ) -> None:
        """An ancestor's .codevira must not swallow a root that already has a
        project identity of its own.

        Real shape: someone ran `codevira init` in ~/Projects, then opens
        ~/Projects/myapp — a fresh repo. Walking up would bind the whole
        parent folder and every sibling project's memory with it. The walk-up
        is for ORPHAN roots only: those with neither marker.
        """
        outer = tmp_path / "Projects"
        (outer / ".codevira").mkdir(parents=True)
        app = outer / "myapp"
        (app / ".git").mkdir(parents=True)
        assert pick_project_root([app]) == app

    def test_the_walk_stops_at_a_git_boundary(self, tmp_path: Path) -> None:
        """The walk must not cross OUT of a repo to find a .codevira above it.

        Shape: `codevira init` was run in ~/Projects, and ~/Projects/myapp is
        its own repo. Opening ~/Projects/myapp/src advertises a root that is
        neither a codevira project nor a repo, so it gets walked up — and the
        first cut walked straight past myapp/.git to Projects/.codevira. Every
        sibling repo under Projects then shares one store: cross-project memory
        bleed, which is precisely what the orphan guard exists to prevent. The
        guard only covered a root that IS a repo, not one INSIDE a repo.
        """
        outer = tmp_path / "Projects"
        (outer / ".codevira").mkdir(parents=True)
        app = outer / "myapp"
        (app / ".git").mkdir(parents=True)
        src = app / "src"
        src.mkdir()
        assert pick_project_root([src]) == app

    def test_the_walk_still_crosses_plain_directories(self, tmp_path: Path) -> None:
        """Only a repo boundary stops it — an ordinary intermediate folder
        (packages/, apps/) must still resolve to the enclosing project."""
        proj = tmp_path / "repo"
        (proj / ".codevira").mkdir(parents=True)
        (proj / ".git").mkdir()
        deep = proj / "packages" / "web" / "src"
        deep.mkdir(parents=True)
        assert pick_project_root([deep]) == proj

    def test_a_nested_codevira_project_keeps_itself(self, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        (outer / ".codevira").mkdir(parents=True)
        inner = outer / "vendored"
        (inner / ".codevira").mkdir(parents=True)
        assert pick_project_root([inner]) == inner

    def test_subdir_loses_to_a_directly_advertised_project(
        self, tmp_path: Path
    ) -> None:
        """Order is preserved after the walk-up: a subdir listed first still
        resolves to its parent, and that parent still wins as the first
        .codevira root."""
        outer = tmp_path / "outer"
        (outer / ".codevira").mkdir(parents=True)
        sub = outer / "pkg"
        sub.mkdir()
        other = tmp_path / "other"
        (other / ".codevira").mkdir(parents=True)
        assert pick_project_root([sub, other]) == outer


class TestIsInitialized:
    def test_true_for_codevira_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".codevira").mkdir()
        assert is_initialized_codevira_project(tmp_path) is True

    def test_false_for_plain_dir(self, tmp_path: Path) -> None:
        assert is_initialized_codevira_project(tmp_path) is False

    def test_false_for_none(self) -> None:
        assert is_initialized_codevira_project(None) is False
