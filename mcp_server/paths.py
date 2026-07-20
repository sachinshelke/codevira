"""
paths.py — Centralized path resolution for Codevira.

Resolution priority for get_data_dir():
  1. Centralized ~/.codevira/projects/<key>/ — new in v1.6 (keyed by project path)
  2. Git remote lookup — survives directory renames
  3. Legacy <project_root>/.codevira/ — backward compat for existing projects
  4. Default to centralized path for brand-new projects

Project root discovery (get_project_root()):
  - Uses --project-dir CLI override if set
  - Walks upward from cwd looking for project markers:
    .git, pyproject.toml, package.json, go.mod, Cargo.toml, .codevira/
  - Falls back to cwd if no marker found

Performance notes:
  - get_data_dir() result is cached per project root (_data_dir_cache).
    First call may spawn one `git remote` subprocess and scan metadata files;
    every subsequent call for the same root is a dict lookup (~0µs).
  - Call invalidate_data_dir_cache() after init/migration to force re-resolution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

# Allow overriding project directory via CLI flag (e.g. for Google Antigravity
# which doesn't support the `cwd` option in its MCP config) or via the
# CODEVIRA_PROJECT_DIR env var (v3.0.0 round-3 — for IDE MCP configs like
# Claude Desktop's mcpServers entry that pass no CLI args and have no
# meaningful working directory to anchor on).
_project_dir_override: Path | None = None
_PROJECT_DIR_ENV = "CODEVIRA_PROJECT_DIR"

# D000118 — project-root pin. get_project_root() used to re-resolve from
# Path.cwd() on every call, so a cwd change between record() and search() bound
# them to two different .codevira stores (non-monotonic / duplicate ids, reads
# that missed just-written records). We pin the first resolved root and reuse
# it; a later cwd/env drift logs a WARN but the pinned root wins.
#
# v3.7.0 (Phase 31) — the pin is a ContextVar, NOT a plain module global, so it
# is scoped per asyncio task/request. This is what makes a SINGLE MCP process
# safely serve MULTIPLE projects concurrently (the multiplex): each request
# runs in its own context (asyncio copies context per Task), so one request's
# pin can't clobber another's. Within one request the D000118 guarantee is
# unchanged. NOT a revert of D000118 — the pin logic is identical; only its
# scope narrows from process to request.
_pinned_root: ContextVar[Path | None] = ContextVar("codevira_pinned_root", default=None)
_drift_warned: set[Path] = set()

# v3.7.1 — the pin is OWNED BY THE TASK THAT SET IT.
#
# Problem being solved: a ContextVar set in an ancestor context (e.g. the
# `get_project_root()` call `main()` makes BEFORE `asyncio.run`) is inherited by
# every task the server spawns, and `_pinned_root.set(None)` can only clear it
# for the CURRENT task. The MCP SDK starts a fresh task per message
# (`tg.start_soon(self._handle_message, ...)`), so an explicit rebind took
# effect for exactly ONE tool call and every later call silently reverted to the
# inherited pin — the "my LH session returns UDAP's decisions" symptom.
#
# The first attempt used a plain module-global generation counter. That was a
# REGRESSION: bumping it invalidated the pin for every IN-FLIGHT task, so when
# one request rebound, a concurrent request's next call re-resolved against the
# other request's `_project_dir_override` (also a module global) and silently
# drifted into the wrong project — worse than the bug it replaced, since the
# drift warning below became unreachable.
#
# Recording the OWNER instead fixes both: a pin is only honored by the task that
# created it. An inherited pin has a foreign owner, so a fresh request re-
# resolves (fixing the original bug), while a task that pinned its own root
# keeps it no matter what siblings do (preserving the Phase 31 multiplex and the
# D000118 intra-request guarantee).
_pinned_owner: ContextVar[int | None] = ContextVar(
    "codevira_pinned_owner", default=None
)


def _current_owner() -> int | None:
    """Identify the current asyncio task, or None when running synchronously.

    Sync callers (CLI, tests) share the ``None`` owner, which preserves the
    original process-wide pin semantics for them.
    """
    try:
        import asyncio

        task = asyncio.current_task()
    except (RuntimeError, ImportError):
        return None
    return id(task) if task is not None else None


def reset_pinned_root() -> None:
    """Clear the project-root pin (+ drift warnings) for the current context.

    Called by set_project_dir / invalidate_data_dir_cache and by tests to keep
    the per-request pin from leaking. Idempotent.

    Deliberately does NOT reach into other tasks: a concurrent request's binding
    is its own business. Cross-task staleness is handled by pin ownership, not
    by invalidating everyone.
    """
    _pinned_root.set(None)
    _pinned_owner.set(None)
    _drift_warned.clear()


def set_project_dir(path: str | Path) -> None:
    """Override the project directory (called by CLI when --project-dir is passed).

    Also clears the data-dir cache AND the project-root pin so subsequent
    get_project_root()/get_data_dir() calls resolve against the new override,
    not a stale cached entry or an earlier-pinned root.
    """
    global _project_dir_override
    _project_dir_override = Path(path).resolve()
    invalidate_data_dir_cache()


# ---------------------------------------------------------------------------
# Path-key helpers (for centralized storage)
# ---------------------------------------------------------------------------

#: Markers that identify a project root when walking upward.
_PROJECT_MARKERS = frozenset(
    {
        ".git",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        ".codevira",
    }
)


#: Maximum length of the human-readable portion of a project key.
#: Filesystem `ENAMETOOLONG` limit is typically 255 bytes total for any
#: single path component on macOS APFS / Linux ext4. The slug is used as
#: a directory name under ``~/.codevira/projects/<slug>/`` and we also
#: append ``/graph/graph.db`` etc., so the slug itself must be well below
#: 255. Cap at 180 chars; the 8-char hash suffix preserves uniqueness
#: for collisions between paths that differ only after the truncation
#: point. Caught by Week-2 edge-case test (50-level-deep paths).
_MAX_KEY_LEN = 180


def _sanitize_path_key(abs_path: str | Path) -> str:
    """Convert an absolute path to a filesystem-safe key string.

    Uses a short hash suffix to prevent collisions between paths that
    differ only in separator characters (e.g. /foo-bar vs /foo/bar),
    drive letters (D:\\Projects\\Foo vs C:\\Projects\\Foo), or — since
    Week 2 — paths that differ only after the truncation point.

    Examples:
        /Users/alice/Projects/Foo            → Users_alice_Projects_Foo_a1b2c3d4
        /very/deeply/nested/.../50-levels    → very_deeply_nested..._a1b2c3d4
                                                (truncated at 180 chars; hash
                                                preserves uniqueness)

    Filesystem safety: the full slug (key + hash) is capped to ~189 chars,
    well under 255-byte ENAMETOOLONG limit on common filesystems.
    """
    import hashlib

    resolved = str(Path(abs_path).resolve())
    # Hash the FULL resolved path (before any lossy transforms) for uniqueness
    path_hash = hashlib.sha256(resolved.encode()).hexdigest()[:8]
    # Strip drive letter and leading separators for the human-readable part
    stripped = re.sub(r"^[A-Za-z]:", "", resolved)  # Windows drive letter
    stripped = stripped.lstrip("/\\")
    # Replace path separators with underscores (not hyphens — preserves
    # literal hyphens in directory names as distinct from separators)
    key = re.sub(r"[/\\]", "_", stripped)
    # Replace any remaining non-safe chars with hyphens
    key = re.sub(r"[^a-zA-Z0-9._-]", "-", key)
    # Collapse consecutive underscores/hyphens
    key = re.sub(r"[_-]{2,}", "_", key)
    key = key.strip("_-")
    # Cap human-readable portion to keep total slug under 255 bytes
    # (key + "_" + 8-char hash → max ~189). Caught by Week-2 deep-path
    # edge-case test — without this, deeply nested project paths
    # produced ENAMETOOLONG when codevira tried to mkdir the project dir.
    if len(key) > _MAX_KEY_LEN:
        key = key[:_MAX_KEY_LEN].rstrip("_-")
    return f"{key}_{path_hash}"


def _get_git_remote_url(project_root: Path) -> str | None:
    """Return the git remote 'origin' URL for project_root, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url if url else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


#: In-repo ``.codevira/`` files that hold MEMORY (not the opt-in marker).
#: These must never be git-tracked: if committed, they travel to any clone
#: or copy of the repo, silently sharing one project's decisions/sessions
#: with an unrelated project (the v3.7.1 cross-project-bleed root cause).
_MEMORY_FILES = ("decisions.jsonl", "sessions.jsonl", "outcomes.jsonl")


def git_tracked_memory_files(project_root: Path) -> list[str]:
    """Return the ``.codevira/`` memory files git currently tracks, if any.

    ``.gitignore`` does NOT untrack a file that was committed before the
    ignore rule existed (older codevira versions committed
    ``.codevira/decisions.jsonl``). Such a file keeps traveling via git —
    so a project copied/cloned from another inherits its memory. This detects
    that condition. Best-effort: returns ``[]`` when not a git repo / git is
    unavailable. Paths are returned as git reports them (repo-relative).
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "ls-files",
                *[f".codevira/{f}" for f in _MEMORY_FILES],
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def untrack_git_memory_files(project_root: Path) -> list[str]:
    """Stop git from tracking ``.codevira/`` memory files (keep them on disk).

    Runs ``git rm --cached`` on each tracked memory file so it is removed from
    the index (and future clones) while the local copy — the actual memory —
    is preserved. Returns the list of files untracked (``[]`` if none were
    tracked). Best-effort; never raises.
    """
    tracked = git_tracked_memory_files(project_root)
    if not tracked:
        return []
    try:
        subprocess.run(
            ["git", "-C", str(project_root), "rm", "--cached", "--quiet", *tracked],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    return tracked


def _find_project_by_git_remote(remote_url: str | None) -> Path | None:
    """Scan ~/.codevira/projects/ for a project whose metadata.json matches remote_url.

    v3.7.1 fix E (cross-project memory bleed): match ONLY on a non-empty
    remote. A None/empty ``remote_url`` must never match — otherwise it
    equals the ``git_remote: null`` stored for EVERY project without a git
    remote, so the first-scanned no-remote project's data dir is returned for
    all of them, silently sharing decisions across unrelated projects. We also
    skip stored empty/None ``git_remote`` values for the same reason (only a
    real remote-to-remote match is meaningful for rename-survival).
    """
    if not remote_url:
        return None
    projects_dir = get_global_home() / "projects"
    if not projects_dir.exists():
        return None

    # v3.7.1: collect ALL matches, then choose deterministically.
    #
    # Two problems with returning the first glob hit:
    #   1. A match on metadata.json ALONE let a GHOST dir (metadata written by a
    #      scan, no config.yaml, no real store) out-rank a genuinely initialized
    #      project — "a populated store loses to an empty one".
    #   2. glob() order is filesystem-dependent, so with several matching dirs
    #      (clones and forks share an `origin`) the winner varied between runs
    #      and between machines.
    # We now require a real store (config.yaml present) and sort by path so the
    # result is stable.
    matches: list[Path] = []
    for meta_file in sorted(projects_dir.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        stored = meta.get("git_remote")
        if not (stored and stored == remote_url):
            continue
        candidate = meta_file.parent
        if not (candidate / "config.yaml").is_file():
            logger.debug(
                "git-remote match at %s ignored: no config.yaml (ghost dir).",
                candidate,
            )
            continue
        matches.append(candidate)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "git remote %s matches %d centralized stores (%s); using %s. "
            "Clones/forks of one repo share a remote — pass --project-dir or "
            "set CODEVIRA_PROJECT_DIR to bind explicitly.",
            remote_url,
            len(matches),
            ", ".join(m.name for m in matches),
            matches[0].name,
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------


def _discover_project_root(start: Path) -> Path:
    """Walk upward from *start* to find the nearest project root.

    A project root is any ancestor directory that contains at least one
    of: .git, pyproject.toml, package.json, go.mod, Cargo.toml, .codevira/

    Stops at the first match so that nested repos return the inner root.
    Falls back to *start* if no marker is found.

    ``$HOME`` and system top-levels are NEVER returned even when they carry
    a marker: codevira's own global home lives at ``~/.codevira``, so ``$HOME``
    ALWAYS has a ``.codevira`` "marker". Without this skip, every marker-less
    folder under ``$HOME`` (e.g. a fresh project you're about to ``init``)
    resolved to ``$HOME`` and got refused. The skip lets the walk fall through
    to the real ``start`` directory instead.
    """
    start = start.resolve()
    for candidate in (start, *start.parents):
        if is_invalid_project_root(candidate) is not None:
            continue  # never resolve to $HOME / a system top-level
        for marker in _PROJECT_MARKERS:
            if (candidate / marker).exists():
                return candidate
    return start


def _resolve_project_root() -> Path:
    """Resolve the project root from override / env / cwd (no pinning).

    Resolution order:
      1. ``--project-dir`` CLI flag (via ``set_project_dir()``) — wins
         over everything else.
      2. ``$CODEVIRA_PROJECT_DIR`` env var — for IDE MCP configs
         (Claude Desktop, Codex, etc.) that spawn ``codevira serve``
         with no CLI args and no meaningful cwd to anchor on. Added in
         v3.0.0 round-3 after the AgentStore Claude-Desktop pin caught
         that ``env`` blocks in MCP config were being silently ignored.
      3. ``Path.cwd()`` — discover from current working directory by
         walking upward looking for project markers (.git,
         pyproject.toml, package.json, etc.). Falls back to cwd
         if no marker found.
    """
    if _project_dir_override is not None:
        return _discover_project_root(_project_dir_override)
    env_override = os.environ.get(_PROJECT_DIR_ENV)
    if env_override:
        return _discover_project_root(Path(env_override).resolve())
    return _discover_project_root(Path.cwd())


def get_project_root() -> Path:
    """Return the project root directory, pinned for the process life.

    Resolves via :func:`_resolve_project_root` (override > env > cwd) on the
    FIRST call and pins the result. Every later call returns that pinned root
    so record()/search() stay bound to a single ``.codevira`` store even if
    the process's cwd changes in between (D000118). A drift — a later
    resolution that differs from the pin — logs a WARN once per drifted target
    but the pinned root still wins. The pin is cleared by
    :func:`set_project_dir` / :func:`invalidate_data_dir_cache`.
    """
    resolved = _resolve_project_root()
    pinned = _pinned_root.get()
    # Only honor a pin this task set itself. A pin with a foreign owner was
    # INHERITED from an ancestor context (asyncio copies the context into each
    # task), so it says nothing about what THIS request should bind to — treat
    # it as absent and resolve fresh. This is what makes an explicit rebind
    # apply to every later request instead of just the one that performed it,
    # without letting one request's rebind disturb a concurrent one.
    if pinned is None or _pinned_owner.get() != _current_owner():
        _pinned_root.set(resolved)
        _pinned_owner.set(_current_owner())
        return resolved
    if resolved != pinned and resolved not in _drift_warned:
        _drift_warned.add(resolved)
        logger.warning(
            "get_project_root: project-root drift — resolved %s but keeping "
            "pinned root %s for this request (D000118). record()/search() stay "
            "bound to one store; call set_project_dir() to re-pin deliberately.",
            resolved,
            pinned,
        )
    return pinned


# ---------------------------------------------------------------------------
# Project-root validation (v1.8.1 — refuse $HOME and system dirs)
# ---------------------------------------------------------------------------

#: Absolute paths that must never be a project root. These are top-level user
#: or system directories where treating them as a project causes the watcher
#: to walk huge unrelated trees (``~/Library/Group Containers/...``,
#: ``/var/log``, etc.) and crash on EINTR / permission errors. macOS
#: aggressively symlinks system top-levels (``/etc -> /private/etc``,
#: ``/var -> /private/var``, ``/tmp -> /private/tmp``,
#: ``/home -> /System/Volumes/Data/home``) so the resolved forms are
#: listed too — ``Path("/etc").resolve()`` returns ``/private/etc`` on
#: macOS, and our equality check has to catch that.
_FORBIDDEN_PROJECT_ROOTS: frozenset[Path] = frozenset(
    {
        Path("/"),
        Path("/Users"),
        Path("/home"),
        Path("/System/Volumes/Data/home"),  # macOS resolved /home
        Path("/tmp"),
        Path("/private/tmp"),  # macOS resolved /tmp
        Path("/var"),
        Path("/private/var"),  # macOS resolved /var
        Path("/etc"),
        Path("/private/etc"),  # macOS resolved /etc
        Path("/opt"),
    }
)


def is_invalid_project_root(p: Path) -> str | None:
    """Return a human-readable rejection reason if ``p`` shouldn't be a
    project root, else ``None``.

    Refuses ``$HOME`` and known system top-levels (``/``, ``/Users``,
    ``/home``, ``/tmp``, ``/var``, ``/etc``, ``/opt``, plus the
    macOS-resolved ``/private/...`` and ``/System/Volumes/Data/home``
    forms). Treating any of these as a project causes the background
    watcher to walk huge unrelated trees — see v1.8.1 crash-log
    analysis: 41 ``InterruptedError`` crashes traced to a rogue
    ``$HOME``-rooted project bootstrapped on v1.8.0.

    Symlink-aware via ``Path.resolve()``: a symlinked ``$HOME`` or a path
    that resolves into ``/private/tmp`` is correctly rejected. We also
    check the *unresolved* input — on platforms where a forbidden top
    isn't a symlink, that's the only form we'd see. If ``.resolve()``
    itself raises (filesystem race, dangling symlink) we still check the
    raw input and return ``None`` only if neither matches; we never want
    to mask weirdness as "valid".
    """
    candidates: list[Path] = [p]
    try:
        resolved = p.resolve()
        candidates.append(resolved)
    except (OSError, RuntimeError):
        resolved = None
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        for cand in candidates:
            if cand == home:
                return (
                    f"$HOME ({cand}) is not a project. "
                    f"cd into a real project subdirectory first."
                )
    for cand in candidates:
        if cand in _FORBIDDEN_PROJECT_ROOTS:
            return f"{cand} is a system directory, not a project."
    return None


def is_ephemeral_project_path(p: Path) -> bool:
    """Return True if ``p`` lives under an OS temp directory.

    Used to keep transient pytest / scratch projects out of the
    cross-machine registry that ``codevira projects`` lists. Best-effort —
    never raises.

    Detection is by **temp-directory ancestry ONLY** — deliberately NOT
    by substring markers like ``pytest-``. A real project named
    ``pytest-django`` (or any path that merely *contains* such a token)
    must never be classified ephemeral, or codevira would silently hide a
    user's real project. The genuine pytest / scratch dirs always live
    under the system temp root
    (``/private/var/folders/.../pytest-of-<user>/...`` on macOS,
    ``/tmp/pytest-of-...`` on Linux), so ancestry is both sufficient and
    safe.

    Args:
        p: The candidate project root.

    Returns:
        True only when ``p`` resolves under a temp root; False otherwise.
    """
    import tempfile

    try:
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            resolved = p

        temp_roots: list[Path] = []
        try:
            temp_roots.append(Path(tempfile.gettempdir()).resolve())
        except (OSError, RuntimeError):
            pass
        temp_roots += [
            Path("/tmp"),
            Path("/private/tmp"),
            Path("/var/folders"),
            Path("/private/var/folders"),
        ]
        for root in temp_roots:
            if resolved == root or root in resolved.parents:
                return True
    except Exception:  # noqa: BLE001 — classification must never raise
        return False
    return False


# ---------------------------------------------------------------------------
# Data directory resolution (v1.6 centralized + legacy fallback)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Data directory cache — avoids re-running subprocess + glob on every tool call.
# Keyed by resolved project root Path.  Invalidated after init/migration.
# ---------------------------------------------------------------------------
_data_dir_cache: dict[Path, Path] = {}


def invalidate_data_dir_cache(project_root: Path | None = None) -> None:
    """Clear the data-dir cache so the next call re-resolves from disk.

    Call this after codevira init or after a migration completes, when the
    centralized directory has just been created and the cache entry would
    still point to the old (non-existent) default path.

    Args:
        project_root: If given, only invalidate that project's entry.
                      If None, clear the entire cache.
    """
    # Also drop the project-root pin (D000118) so the next get_project_root()
    # re-resolves — init/migration/set_project_dir may have moved the root out
    # from under an earlier pin. (Context-scoped since Phase 31.)
    reset_pinned_root()
    if project_root is None:
        _data_dir_cache.clear()
    else:
        _data_dir_cache.pop(Path(project_root).resolve(), None)


def get_data_dir() -> Path:
    """Return the Codevira data directory for the current project.

    Resolution chain (run once per project root, then cached):
      1. Centralized ~/.codevira/projects/<key>/ if config.yaml exists there
      2. Git remote lookup — finds centralized dir even after directory rename
      3. Legacy <project_root>/.codevira/ if config.yaml exists there
      4. Default to centralized path (new projects land here automatically)

    After the first call for a given project root the result is cached in
    _data_dir_cache.  Subsequent calls are O(1) dict lookups with no I/O.
    Call invalidate_data_dir_cache() after init or migration to force refresh.

    v3.0 hardening (2026-05-23 audit): refuses invalid roots (``$HOME``,
    system top-levels). Pre-fix, this function happily resolved to
    ``~/.codevira/projects/<sanitized-$HOME-key>/`` for an invalid root
    and callers then created ghost dirs there — the v1.8.0 production
    crash class. Now raises ``ValueError`` with the rejection reason
    instead. Callers that need graceful degradation should call
    ``is_invalid_project_root(p)`` first.
    """
    project_root = get_project_root()

    rejection = is_invalid_project_root(project_root)
    if rejection:
        raise ValueError(
            f"get_data_dir() refuses invalid project root: {rejection} "
            f"(root resolved to {project_root}). Set CODEVIRA_PROJECT_DIR "
            f"or cd into a real project subdirectory."
        )

    # Fast path — already resolved for this root.
    #
    # v3.7.1: a cached rule-4 result is PROVISIONAL. Rule 4 is "no store exists
    # yet, assume the default centralized path", and that answer goes stale the
    # moment a store actually appears — which routinely happens in ANOTHER
    # process: the MCP server is already running when the user runs
    # `codevira init` in a terminal. Nothing invalidates this process's cache
    # cross-process, so the live server kept writing to a directory the CLI
    # never reads. (opt_in.py deliberately re-reads negatives from disk for the
    # same reason.) That staleness is also what masked today's data-loss bug:
    # a session looked healthy purely because it had resolved before the store
    # moved.
    #
    # Re-checking costs two is_file() calls — the expensive parts of resolution
    # (a git subprocess and a metadata scan) only run if a real store appeared.
    cached = _data_dir_cache.get(project_root)
    if cached is not None:
        if not _is_provisional(project_root, cached):
            return cached
        # A provisional entry means "no store existed when we last looked".
        # Re-check CHEAPLY (two is_file calls) before paying for a full
        # re-resolution: the earlier version re-resolved unconditionally, which
        # meant a `git remote get-url` subprocess (~12ms, 3s timeout) plus a
        # scan of every ~/.codevira/projects/*/metadata.json on EVERY call for
        # any not-yet-initialized project — a permanent 100% cache miss on the
        # hot path, the opposite of what the change claimed.
        if not _store_appeared(project_root):
            return cached
        refreshed = _resolve_data_dir(project_root)
        _data_dir_cache[project_root] = refreshed
        return refreshed

    result = _resolve_data_dir(project_root)
    _data_dir_cache[project_root] = result
    return result


def _is_provisional(project_root: Path, cached: Path) -> bool:
    """True when ``cached`` was a rule-4 guess that may now be wrong.

    A resolution is settled once the directory it points at is a real store
    (has ``config.yaml``). Until then the answer is "nothing exists yet", which
    any other process can invalidate at any moment.
    """
    try:
        # Settled iff the cached location is a real store. Otherwise it was a
        # "nothing exists yet" guess and may need re-resolving, because a store
        # can appear (in-repo or centralized) in another process at any time.
        return not (cached / "config.yaml").is_file()
    except OSError:
        return False


def _store_appeared(project_root: Path) -> bool:
    """Cheap check: has a real store shown up since we guessed?

    Two ``is_file()`` calls, no subprocess and no directory scan — so a
    provisional cache entry costs a couple of stats per call instead of a full
    re-resolution. Only when this returns True do we pay for ``_resolve_data_dir``.
    """
    try:
        key = _sanitize_path_key(project_root)
        if (get_global_home() / "projects" / key / "config.yaml").is_file():
            return True
        return (project_root / ".codevira" / "config.yaml").is_file()
    except OSError:
        return False


def _resolve_data_dir(project_root: Path) -> Path:
    """Perform the actual (potentially slow) data-dir resolution.

    This is the only place that spawns subprocesses or reads metadata files.
    Always call get_data_dir() in production code — it caches this result.
    """
    key = _sanitize_path_key(project_root)
    centralized = get_global_home() / "projects" / key

    # 1. Centralized dir already initialized?
    if (centralized / "config.yaml").is_file():
        return centralized

    # 2. THIS project's own in-repo store, if it was explicitly initialized.
    #
    # v3.7.1: this used to run AFTER the git-remote lookup below, which meant a
    # project with its own `codevira init`-created .codevira/config.yaml could
    # still be routed to a DIFFERENT project's centralized dir whenever the two
    # shared a git remote — i.e. any clone, fork or second checkout of one repo.
    # The consequence is a session reading another project's graph/index, so
    # get_impact / query_graph / search_codebase answer about the wrong code.
    # An explicit local init is the strongest signal of intent we have, so it
    # must outrank a remote match.
    #
    # HONEST CAVEAT (do not repeat the earlier claim that rule 3 still covers
    # renames "for projects with no in-repo marker"): that is FALSE now.
    # Migration became non-destructive in the same release, so
    # <project>/.codevira/config.yaml effectively always survives and this
    # branch nearly always wins — making rule 3 unreachable in practice.
    #
    # The consequence is bounded and non-destructive: after a directory rename
    # the path key changes, so the project resolves to its in-repo store and the
    # code graph/index must be rebuilt (`codevira index`). Memory is unaffected
    # because it lives in the repo and moves with it. The previous centralized
    # dir is left orphaned on disk until `codevira clean --orphans`.
    #
    # Deliberately NOT "fixed" by preferring a remote-matched centralized store
    # here: get_data_dir() returns ONE directory, so doing that would put the
    # graph in one place and the memory in another, which is precisely the
    # split-brain that caused this release's data-loss bug. Rebuilding a
    # derived index is the cheaper, safer trade.
    legacy = project_root / ".codevira"
    if (legacy / "config.yaml").is_file():
        return legacy

    # 3. Git remote lookup — finds a centralized store for a project that has no
    #    in-repo marker at all (see the caveat above: rarely reached today).
    #    _get_git_remote_url() and _find_project_by_git_remote() are the
    #    potentially expensive operations — subprocess + metadata file scan.
    #    They only run once per project root thanks to the cache above.
    remote_url = _get_git_remote_url(project_root)
    if remote_url:
        found = _find_project_by_git_remote(remote_url)
        if found is not None:
            return found

    # 4. Default to centralized path — new project, will be created on init
    return centralized


def get_package_data_dir() -> Path:
    """Return the bundled data directory that ships with the pip package.

    Contains: rules/, agents/, config.example.yaml
    These are read-only assets installed alongside the package.
    """
    return Path(__file__).parent / "data"


def get_global_home() -> Path:
    """Return ~/.codevira/ global data directory. Creates it if needed."""
    home = Path.home() / ".codevira"
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_global_db_path() -> Path:
    """Return path to the global SQLite database for cross-project intelligence."""
    return get_global_home() / "global.db"
