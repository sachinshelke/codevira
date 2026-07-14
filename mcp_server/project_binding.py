"""project_binding.py — resolve the active project from the MCP client.

The binding bug this fixes: a **user-scope** codevira MCP server (one
shared entry in the IDE config, launched with no ``cwd`` and no
``--project-dir`` / ``CODEVIRA_PROJECT_DIR``) has no project binding. It
falls through to cwd discovery and resolves to whatever directory the
server process happened to inherit — frequently the WRONG project. The
symptom is cross-project memory contamination (decisions/graph/sessions
read from project A while the user is working in project B).

The fix: when the server is not explicitly pinned, ask the MCP client
for its workspace **roots** (which every modern client — Claude Code,
Cursor, Windsurf — exposes) and bind to the real project. Pure helpers
here are unit-tested; the server wires :func:`resolve_project_root_from_roots`
into ``call_tool`` (once per process, best-effort, never blocking).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


def root_uri_to_path(uri: str | None) -> Path | None:
    """Convert an MCP root URI to a Path, cross-platform.

    Handles the three real-world ``file://`` shapes (Cursor / Windsurf run
    on Windows, so these matter):
      - POSIX:           ``file:///Users/x/proj``     -> ``/Users/x/proj``
      - Windows drive:   ``file:///C:/Users/x/proj``  -> ``C:/Users/x/proj``
        (urlparse leaves a spurious leading slash before the drive letter)
      - Windows UNC:     ``file://host/share/proj``   -> ``//host/share/proj``
        (the authority lands in ``netloc``; dropping it would mis-bind)

    ``localhost`` / empty authority is treated as local. Returns None for
    empty input or a non-``file://`` scheme (a remote root we can't map).

    Args:
        uri: The root's URI string, or None.

    Returns:
        The local filesystem Path, or None when unmappable.
    """
    if not uri:
        return None
    s = str(uri)
    if s.startswith("file://"):
        parsed = urlparse(s)
        netloc = parsed.netloc
        path = unquote(parsed.path)
        if netloc and netloc.lower() != "localhost":
            # UNC: file://host/share/... -> //host/share/... (keep the host).
            path = f"//{netloc}{path}"
        elif len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
            # Windows drive: /C:/Users/... -> C:/Users/...
            path = path[1:]
        if not path:
            return None
        s = path
    elif "://" in s:
        # Some non-local scheme — not a local project root.
        return None
    try:
        return Path(s)
    except (ValueError, OSError):
        return None


def pick_project_root(candidates: list[Path | None]) -> Path | None:
    """Choose the best project root from candidate workspace roots.

    Preference order, among roots that are real directories and not
    refused system roots:
      1. a root that already has a ``.codevira/`` dir (an initialized
         codevira project);
      2. a root that has a ``.git/`` dir (a repo);
      3. the first valid root.

    Args:
        candidates: Candidate roots (None entries are ignored).

    Returns:
        The chosen project root, or None if none are usable.
    """
    from mcp_server.paths import is_invalid_project_root

    valid: list[Path] = []
    for c in candidates:
        if c is None:
            continue
        try:
            if not c.is_dir():
                continue
            if is_invalid_project_root(c) is not None:
                continue
        except OSError:
            continue
        valid.append(c)

    if not valid:
        return None

    codevira_roots = [c for c in valid if _has_codevira(c)]
    if len(codevira_roots) > 1:
        # Lane-A safety net (v3.7.0): a multi-root workspace with 2+ initialized
        # codevira projects is AMBIGUOUS — we still pick deterministically (the
        # first) so behavior is unchanged, but we no longer do it SILENTLY. The
        # user can pin the intended one with --project-dir / CODEVIRA_PROJECT_DIR.
        logger.warning(
            "project binding is ambiguous: %d workspace roots have a .codevira/ "
            "(%s). Binding to the first (%s). Pin the intended project with "
            "`--project-dir` or CODEVIRA_PROJECT_DIR to remove the ambiguity.",
            len(codevira_roots),
            ", ".join(str(c) for c in codevira_roots),
            codevira_roots[0],
        )
    if codevira_roots:
        return codevira_roots[0]
    for c in valid:
        if (c / ".git").is_dir():
            return c
    return valid[0]


def _has_codevira(path: Path) -> bool:
    try:
        return (path / ".codevira").is_dir()
    except OSError:
        return False


def ambiguous_codevira_roots(candidates: list[Path | None]) -> list[Path]:
    """Return the initialized-codevira roots among ``candidates`` when there is
    MORE THAN ONE (i.e. the binding is ambiguous); otherwise an empty list.

    Pure + unit-testable. Callers use this to surface the ambiguity (e.g. in
    ``get_session_context``) so a silently-wrong multi-root binding becomes a
    visible, correctable event rather than a guess."""
    roots: list[Path] = []
    for c in candidates:
        if c is None:
            continue
        try:
            if c.is_dir() and _has_codevira(c):
                roots.append(c)
        except OSError:
            continue
    return roots if len(roots) > 1 else []


def is_initialized_codevira_project(path: Path | None) -> bool:
    """True if ``path`` is an already-initialized codevira project.

    Used as the conservative gate for RE-binding a running server: we
    only override the current project binding when the target is certain
    to be an established codevira project (has a ``.codevira/`` dir). This
    prevents a monorepo workspace root (``.git`` but no ``.codevira``) or
    a fresh repo from hijacking a working setup — those keep whatever the
    server already resolved.

    Args:
        path: A candidate project root, or None.

    Returns:
        True only when ``path`` exists and contains ``.codevira/``.
    """
    if path is None:
        return False
    try:
        return (path / ".codevira").is_dir()
    except OSError:
        return False


async def resolve_project_root_from_roots(
    session: Any,
    *,
    timeout: float = 2.0,
) -> Path | None:
    """Query the MCP client's workspace roots and return the best project.

    Best-effort and bounded: a client that doesn't support roots, errors,
    or is slow yields None (the caller then falls back to cwd discovery).
    Never raises.

    Args:
        session: The MCP server session (must expose async ``list_roots``).
        timeout: Max seconds to wait for the client's roots reply.

    Returns:
        The resolved project root, or None.
    """
    try:
        result = await asyncio.wait_for(session.list_roots(), timeout)
    except Exception:  # noqa: BLE001 — best-effort (timeout, no-roots, transport)
        return None
    roots = getattr(result, "roots", None) or []
    # Return the RAW best workspace root. The bind/no-bind decision — incl.
    # the conservative monorepo / fresh-project gating — lives in
    # ``choose_binding`` so the caller can weigh it against cwd resolution.
    return pick_project_root([root_uri_to_path(getattr(r, "uri", None)) for r in roots])


def choose_binding(
    workspace_root: Path | None,
    cwd_root: Path | None,
) -> Path | None:
    """Decide which project a running server should bind to.

    Given the client's chosen workspace root (from
    :func:`resolve_project_root_from_roots`) and what cwd discovery
    currently resolves to, return the root to bind to, or None to keep the
    existing cwd binding.

    Rules:
      1. ``workspace_root`` has ``.codevira`` -> bind (an established
         codevira project; the client's workspace IS the project).
      2. ``workspace_root`` is a ``.git`` repo AND ``cwd_root`` is NOT an
         initialized codevira project -> bind (a brand-new project; this
         prevents auto-initing ``.codevira`` in the wrong inherited cwd).
         The cwd guard protects the monorepo-subdir case: if cwd already
         points at a real ``.codevira`` project, we never override it.
      3. otherwise -> None (keep cwd).

    Known limitation (documented; ``codevira doctor`` surfaces the binding):
    if cwd already resolves to a DIFFERENT initialized codevira project and
    the fresh workspace has no ``.codevira`` yet, rule 3 keeps cwd to avoid
    hijacking — pin ``--project-dir`` for that case.

    Args:
        workspace_root: The client's workspace root, or None.
        cwd_root: The project root cwd discovery resolves to, or None.

    Returns:
        The root to bind to, or None to keep the cwd binding.
    """
    if workspace_root is None:
        return None
    if is_initialized_codevira_project(workspace_root):
        return workspace_root
    try:
        is_repo = (workspace_root / ".git").is_dir()
    except OSError:
        is_repo = False
    if is_repo and not is_initialized_codevira_project(cwd_root):
        return workspace_root
    return None


def resolve_project_from_file_path(file_path: str | None) -> Path | None:
    """Find the initialized codevira project that contains ``file_path``.

    Walks up from the file's directory to the first ancestor that has a
    ``.codevira/`` dir (and is not a refused system root). Used by GLOBAL
    clients with no per-conversation workspace signal (Claude Desktop) to
    drive memory by the project the user is actually working in — the tool
    call's path is the only project signal available there.

    Works on a path string even if the file doesn't exist yet (new file):
    only the ancestor directories are stat'd. Returns None for empty input
    or when no enclosing codevira project is found.

    Args:
        file_path: A file or directory path from a tool call, or None.

    Returns:
        The enclosing initialized codevira project root, or None.
    """
    if not file_path:
        return None
    from mcp_server.paths import is_invalid_project_root

    try:
        p = Path(file_path)
    except (OSError, ValueError):
        return None
    try:
        is_dir = p.is_dir()
    except OSError:
        is_dir = False
    start = p if is_dir else p.parent
    for cand in [start, *start.parents]:
        try:
            if (cand / ".codevira").is_dir() and is_invalid_project_root(cand) is None:
                return cand
        except OSError:
            continue
    return None
