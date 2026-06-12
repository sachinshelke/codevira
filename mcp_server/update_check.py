"""
update_check.py — "new version available" notice for the codevira CLI.

Like homebrew / gh / npm: when the user runs a codevira command and a
newer release exists on PyPI, print a short notice to stderr. The
design constraint is ZERO added latency on the command path:

  - The command path only READS a small cache file
    (``~/.codevira/update_check.json``). No network, ever.
  - When the cache is stale (> 24 h), a detached fire-and-forget
    subprocess (``python -m mcp_server.update_check``) refreshes it.
    The notice therefore appears on the NEXT run — the same model
    homebrew uses.

Failure philosophy: this is an advisory feature. Network down, PyPI
unreachable, malformed responses — all degrade to "no notice", never
to an error or a stall. The cache file records the last error for
debuggability (``codevira doctor`` users can inspect it), but the
command path stays silent.

Opt-out: set ``CODEVIRA_NO_UPDATE_CHECK=1`` (any non-empty value).
Reverse / cleanup: delete ``~/.codevira/update_check.json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Commands where the notice must never fire:
#   serve  — MCP stdio transport; stderr lands in IDE logs and a
#            detached subprocess per server boot is unwanted.
#   engine — the lifecycle-hook hot path (fires on every tool call).
#   None   — bare `codevira` prints help; keep it clean.
_SKIP_COMMANDS = frozenset({"serve", "engine", None})

_CACHE_TTL_S = 24 * 3600.0
_FETCH_TIMEOUT_S = 5.0
_PYPI_URL = "https://pypi.org/pypi/codevira/json"

# Plain releases only (e.g. "3.3.0"). Pre-releases ("3.3.0rc1") never
# trigger a notice — users on stable shouldn't be nudged to an RC.
_RELEASE_RE = re.compile(r"\d+(\.\d+)*")


def _cache_path() -> Path:
    from mcp_server.paths import get_global_home

    return get_global_home() / "update_check.json"


def _parse_version(version: str) -> tuple[int, ...] | None:
    """Parse 'X.Y.Z' into a comparable tuple. None for pre-releases
    or anything else that isn't a plain dotted-integer release.
    """
    if not isinstance(version, str) or not _RELEASE_RE.fullmatch(version.strip()):
        return None
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except (ValueError, TypeError):
        return None


def _is_newer(latest: str, current: str) -> bool:
    """True iff ``latest`` is a plain release strictly newer than
    ``current``. Defensive: any unparseable input → False (no notice).
    """
    latest_t = _parse_version(latest)
    current_t = _parse_version(current)
    if latest_t is None or current_t is None:
        return False
    # Pad to equal length so 3.3 vs 3.3.0 compares as equal.
    width = max(len(latest_t), len(current_t))
    return latest_t + (0,) * (width - len(latest_t)) > current_t + (0,) * (
        width - len(current_t)
    )


def _read_cache() -> dict | None:
    """Load the cache file. None on missing/corrupt — never raises."""
    try:
        raw = _cache_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — advisory feature, never break the CLI
        return None


def _write_cache_atomic(payload: dict) -> None:
    """Atomic write (tmp + replace) so concurrent CLI runs never read
    a half-written cache. Failures are swallowed — see module docstring.
    """
    try:
        path = _cache_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _spawn_refresh() -> None:
    """Fire-and-forget detached subprocess that refreshes the cache.

    Detached (new session, no inherited stdio) so it survives the
    parent CLI exiting milliseconds later and can't pollute output.
    """
    try:
        import subprocess

        subprocess.Popen(  # noqa: S603 — fixed argv, our own interpreter
            [sys.executable, "-m", "mcp_server.update_check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass


def maybe_notify(command: str | None) -> None:
    """Called once per CLI invocation from ``cli.main()``.

    Reads the cache (no network); prints the stderr notice if a newer
    release is recorded; kicks off a detached refresh if the cache is
    stale. Must never raise and never add measurable latency.
    """
    try:
        if command in _SKIP_COMMANDS:
            return
        if os.environ.get("CODEVIRA_NO_UPDATE_CHECK"):
            return

        from mcp_server import __version__

        cache = _read_cache()
        now = time.time()

        if cache is not None:
            latest = cache.get("latest")
            if isinstance(latest, str) and _is_newer(latest, __version__):
                print(
                    f"\n  ✦ Update available: codevira {__version__} → {latest}\n"
                    f"    Run: pipx upgrade codevira   "
                    f"(or: pip install -U codevira)\n",
                    file=sys.stderr,
                )

        checked_at = (cache or {}).get("checked_at")
        stale = (
            not isinstance(checked_at, (int, float))
            or now - float(checked_at) > _CACHE_TTL_S
        )
        if stale:
            # Stamp checked_at BEFORE spawning so a burst of CLI calls
            # (e.g. a script looping `codevira status`) spawns exactly
            # one refresh per TTL window, not one per call (P5).
            _write_cache_atomic(
                {
                    **(cache or {}),
                    "schema": 1,
                    "checked_at": now,
                }
            )
            _spawn_refresh()
    except Exception:  # noqa: BLE001 — advisory feature, never break the CLI
        pass


def refresh_cache() -> int:
    """Fetch the latest release from PyPI and write the cache.

    Runs in the detached subprocess (``python -m mcp_server.update_check``).
    Returns 0 on success, 1 on any failure (recorded in the cache's
    ``error`` field for debuggability; the parent never sees it).
    """
    now = time.time()
    try:
        from urllib.request import Request, urlopen

        from mcp_server import __version__

        req = Request(
            _PYPI_URL,
            headers={"User-Agent": f"codevira/{__version__} update-check"},
        )
        # macOS python.org builds ship without a system CA bundle wired
        # into OpenSSL, so bare urlopen fails CERTIFICATE_VERIFY_FAILED.
        # certifi is always present transitively (mcp → httpx → certifi);
        # fall back to the default context if it ever isn't.
        context = None
        try:
            import ssl

            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            context = None
        with urlopen(  # noqa: S310 — fixed https URL
            req, timeout=_FETCH_TIMEOUT_S, context=context
        ) as resp:
            data = json.loads(resp.read(1_000_000).decode("utf-8"))
        latest = data["info"]["version"]
        if _parse_version(latest) is None:
            raise ValueError(f"unrecognized version from PyPI: {latest!r}")
        _write_cache_atomic(
            {"schema": 1, "latest": latest, "checked_at": now, "error": None}
        )
        return 0
    except Exception as e:  # noqa: BLE001 — record + exit; parent is long gone
        existing = _read_cache() or {}
        _write_cache_atomic(
            {
                **existing,
                "schema": 1,
                "checked_at": now,
                "error": f"{type(e).__name__}: {e}"[:300],
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(refresh_cache())
