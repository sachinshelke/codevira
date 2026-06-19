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

Classification is delegated to :mod:`mcp_server._project_inventory` — the
single source of truth shared with ``codevira projects`` / ``clean`` — so
doctor and those commands can never disagree on what counts as a ghost
(vs. a harmless *stale* empty dir).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_server.doctor import CheckResult


def check_ghost_projects() -> "CheckResult":
    """Flag ``~/.codevira/projects/`` dirs that are genuine ghosts.

    A **ghost** is a dir on disk that carries real state (graph / codeindex /
    config / metadata / roadmap) but is missing the full init bookkeeping. An
    empty leftover dir with *no* recognisable state is **stale**, not a ghost —
    harmless cruft, not an incomplete project.

    This check delegates classification to
    :func:`mcp_server._project_inventory.enumerate_projects`, the single source
    of truth that ``codevira projects`` / ``status --global`` / ``clean`` all
    read from. Pre-fix this module rolled its own cruder definition (any dir
    missing config *or* metadata = ghost), which counted stale dirs as ghosts
    and made doctor disagree with ``codevira projects`` (doctor said "29
    ghosts" while ``projects`` said "0 ghost · 29 stale"). Now both agree by
    construction.

    Returns
    -------
    CheckResult
        * ``PASS`` if there are no ghost dirs. Stale dirs are mentioned
          informationally (they don't warrant a warning).
        * ``WARN`` listing up to 3 ghost slugs + the fix command. NEVER
          ``FAIL`` — ghost dirs are a cleanliness issue, not a correctness one
          (the project still works on the canonical-path side).
    """
    # Local import to avoid the circular dependency: doctor.py imports this
    # module to register the check, so this module can't import from doctor
    # at module load time.
    from mcp_server.doctor import CheckResult, _PASS, _WARN

    try:
        from mcp_server._project_inventory import enumerate_projects, summarize

        entries = enumerate_projects()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "ghost_projects",
            _WARN,
            f"could not enumerate projects: {e}",
        )

    counts = summarize(entries)
    ghosts = [e for e in entries if e.status == "ghost"]
    stale = counts.get("stale", 0)

    if not ghosts:
        msg = f"{counts.get('tracked', 0)} tracked project(s) — no ghost dirs"
        if stale:
            # Mirror `codevira projects`: stale dirs are harmless leftovers,
            # surfaced so the count isn't a surprise, but NOT a warning.
            msg += (
                f" ({stale} stale dir(s) — empty leftovers; "
                f"`codevira clean` tidies them)"
            )
        return CheckResult("ghost_projects", _PASS, msg)

    # Show up to 3 names so the user has something concrete to grep for.
    names = [g.slug or g.canonical_path or "<unknown>" for g in ghosts]
    sample = ", ".join(names[:3])
    if len(names) > 3:
        sample += f" (+{len(names) - 3} more)"
    return CheckResult(
        "ghost_projects",
        _WARN,
        f"{len(ghosts)} project dir(s) are ghosts (incomplete bookkeeping): {sample}",
        fix_command="codevira projects --ghosts-only   "
        "# list them, then `codevira clean` to remove",
    )
