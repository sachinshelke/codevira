"""Doctor check for ghost project directories — Bug 21c (rc.4 dogfood, 2026-05-13).

Kept in its own module so adding the check doesn't inflate the public-signature
surface of :mod:`mcp_server.doctor` (which has high downstream blast radius).
``doctor.py`` imports :func:`check_ghost_projects` from here and registers it
in its ``_CHECKS`` tuple.

Background — Sachin found 3 of 5 ``~/.codevira/projects/`` dirs were "ghosts"
created as side effects of MCP tool calls that ran ``get_data_dir()`` and
wrote to it before the full init chain (``config.yaml`` + ``metadata.json``
+ ``global.db.projects`` registration) had completed.

With Bug 21a's synchronous self-heal in place, new ghosts shouldn't form. But
legacy ghosts from pre-rc.4 installs need to be surfaced + cleaned. This
check is the user-facing signal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_server.doctor import CheckResult


def check_ghost_projects() -> "CheckResult":
    """Flag ``~/.codevira/projects/`` dirs missing ``config.yaml`` or ``metadata.json``.

    A ghost dir is one that exists on disk but doesn't have the full init
    bookkeeping. With the Bug 21a synchronous self-heal in place, new ghosts
    can't form on rc.4+. This check surfaces legacy ghosts from earlier
    installs so the user can clean them up before they accumulate.

    Returns
    -------
    CheckResult
        * ``PASS`` if the projects dir is absent (first run) or every dir has
          both ``config.yaml`` AND ``metadata.json``.
        * ``WARN`` listing up to 3 ghost slugs + the fix command. NEVER ``FAIL``
          — ghost dirs are a cleanliness issue, not a correctness one (the
          project still works on the canonical-path side).
    """
    # Local import to avoid the circular dependency: doctor.py imports this
    # module to register the check, so this module can't import from doctor
    # at module load time.
    from mcp_server.doctor import CheckResult, _PASS, _WARN
    try:
        from mcp_server.paths import get_global_home
        projects_dir = get_global_home() / "projects"
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "ghost_projects", _WARN,
            f"could not resolve projects dir: {e}",
        )
    if not projects_dir.is_dir():
        return CheckResult(
            "ghost_projects", _PASS,
            "no projects directory yet (no tracked projects)",
        )

    ghosts: list[str] = []
    total = 0
    for child in projects_dir.iterdir():
        if not child.is_dir():
            continue
        total += 1
        if (
            not (child / "config.yaml").is_file()
            or not (child / "metadata.json").is_file()
        ):
            ghosts.append(child.name)

    if not ghosts:
        return CheckResult(
            "ghost_projects", _PASS,
            f"{total} project(s) tracked — none are ghost dirs",
        )

    # Show up to 3 names so the user has something concrete to grep for.
    sample = ", ".join(ghosts[:3])
    if len(ghosts) > 3:
        sample += f" (+{len(ghosts) - 3} more)"
    return CheckResult(
        "ghost_projects", _WARN,
        f"{len(ghosts)} of {total} project dir(s) are ghosts "
        f"(missing config/metadata): {sample}",
        fix_command="codevira projects --ghosts-only   "
                    "# list them, then `codevira clean` to remove",
    )
