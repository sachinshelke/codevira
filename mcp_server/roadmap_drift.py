"""Roadmap drift detection — Bug 8 (v2.0-rc.3).

Codevira's value proposition is "persistent memory of your project across
AI sessions and tools". The mechanism: AIs call ``update_phase_status`` /
``complete_phase`` / ``record_decision`` etc. as they work, and codevira
keeps a current view of the project's state.

But in real-world dogfood (Sachin's AgentStore project, 2026-05-06) we
hit the failure mode: the AI **doesn't** call those write tools. It chats,
codes, runs tests, ships commits — but never invokes the codevira write
surface. Result: codevira's roadmap freezes at the last seeded state
(May 2) while real work happens. Days later, ``get_session_context``
returns an authoritative-looking but completely stale picture.

This module detects that drift and injects a warning into the session
context so the AI is *prompted* to reconcile codevira's claimed state
with reality (typically: read recent commits, refresh the roadmap, log
decisions). The warning is informational — never blocking — so users
who genuinely don't want drift checks can ignore it.

How drift is detected:

1. **Reference timestamp** — when codevira's current phase last changed.
   Resolved in priority order:
     a. ``current_phase.last_updated`` (ISO string, future schema)
     b. ``current_phase.started`` (ISO string, current schema)
     c. file mtime of roadmap.yaml (last touch)
   Whichever is freshest.
2. **Commits since** — ``git log --since=<reference>`` from the project
   root, counted (capped at 30 to keep it cheap).
3. **Days since** — wallclock days between reference and now.

Drift fires if EITHER:
  - ``days_since > DRIFT_DAYS_THRESHOLD`` (default 3 days)
  - ``commits_since > DRIFT_COMMITS_THRESHOLD`` (default 5 commits)

Output shape (when drift detected):

    {
        "drifted": True,
        "days_since_update": 4,
        "commits_since": 7,
        "last_phase_update": "2026-05-02T23:21:00",
        "recent_commit_subjects": ["fix(operator): hardening", "feat(...): ..."],
        "message": "⚠ Roadmap is 4 days stale — 7 commits landed since "
                   "the last codevira update. Consider running "
                   "update_phase_status or complete_phase before relying "
                   "on get_roadmap state.",
    }

If drift is NOT detected, returns ``None`` (caller can omit the field).

The function is defensive: any error during git invocation, timestamp
parsing, or roadmap read returns ``None``. Drift detection MUST never
crash the session context call — that's the whole product working.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tuneable thresholds. Picked from Sachin's UDAP/AgentStore dogfood:
#   - 3 days = "yesterday's work is fine, last week's isn't"
#   - 5 commits = "small refactor day's worth of activity is fine,
#                  a feature branch's worth isn't"
DRIFT_DAYS_THRESHOLD = 3
DRIFT_COMMITS_THRESHOLD = 5

# Cap how many commits we read back. Keeps git log fast even on big
# repos with months of activity. We only need the count + a few subjects.
_MAX_COMMITS_TO_INSPECT = 30


def check_drift(
    project_root: Path,
    *,
    current_phase: dict[str, Any] | None = None,
    roadmap_path: Path | None = None,
    days_threshold: int = DRIFT_DAYS_THRESHOLD,
    commits_threshold: int = DRIFT_COMMITS_THRESHOLD,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a drift-warning dict if codevira's roadmap is stale, else None.

    Args:
      project_root: project being checked. Required to find .git and roadmap.yaml.
      current_phase: parsed roadmap current phase dict (optional — falls back
                     to roadmap.yaml mtime if missing or has no timestamps).
      roadmap_path: explicit path to roadmap.yaml (default <project>/.codevira/roadmap.yaml).
      days_threshold, commits_threshold: tuneable, see module docstring.
      now: injectable for tests.

    The function NEVER raises. All failure paths return None silently.
    """
    try:
        ref_time = _resolve_reference_time(
            current_phase=current_phase,
            roadmap_path=roadmap_path,
            project_root=project_root,
        )
        if ref_time is None:
            return None

        now = now or datetime.now(timezone.utc)
        # Both must be tz-aware for safe comparison.
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

        days_since = (now - ref_time).total_seconds() / 86400.0

        commits_since, commit_subjects = _git_commits_since(
            project_root=project_root,
            since=ref_time,
        )

        # Drift fires if either threshold is breached.
        if days_since <= days_threshold and commits_since <= commits_threshold:
            return None

        # Format the human-readable message. The AI will see this in the
        # SessionStart context — phrase it as advice, not a command.
        msg_parts = ["⚠ Codevira roadmap may be stale."]
        if days_since > days_threshold:
            msg_parts.append(f"Last update {int(days_since)} days ago.")
        if commits_since > commits_threshold:
            msg_parts.append(f"{commits_since} commits landed since.")
        msg_parts.append(
            "Before relying on get_roadmap state, consider reviewing recent "
            "commits and calling update_phase_status / complete_phase / "
            "record_decision (via write_session_log) to keep memory current."
        )

        return {
            "drifted": True,
            "days_since_update": round(days_since, 1),
            "commits_since": commits_since,
            "last_phase_update": ref_time.isoformat(),
            "recent_commit_subjects": commit_subjects[:5],
            "thresholds": {
                "days": days_threshold,
                "commits": commits_threshold,
            },
            "message": " ".join(msg_parts),
        }
    except Exception as exc:  # noqa: BLE001
        # Never let drift detection crash the caller.
        logger.debug("roadmap drift check failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_reference_time(
    *,
    current_phase: dict[str, Any] | None,
    roadmap_path: Path | None,
    project_root: Path,
) -> datetime | None:
    """Pick the freshest signal of "when did codevira last sync"."""
    candidates: list[datetime] = []

    if current_phase:
        for key in ("last_updated", "updated_at", "started"):
            raw = current_phase.get(key)
            if not raw:
                continue
            parsed = _parse_iso(raw)
            if parsed is not None:
                candidates.append(parsed)

    # Fallback / additional signal: roadmap.yaml mtime
    rp = roadmap_path or (project_root / ".codevira" / "roadmap.yaml")
    if rp.exists():
        try:
            mtime = datetime.fromtimestamp(rp.stat().st_mtime, tz=timezone.utc)
            candidates.append(mtime)
        except OSError:
            pass

    if not candidates:
        return None

    # Freshest wins — if the user manually edited roadmap.yaml today,
    # that beats a "started: 2026-05-02" string.
    return max(candidates)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string. Returns None on any failure."""
    if not isinstance(value, str):
        return None
    try:
        # Accept both "2026-05-02T23:21:00Z" and "2026-05-02T23:21:00+00:00"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _git_commits_since(
    *,
    project_root: Path,
    since: datetime,
) -> tuple[int, list[str]]:
    """Run ``git log --since=<iso>`` and return (count, recent_subjects).

    Returns (0, []) if git is unavailable, the project isn't a git repo,
    or the subprocess errors out. Drift detection then relies on time
    alone, which is still useful.
    """
    git = shutil.which("git")
    if not git:
        return 0, []

    if not (project_root / ".git").exists():
        return 0, []

    try:
        result = subprocess.run(
            [
                git,
                "-C",
                str(project_root),
                "log",
                f"--since={since.isoformat()}",
                "--pretty=%s",
                f"--max-count={_MAX_COMMITS_TO_INSPECT}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0, []

    if result.returncode != 0:
        return 0, []

    subjects = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return len(subjects), subjects
