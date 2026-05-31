"""
session_log_enforcer.py — v3.2.0 hook-layer enforcement of write_session_log.

CLAUDE.md's "before-you-finish" contract told the AI to call
``write_session_log`` whenever a session shipped commits. v3.1.x left this
on the honor system because no engine layer detected the gap. This policy
closes that gap.

Fires on two events:

  - ``SESSION_START`` → appends ``{session_id, started_at, project_root}``
    to ``<project>/.codevira-cache/active_sessions.jsonl``. Per-machine,
    gitignored. ~5ms.

  - ``STOP`` → looks up the active-session record, then:
      1. Counts commits in ``project_root`` since ``started_at``.
         (Any commit, regardless of author — recommendation accepted
         interactively 2026-05-31.)
      2. Scans ``.codevira/sessions.jsonl`` for an entry whose timestamp
         falls in [started_at, now]. (Claude Code's session_id is a UUID;
         the user picks a short slug for sessions.jsonl — so we match by
         time window, not by id.)
      3. If commits > 0 AND no matching session entry → emit ``warn``
         with a call-template the AI can paste into its next turn.

Failure modes:
  - Non-git project → ``git rev-parse`` fails → policy returns ``allow``.
  - No SESSION_START record → ``allow`` (Claude Code may have restored
    a cached session without firing SESSION_START; better to under-fire
    than over-warn).
  - Any disk/parse error → ``allow`` + ``metadata.error`` for ``codevira
    doctor`` to surface.

Ship plan:
  - v3.2.0: ``warn`` only — non-blocking, instruments how often the gap
    exists in real sessions.
  - v3.2.1 (planned): once data confirms the warn isn't noisy, upgrade
    to ``block`` so Claude Code's Stop hook re-engages the AI until the
    log lands.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

_CACHE_REL = ".codevira-cache"
_ACTIVE_FILENAME = "active_sessions.jsonl"
_SESSIONS_REL = ".codevira/sessions.jsonl"

_DEFAULT_MODE = "warn"
_MODES = ("off", "warn", "block")


class SessionLogEnforcer(Policy):
    """Nudge the AI to call write_session_log whenever a session shipped commits."""

    name = "session_log_enforcer"
    handles = (EventType.SESSION_START, EventType.STOP)
    enabled_by_default = True
    priority = 5

    def _config(self) -> dict[str, Any]:
        mode_raw = (
            os.environ.get(
                "CODEVIRA_SESSION_LOG_ENFORCER_MODE",
                _DEFAULT_MODE,
            )
            .strip()
            .lower()
        )
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {"mode": mode}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_SESSION_LOG_ENFORCER_MODE",
                "description": (
                    "off (disabled) | warn (v3.2.0 default — non-blocking "
                    "nudge) | block (planned for v3.2.1 once warn-mode "
                    "instrumentation confirms low noise)"
                ),
            },
        }

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if event.event_type == EventType.SESSION_START:
            return self._on_session_start(event)
        if event.event_type == EventType.STOP:
            return self._on_stop(event, config["mode"])
        return PolicyVerdict.allow()

    # ------------------------------------------------------------------
    # SESSION_START — record start marker
    # ------------------------------------------------------------------

    def _on_session_start(self, event: HookEvent) -> PolicyVerdict:
        if not event.session_id:
            return PolicyVerdict.allow()
        try:
            active_path = _active_path(event.project_root)
            active_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "session_id": event.session_id,
                "started_at": event.timestamp or time.time(),
                "project_root": str(event.project_root),
            }
            with active_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "session_start",
                    "error": f"write_failed:{type(exc).__name__}",
                }
            )
        return PolicyVerdict.allow(
            metadata={
                "policy": self.name,
                "stage": "session_start",
                "recorded": True,
            }
        )

    # ------------------------------------------------------------------
    # STOP — enforce
    # ------------------------------------------------------------------

    def _on_stop(self, event: HookEvent, mode: str) -> PolicyVerdict:
        if not event.session_id:
            return PolicyVerdict.allow()

        try:
            record = _lookup_active(event.project_root, event.session_id)
        except OSError as exc:
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "stop",
                    "error": f"active_lookup_failed:{type(exc).__name__}",
                }
            )

        if record is None:
            # SESSION_START never fired for this id (cached/restored session,
            # or v3.1.x machine without the marker). Don't warn — better
            # to under-fire than over-warn in warn-mode.
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "stop",
                    "reason": "no_active_record",
                }
            )

        started_at = float(record.get("started_at", 0.0))
        if started_at <= 0:
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "stop",
                    "reason": "invalid_start_time",
                }
            )

        commit_count = _count_commits_since(event.project_root, started_at)
        if commit_count == 0:
            # No commits this session — nothing meaningful to log.
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "stop",
                    "commit_count": 0,
                }
            )

        if _session_log_written(event.project_root, started_at):
            # The AI already called write_session_log — honor it.
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "stage": "stop",
                    "commit_count": commit_count,
                    "log_present": True,
                }
            )

        # GAP DETECTED — emit warn (or block once v3.2.1 lands).
        message = _format_message(
            commit_count=commit_count,
            session_id=event.session_id,
        )
        metadata = {
            "policy": self.name,
            "stage": "stop",
            "commit_count": commit_count,
            "log_present": False,
            "mode": mode,
        }
        if mode == "block":
            return PolicyVerdict.block(message, metadata=metadata)
        return PolicyVerdict.warn(message, metadata=metadata)


# ----------------------------------------------------------------------
# Helpers — pulled out so tests can target them directly
# ----------------------------------------------------------------------


def _active_path(project_root: Path) -> Path:
    return project_root / _CACHE_REL / _ACTIVE_FILENAME


def _lookup_active(project_root: Path, session_id: str) -> dict[str, Any] | None:
    """Find the most recent SESSION_START record for ``session_id``.

    JSONL is append-only, so a session that restarted mid-day would have
    multiple rows. Take the latest.
    """
    path = _active_path(project_root)
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("session_id") != session_id:
                continue
            latest = row
    return latest


def _count_commits_since(project_root: Path, started_at: float) -> int:
    """Count commits in ``project_root`` since ``started_at`` (epoch seconds).

    Uses ``--since=@<epoch>`` so we don't depend on the user's locale /
    timezone (git's default ``--since=<iso>`` parses in *local* time, which
    silently miscounts on machines whose TZ != UTC).

    Returns 0 when the directory isn't a git repo OR git fails for any reason.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since=@{int(started_at)}", "--oneline"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def _session_log_written(project_root: Path, started_at: float) -> bool:
    """True if sessions.jsonl has any entry whose ``ts`` is >= ``started_at``.

    Heuristic: Claude Code's hook session_id is a UUID; the user-supplied
    slug stored in sessions.jsonl is unrelated. Matching by time window
    rather than id is the cleanest cross-walk we can do without modifying
    the sessions.jsonl schema.
    """
    path = project_root / _SESSIONS_REL
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = row.get("ts") or row.get("created_at")
                if not isinstance(ts_str, str):
                    continue
                ts = _parse_iso_to_epoch(ts_str)
                if ts is None:
                    continue
                if ts >= started_at:
                    return True
    except OSError:
        return False
    return False


def _parse_iso_to_epoch(ts: str) -> float | None:
    """Parse an ISO-8601 timestamp (with or without Z) into epoch seconds."""
    from datetime import datetime

    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.timestamp()


def _format_message(*, commit_count: int, session_id: str) -> str:
    plural = "" if commit_count == 1 else "s"
    return (
        f"[codevira] This session shipped {commit_count} commit{plural} "
        f"but no write_session_log call landed. CLAUDE.md's "
        f"'before-you-finish' contract asks for one before stopping. "
        f"Drop this into your final response:\n\n"
        f"  write_session_log(\n"
        f"      session_id='<short-slug>',\n"
        f"      task='<one-line user request>',\n"
        f"      phase='<current phase>',\n"
        f"      files_changed=[...],\n"
        f"      decisions=[{{'decision': '...', 'context': '...'}}],\n"
        f"      next_steps=[...],\n"
        f"  )\n\n"
        f"(Stop event session_id={session_id}; commits counted since "
        f"SESSION_START.)"
    )
