"""
opt_in.py — Explicit project activation gate (v3.7.0).

Codevira runs as a SINGLE global MCP registration, so its tools are available
in every IDE window. Without a gate, merely *reading* a project (e.g.
``get_impact`` before an edit) auto-adopts it — creating a centralized data
dir and tracking a project the user never chose. This module is the predicate
that makes ``codevira init`` the explicit opt-in: codevira tracks ONLY the
projects the user deliberately initialized and stays inert everywhere else.

The opt-in MARKER is the in-repo ``<project>/.codevira/config.yaml``. It is
written ONLY by explicit ``codevira init`` (``cli_init.cmd_init``); auto_init,
repair, and the graph-read path never create it. Its presence therefore
cleanly separates deliberately-tracked projects from projects codevira merely
touched (empirically: the real projects all have it, the ~60 ghost dirs do not).

Tracking mode governs what happens for an un-adopted project (D1, locked
2026-07-15):

  * ``hint``       (default) — reads return an inert payload + a hint to run
                    ``codevira init``; writes refuse + hint.
  * ``strict``     — reads empty, writes refuse (no guidance).
  * ``auto_adopt`` — pre-v3.7.0 behavior: adopt any project touched.

Resolution order: env ``CODEVIRA_AUTO_ADOPT`` (``1``→auto_adopt, ``0``→strict,
or an explicit mode name) overrides the ``tracking.mode`` key in the global
``~/.codevira/config.yaml``, which overrides the shipped default (``hint``).

This module is pure predicate + mode resolution + a small cache. The gating
(refusing tool calls, skipping dir creation) is layered on at the four creation
vectors in later phases — this file never itself refuses or creates anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp_server.paths import (
    broken_worktree_message,
    get_global_home,
    get_project_root,
    project_root_exists,
)

CODEVIRA_DIR_NAME = ".codevira"
DEFAULT_TRACKING_MODE = "hint"
_VALID_MODES = ("strict", "hint", "auto_adopt")

# Cache: resolved project root -> True. Only POSITIVE (opted-in) results are
# cached — once a project is opted in it stays opted in (the marker persists),
# so caching True is safe and keeps the hot path fast. A negative result is
# NEVER cached: a fresh ``codevira init`` (even from another process — the CLI
# vs a long-lived MCP server) must be seen on the very next call. Checking a
# not-yet-opted project costs one or two ``is_file()`` stats — negligible.
_opt_in_cache: dict[Path, bool] = {}


def invalidate_opt_in_cache(project_root: Path | None = None) -> None:
    """Drop cached positive results so opt-in is re-read from disk.

    Only needed to UN-track a project (delete the marker) within a live
    process — a fresh init is picked up automatically (False is never cached).
    Pass a ``project_root`` to drop only that entry, or ``None`` to clear all.
    """
    if project_root is None:
        _opt_in_cache.clear()
    else:
        _opt_in_cache.pop(Path(project_root).resolve(), None)


def _config_marker(project_root: Path) -> Path:
    """The in-repo opt-in marker: ``<project>/.codevira/config.yaml``."""
    return project_root / CODEVIRA_DIR_NAME / "config.yaml"


def _centralized_marker(project_root: Path) -> Path | None:
    """The centralized ``config.yaml`` for this root, or None if unresolvable.

    The v1.6 auto-migration moves an in-repo ``.codevira/`` to
    ``~/.codevira/projects/<key>/`` (renaming the in-repo dir away), so after a
    migration — or a git-clone onto a fresh machine that then migrates — the
    marker lives here, not in-repo. Checking both keeps opt-in stable across
    that move.
    """
    try:
        from mcp_server.paths import _sanitize_path_key, get_global_home

        return (
            get_global_home()
            / "projects"
            / _sanitize_path_key(project_root)
            / "config.yaml"
        )
    except Exception:
        return None


def _has_config_marker(root: Path) -> bool:
    """True iff a ``config.yaml`` exists in the in-repo OR centralized store."""
    if _config_marker(root).is_file():
        return True
    centralized = _centralized_marker(root)
    return centralized is not None and centralized.is_file()


def is_project_opted_in(project_root: Path | None = None) -> bool:
    """True iff the project was explicitly initialized (``codevira init``).

    A ``config.yaml`` is written ONLY by explicit init (never by auto_init /
    repair / graph-read). We accept it in the in-repo store
    (``<project>/.codevira/config.yaml``, git-committed, the primary marker) OR
    the centralized store (``~/.codevira/projects/<key>/config.yaml``, where the
    v1.6 migration moves it) — see :func:`_centralized_marker`.

    Positive results are cached; a negative is always re-checked so a fresh
    ``codevira init`` is seen on the next call.
    """
    root = (project_root if project_root is not None else get_project_root()).resolve()
    if _opt_in_cache.get(root):
        return True
    result = _has_config_marker(root)
    if result:
        _opt_in_cache[root] = True
    return result


def _global_config_mode() -> str | None:
    """Read ``tracking.mode`` from the global ``~/.codevira/config.yaml``.

    Returns a valid mode string, or ``None`` if the file is absent, malformed,
    or carries no recognized mode.
    """
    cfg_path = get_global_home() / "config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    tracking = data.get("tracking")
    if not isinstance(tracking, dict):
        return None
    mode = str(tracking.get("mode") or "").strip().lower()
    return mode if mode in _VALID_MODES else None


def tracking_mode() -> str:
    """Return the active tracking mode for un-adopted projects.

    Resolution (D1): env ``CODEVIRA_AUTO_ADOPT`` > global config
    ``tracking.mode`` > shipped default (``hint``).

      * ``CODEVIRA_AUTO_ADOPT`` in {1, true, yes, on} → ``auto_adopt``
      * ``CODEVIRA_AUTO_ADOPT`` in {0, false, no, off} → ``strict``
      * an explicit mode name (``strict``/``hint``/``auto_adopt``) is honored too
    """
    raw = os.environ.get("CODEVIRA_AUTO_ADOPT")
    if raw is not None:
        val = raw.strip().lower()
        if val in ("1", "true", "yes", "on"):
            return "auto_adopt"
        if val in ("0", "false", "no", "off"):
            return "strict"
        if val in _VALID_MODES:
            return val
    return _global_config_mode() or DEFAULT_TRACKING_MODE


def activation_allowed(project_root: Path | None = None) -> bool:
    """True iff codevira may create or track state for this project.

    ``auto_adopt`` mode always allows (restores pre-v3.7.0 behavior); otherwise
    only explicitly opted-in projects are allowed. This is the single predicate
    every creation vector consults before adopting a project.
    """
    if tracking_mode() == "auto_adopt":
        return True
    return is_project_opted_in(project_root)


# ── Tool classification for the dispatch gate (Phase 5) ────────────────────
#
# Every MCP tool the server dispatches is in exactly one of these sets. For a
# project that isn't opted in (hint mode), a READ returns an inert payload + a
# hint, a WRITE refuses + a hint (D2). A test
# (tests/test_opt_in.py::TestOptInDispatchClassification) asserts every
# dispatched tool name is classified, so a new tool can't silently default to
# the wrong side. classify_tool() falls back to "read" (the SAFE default —
# inert, never mutates) for anything unlisted, but the test forbids unlisted.

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "record_decision",
        "supersede_decision",
        "set_decision_flag",
        "mark_decision_outdated",
        "reaffirm_decision",
        "write_session_log",
        "add_phase",
        "update_phase_status",
        "defer_phase",
        "complete_phase",
        "bulk_import_phases",
        "update_next_action",
        "refresh_graph",
        "working_add",
        "working_promote",
        "record_skill",
        "apply_skill_outcome",
        "supersede_skill",
        "promote_skill_to_playbook",
    }
)

READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_node",
        "get_impact",
        "get_roadmap",
        "get_phase",
        "search_codebase",
        "search_decisions",
        "list_decisions",
        "list_tags",
        "expand",
        "get_history",
        "get_playbook",
        "get_session_context",
        "query_graph",
        "working_get",
        "get_working_context",
        "get_skill",
        "list_skills",
        "origin_of",
        "check_conflict",
    }
)


def classify_tool(name: str) -> str:
    """Return ``"write"`` if the tool mutates state, else ``"read"``.

    ``read`` is the safe fallback for any unlisted name (inert, never mutates).
    """
    return "write" if name in WRITE_TOOLS else "read"


def opt_in_hint_payload(tool_name: str) -> dict:
    """Inert response for a tool call on a project that isn't opted in.

    READ tools get an empty-but-valid payload + a hint; WRITE tools (D2) get a
    friendly refusal. Both carry ``not_opted_in`` and ``fix_command`` so the
    calling AI knows exactly how to enable codevira here.
    """
    if classify_tool(tool_name) == "write":
        return {
            "not_opted_in": True,
            "error": "refused",
            "message": (
                "codevira isn't tracking this project, so it won't record or "
                "modify memory here. Run `codevira init` in the project root "
                "to start tracking it."
            ),
            "fix_command": "codevira init",
        }
    return {
        "not_opted_in": True,
        "message": (
            "codevira isn't tracking this project yet, so there's no memory to "
            "read. Run `codevira init` in the project root to enable it."
        ),
        "fix_command": "codevira init",
    }


# ── Vanished-project gate ──────────────────────────────────────────────────
#
# A long-lived server pinned with --project-dir keeps running after the user
# moves/renames the folder. Without this gate its next write recreated the old
# path, started a NEW store at D000001 and reported success: memory silently
# split in two. This runs BEFORE the opt-in gate on purpose — opt-in positives
# are cached for the process and a centralized marker outlives the folder, so
# opt-in alone can never notice the folder is gone.

# Roots for which THIS process has seen an in-repo ``.codevira``. If it later
# disappears while the folder itself exists (another program recreated the
# folder with, say, only a ``.claude/`` in it), writes must refuse even though
# a centralized marker still says "opted in". A project that only ever had the
# centralized marker is never added, so it keeps working.
_seen_in_repo_store: set[Path] = set()


def _folder_gone_payload(
    root: Path | str, message: str, fix_command: str
) -> dict[str, object]:
    """The loud refusal returned for BOTH read and write tools."""
    return {
        "error": "project_folder_missing",
        "recorded": False,
        "message": message,
        "project_root": str(root),
        "fix_command": fix_command,
    }


def project_folder_gate(tool_name: str) -> dict[str, object] | None:
    """Refusal payload when the bound project's folder (or memory) is gone.

    Returns ``None`` when the call may proceed. Must run AFTER project
    re-binding, so a client that reports the new folder keeps working, and
    needs no restart once the folder comes back (nothing is cached except the
    "seen an in-repo store" fact). Cost: a few ``stat`` calls.
    """
    try:
        root = get_project_root()
    except OSError:
        # cwd itself was deleted (Linux): there is no root to name.
        return _folder_gone_payload(
            "(unknown)",
            "The folder this codevira server was started in no longer exists "
            "(moved, renamed or deleted), so nothing was saved. Reopen the "
            "project from its new location.",
            "codevira doctor",
        )
    if not project_root_exists(root):
        return _folder_gone_payload(
            root,
            f"The project folder {root} no longer exists (it was moved, "
            f"renamed or deleted), so nothing was saved and no memory was "
            f"read. This codevira server is still pointing at the old path. "
            f"Fix: reopen the project from its new location (restart your AI "
            f"tool in that folder). The memory moves with the folder.",
            "codevira doctor",
        )
    from mcp_server.storage.paths import missing_main_worktree

    main = missing_main_worktree(root)
    if main is not None:
        return _folder_gone_payload(
            root,
            broken_worktree_message(root, main),
            f"git worktree repair {root}",
        )
    try:
        in_repo = _config_marker(root).parent.is_dir()
    except OSError:
        return None
    if in_repo:
        _seen_in_repo_store.add(root)
    elif root in _seen_in_repo_store and classify_tool(tool_name) == "write":
        return _folder_gone_payload(
            root,
            f"The project's memory is no longer in {root}: its .codevira "
            f"folder is gone (the folder was likely deleted and recreated by "
            f"another program), so nothing was saved. Fix: restore the "
            f".codevira folder (git checkout / backup), or run `codevira init` "
            f"here to start a fresh store.",
            "codevira init",
        )
    return None
