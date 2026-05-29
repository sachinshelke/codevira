"""
config.py — v3.1.0: tiny accessor for .codevira/config.yaml flags.

Most v3.1.0 subsystems are gated behind a config flag so users can
opt into or out of behavior without touching code. The config file
itself is YAML; this module exposes a single ``get_flag(path,
default)`` so feature-flag checks read the same source of truth
everywhere.

We deliberately don't add a schema validator: the config is small,
fail-open is the right default (missing key → caller's default),
and codevira already inherits a fail-open culture for cache layers.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server.storage import paths

logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    """Read and parse the project config; return empty dict on missing
    or malformed input."""
    path = paths.config_path()
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "config.load: failed to parse %s; falling back to defaults: %s",
            path,
            exc,
        )
        return {}
    return data if isinstance(data, dict) else {}


def get_flag(path: str, default: Any = None) -> Any:
    """Look up a dotted key in the config (e.g.
    ``"memory.consensus.handshake_enabled"``). Returns ``default`` on
    any miss.
    """
    if not isinstance(path, str) or not path:
        return default
    data = _load_config()
    cursor: Any = data
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def is_enabled(path: str, *, default: bool = False) -> bool:
    """Type-safe wrapper around ``get_flag`` for boolean toggles."""
    val = get_flag(path, default=default)
    return bool(val)
