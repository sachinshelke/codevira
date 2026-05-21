"""
_safe_crash.py — single-line `safe_log_crash(error, context)` helper.

Pillar 3.4 of v2.0 master plan. Until v2.0-rc.1, the same defensive
pattern was duplicated 14 times across cli.py / server.py /
index_codebase.py:

    try:
        from mcp_server.crash_logger import log_crash
        log_crash(e, context="...")
    except Exception: pass    # ← guards against crash logger itself failing

This file replaces the boilerplate with one helper. The 14 sites become
one-liners:

    safe_log_crash(e, "context string")

The defensive try/except is preserved (we can't trust the crash logger
import or call to never fail in pathological deployments — bad PYTHONPATH,
half-built install, tests with weird mocks). It's just centralized.

Audit note: ``crash_logger.log_crash`` is itself defensive — its body
is wrapped in try/except so it never raises. So technically the outer
guard in this helper is paranoid. But import-time failures (the line
``from mcp_server.crash_logger import log_crash``) are still possible
in broken installs, so the defensive wrap stays.
"""
from __future__ import annotations



def safe_log_crash(
    error: BaseException,
    context: str = "",
    *,
    tool_name: str = "",
    project_path: str = "",
) -> None:
    """Log a crash to ~/.codevira/logs/crashes.log without ever raising.

    Pillar 3.4 dedup helper. Replaces the ``try: log_crash(...) except: pass``
    pattern that appeared 14 times in v2.0-alpha.

    All arguments forward to ``crash_logger.log_crash``. If the crash
    logger itself can't be imported or called (broken install, missing
    deps), this function silently no-ops — the outer caller has already
    handled the user-facing error path; the crash log is just for
    diagnostics on `codevira report`.
    """
    try:
        from mcp_server.crash_logger import log_crash
        log_crash(
            error, context=context,
            tool_name=tool_name, project_path=project_path,
        )
    except Exception:  # noqa: BLE001 — last-resort defense
        pass
