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

from mcp_server.paths import get_global_home, get_project_root

CODEVIRA_DIR_NAME = ".codevira"
DEFAULT_TRACKING_MODE = "hint"
_VALID_MODES = ("strict", "hint", "auto_adopt")

# Cache: resolved project root -> opted-in bool. Mirrors the get_data_dir
# cache lifecycle — an entry is only invalidated when the marker could have
# changed (init / migration), via invalidate_opt_in_cache().
_opt_in_cache: dict[Path, bool] = {}


def invalidate_opt_in_cache(project_root: Path | None = None) -> None:
    """Clear the opt-in cache so the next check re-reads from disk.

    Call after ``codevira init`` writes the marker (or a migration creates it).
    Pass a ``project_root`` to drop only that entry, or ``None`` to clear all.
    """
    if project_root is None:
        _opt_in_cache.clear()
    else:
        _opt_in_cache.pop(Path(project_root).resolve(), None)


def _config_marker(project_root: Path) -> Path:
    """The in-repo opt-in marker: ``<project>/.codevira/config.yaml``."""
    return project_root / CODEVIRA_DIR_NAME / "config.yaml"


def is_project_opted_in(project_root: Path | None = None) -> bool:
    """True iff the project was explicitly initialized (``codevira init``).

    The marker is the in-repo ``<project>/.codevira/config.yaml`` — written
    ONLY by explicit init, never by auto_init / repair / graph-read. Result is
    cached per resolved root; call :func:`invalidate_opt_in_cache` after init.
    """
    root = (project_root if project_root is not None else get_project_root()).resolve()
    cached = _opt_in_cache.get(root)
    if cached is not None:
        return cached
    result = _config_marker(root).is_file()
    _opt_in_cache[root] = result
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
