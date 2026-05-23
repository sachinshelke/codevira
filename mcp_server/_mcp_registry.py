"""Track currently-running codevira MCP server processes.

Pre-3.0, codevira had no way to detect when a Claude Code / Cursor /
Antigravity MCP stdio child was running a STALE codevira version because
the user had ``pipx install --force codevira`` after the IDE spawned
the child. ``pipx`` replaces the wheel on disk, but the running stdio
children loaded the OLD code into ``sys.modules`` at startup and keep
serving it until the parent IDE is restarted.

This module gives every MCP server process a small write-on-startup
registration at ``~/.codevira/run/<pid>.json`` containing:

  - ``pid``: OS process ID
  - ``version``: ``mcp_server.__version__`` at the moment startup ran
  - ``project_root``: as resolved by ``get_project_root()``
  - ``transport``: ``"stdio"`` | ``"http"``
  - ``started_at``: ISO-8601 UTC

``codevira doctor`` reads this registry, drops stale entries (PIDs
that don't exist anymore), and warns whenever a running MCP's
``version`` differs from the currently-installed wheel — signaling
"restart your IDE to load the new code."

Everything here is best-effort; failures must never block MCP
``initialize``. Caller wraps in try/except.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _registry_dir() -> Path:
    return Path.home() / ".codevira" / "run"


def _entry_path(pid: int) -> Path:
    return _registry_dir() / f"{pid}.json"


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. POSIX-only (good enough)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid exists but belongs to another user; treat as alive so we
        # don't sweep something we can't actually verify.
        return True
    except OSError:
        return False


def register(*, transport: str, project_root: Path | str | None = None) -> Path | None:
    """Drop our entry; sweep dead entries. Returns the entry path or None."""
    try:
        from mcp_server import __version__
    except Exception:
        __version__ = "unknown"

    try:
        _registry_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    # Sweep stale entries first (cheap, ~10ms for typical user).
    sweep_stale()

    entry = {
        "pid": os.getpid(),
        "version": __version__,
        "project_root": str(project_root) if project_root else None,
        "transport": transport,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _entry_path(os.getpid())
    try:
        from mcp_server.storage.atomic import atomic_write_text

        atomic_write_text(path, json.dumps(entry, indent=2))
    except Exception:
        # Last-ditch direct write; never block startup on registry I/O.
        try:
            path.write_text(json.dumps(entry, indent=2))
        except OSError:
            return None
    return path


def unregister(pid: int | None = None) -> None:
    """Remove our entry. Best-effort, called from atexit."""
    pid = pid if pid is not None else os.getpid()
    try:
        _entry_path(pid).unlink()
    except (FileNotFoundError, OSError):
        pass


def sweep_stale() -> int:
    """Remove entries whose pid no longer exists. Returns count removed."""
    removed = 0
    d = _registry_dir()
    if not d.is_dir():
        return 0
    for path in d.glob("*.json"):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        if not _pid_alive(pid):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def list_running() -> list[dict]:
    """Return registry entries for currently-alive MCP processes.

    Auto-sweeps stale entries as a side effect — callers don't have to.
    """
    sweep_stale()
    out: list[dict] = []
    d = _registry_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(data)
    return out
