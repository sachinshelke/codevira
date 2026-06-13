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
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def root_uri_to_path(uri: str | None) -> Path | None:
    """Convert an MCP root URI (``file:///abs/path``) to a Path.

    Returns None for empty input or a non-``file://`` scheme (e.g. a
    remote root we can't map to a local project).

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
        s = unquote(parsed.path)
        if not s:
            return None
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
    for c in valid:
        if (c / ".codevira").is_dir():
            return c
    for c in valid:
        if (c / ".git").is_dir():
            return c
    return valid[0]


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
    chosen = pick_project_root(
        [root_uri_to_path(getattr(r, "uri", None)) for r in roots]
    )
    # Conservative re-bind: only an already-initialized codevira project is
    # a safe target. A monorepo workspace root (.git, no .codevira) or a
    # fresh repo must not hijack whatever the server already resolved — for
    # those, return None and let cwd discovery stand.
    return chosen if is_initialized_codevira_project(chosen) else None
