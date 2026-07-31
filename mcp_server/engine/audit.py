"""
audit.py — per-verdict enforcement instrumentation (4.0 Step 3.5).

Records what the engine decided, and on what evidence, to
``<project>/.codevira-cache/enforcement.jsonl``.

WHY THIS EXISTS
---------------
Before 4.0 the only instrumented policy was ``session_log_enforcer``
(``enforcer_outcomes.jsonl``, v3.3.0). ``decision_lock``,
``anti_regression`` and ``blast_radius`` — the three that actually refuse
edits — recorded nothing. So the questions we most need to answer about
governance were unanswerable:

  * What is our false-block rate?
  * Which decisions do the blocks cite?
  * Does a user who downgrades a policy to ``warn`` show up anywhere?

That last one is not hypothetical: two of the largest codevira corpora had
``anti_regression`` and ``blast_radius`` downgraded to ``warn`` for nine
days and nothing recorded it (D00012H, D00012O).

DESIGN CONSTRAINTS (all load-bearing — do not relax without a decision)
----------------------------------------------------------------------
* **Cache-only.** Writes to ``.codevira-cache/`` (per-machine, gitignored,
  rebuildable) and NEVER to ``.codevira/``. Canonical memory is untouched
  by instrumentation — D0000KO, D00011Z.
* **P9: never change the verdict.** Every failure is swallowed. A full
  disk, a read-only FS or a permissions error must not turn an ``allow``
  into anything else, nor raise into the hook wiring.
* **Bounded.** Single-file rotation at ``_MAX_BYTES`` and a per-row cap, so
  a long-lived machine cannot grow this without limit — D00012K rule (c).
* **Identity stamped.** Every row carries ``host_hash``, ``version`` and
  ``ide``. The v3.3.0 outcomes file omitted these, which is why its
  "across multiple machines" question was unanswerable by construction.

Mirrors ``session_log_enforcer._record_outcome`` deliberately — one
instrumentation shape in the codebase, not two.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_CACHE_REL = ".codevira-cache"
_FILENAME = "enforcement.jsonl"

#: P5 bound: rotate to a single ``.1`` sibling at this size (~1,300 rows).
_MAX_BYTES = 256 * 1024

#: Per-row cap. Policy metadata is free-form; a pathological rationale must
#: not produce a multi-megabyte line.
_MAX_ROW_BYTES = 2048


def path(project_root: Path) -> Path:
    return project_root / _CACHE_REL / _FILENAME


def record(
    *,
    project_root: Path,
    event_type: str,
    session_id: str | None,
    action: str,
    policies: list[str],
    target: str | None,
    metadata: dict[str, Any] | None,
    eligible: int,
) -> None:
    """Append one verdict row. Never raises, never changes the verdict.

    Args:
        project_root: the resolved project — stamped so a mis-bound root is
            visible in the data rather than silently polluting another
            project's numbers.
        event_type: e.g. ``"PRE_TOOL_USE"``, ``"STOP"``.
        action: the COMBINED verdict — ``allow`` | ``warn`` | ``inject`` |
            ``block``. ``allow`` rows are the denominator; without them no
            rate can be computed.
        policies: names of the policies that contributed a non-allow verdict.
        target: the file the action was aimed at, when the event carries one.
        metadata: the combined verdict's metadata (decision ids, score
            breakdown, rationale). Truncated to fit ``_MAX_ROW_BYTES``.
        eligible: how many policies claimed this event type.
    """
    try:
        p = path(project_root)
        p.parent.mkdir(parents=True, exist_ok=True)

        try:
            if p.stat().st_size >= _MAX_BYTES:
                p.replace(p.with_suffix(p.suffix + ".1"))
        except FileNotFoundError:
            pass

        row: dict[str, Any] = {
            "ts": time.time(),
            "event": event_type,
            "action": action,
            "eligible": eligible,
            "policies": policies or [],
            "session_id": session_id,
            "project": str(project_root),
            "target": target,
            **_identity(),
        }

        # Metadata last so truncation drops evidence before identity.
        if metadata:
            row["meta"] = metadata
        line = json.dumps(row, default=str)
        if len(line) > _MAX_ROW_BYTES:
            row["meta"] = {"_truncated": True}
            line = json.dumps(row, default=str)

        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        # P9. Instrumentation is never allowed to affect enforcement, and a
        # broken audit path must not surface as a hook failure to the user.
        return


def _identity() -> dict[str, Any]:
    """host_hash / version / ide, or empty on any failure.

    Reuses ``storage.origin`` rather than deriving a second identity — the
    v3.3.0 outcomes file had no identity at all, which is the defect this
    closes.
    """
    try:
        from mcp_server.storage import origin

        o = origin.current_origin() or {}
        return {
            "host_hash": o.get("host_hash"),
            "ide": o.get("ide"),
            "version": _version(),
        }
    except Exception:  # noqa: BLE001
        return {}


def _version() -> str | None:
    try:
        from mcp_server import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:  # noqa: BLE001
        try:
            from importlib.metadata import version

            return version("codevira")
        except Exception:  # noqa: BLE001
            return None
