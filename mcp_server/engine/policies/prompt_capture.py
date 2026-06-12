"""
prompt_capture.py — v3.3.0 Phase 4: raw prompt capture + distill nudge.

First half of the preference-capture loop (decision D0000LU):

  raw prompts → LLM distillation (distill_preferences tool) → budgeted
  injection (get_session_context style panel)

Fires on two events:

  - ``USER_PROMPT_SUBMIT`` → appends ``{ts, session_id, prompt}`` to
    ``<project>/.codevira-cache/prompts.jsonl`` (per-machine, gitignored,
    size-capped). The prompt text is sanitized with the same scrubber
    decisions go through. Capture is deliberately rule-free — no
    keyword/regex filtering decides what "looks like" an instruction;
    the LLM judges at distillation time. (User decision 2026-06-12:
    "what user wants we never know — memory has to be smart.")

  - ``STOP`` → if enough captured prompts are pending AND the nudge
    cooldown has elapsed, emit a ``warn`` asking the AI to call the
    ``distill_preferences`` MCP tool. The Stop hook runs as a CLI
    subprocess with no MCP client connection, so it CANNOT call
    sampling itself — the nudge-the-AI pattern (same as
    session_log_enforcer) is the session-end trigger.

History note: v2.2.0 deleted Hero 7 (live_style.LiveStyleEnforcement)
because the old frequency-counted preferences surface was noise. This
revival is a different mechanism — LLM-distilled at session end, stored
in global.db (user-scoped, not repo-scoped), surfaced as ONE budgeted
line — and is throttled here (pending threshold + cooldown) so the
nudge can't become the new noise.

Degradation: any capture failure returns ``allow`` with a metadata
error — a broken cache file can never block or alter a prompt.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

_CACHE_REL = ".codevira-cache"
_PROMPTS_FILENAME = "prompts.jsonl"
_NUDGE_MARKER_FILENAME = "prompts_nudge_marker.json"

# P5 bounds: rotate the capture file at this size (same single-.1
# rotation as enforcer_outcomes); skip prompts beyond this length
# (pasted logs/diffs aren't instructions — and they'd blow the
# distillation context).
_PROMPTS_MAX_BYTES = 256 * 1024
_MAX_PROMPT_CHARS = 2_000

_DEFAULT_MODE = "warn"
_MODES = ("off", "warn")
_DEFAULT_NUDGE_THRESHOLD = 10
_DEFAULT_NUDGE_COOLDOWN_S = 24 * 3600.0


class PromptCapture(Policy):
    """Capture user prompts for session-end preference distillation."""

    name = "prompt_capture"
    handles = (EventType.USER_PROMPT_SUBMIT, EventType.STOP)
    enabled_by_default = True
    priority = 90  # late — relevance_inject (read side) should run first

    def _config(self) -> dict[str, Any]:
        mode_raw = (
            os.environ.get("CODEVIRA_PROMPT_CAPTURE_MODE", _DEFAULT_MODE)
            .strip()
            .lower()
        )
        return {"mode": mode_raw if mode_raw in _MODES else _DEFAULT_MODE}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_PROMPT_CAPTURE_MODE",
                "description": (
                    "off (no capture, no nudge) | warn (default — capture "
                    "prompts; nudge distill_preferences on Stop when enough "
                    "are pending and the cooldown elapsed)"
                ),
            },
        }

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        if self._config()["mode"] == "off":
            return PolicyVerdict.allow()
        if event.event_type == EventType.USER_PROMPT_SUBMIT:
            return self._on_prompt(event)
        if event.event_type == EventType.STOP:
            return self._on_stop(event)
        return PolicyVerdict.allow()

    # ------------------------------------------------------------------
    # USER_PROMPT_SUBMIT — capture
    # ------------------------------------------------------------------

    def _on_prompt(self, event: HookEvent) -> PolicyVerdict:
        text = (event.prompt_text or "").strip()
        if not text:
            return PolicyVerdict.allow(
                metadata={"policy": self.name, "captured": False, "reason": "empty"}
            )
        if len(text) > _MAX_PROMPT_CHARS:
            text = text[:_MAX_PROMPT_CHARS]
        try:
            from mcp_server.storage.sanitize import scrub_sensitive

            text = scrub_sensitive(text)
            path = prompts_path(event.project_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size >= _PROMPTS_MAX_BYTES:
                    path.replace(path.with_suffix(path.suffix + ".1"))
            except FileNotFoundError:
                pass
            record = {
                "ts": event.timestamp or time.time(),
                "session_id": event.session_id,
                "prompt": text,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "captured": False,
                    "error": f"write_failed:{type(exc).__name__}",
                }
            )
        return PolicyVerdict.allow(metadata={"policy": self.name, "captured": True})

    # ------------------------------------------------------------------
    # STOP — distill nudge (throttled)
    # ------------------------------------------------------------------

    def _on_stop(self, event: HookEvent) -> PolicyVerdict:
        try:
            pending = count_pending(event.project_root)
            if pending < _DEFAULT_NUDGE_THRESHOLD:
                return PolicyVerdict.allow(
                    metadata={"policy": self.name, "pending": pending}
                )
            marker = _nudge_marker_path(event.project_root)
            now = time.time()
            if marker.is_file():
                try:
                    last = float(
                        json.loads(marker.read_text(encoding="utf-8")).get(
                            "last_nudge_ts", 0.0
                        )
                    )
                except (json.JSONDecodeError, ValueError, TypeError):
                    last = 0.0
                if now - last < _DEFAULT_NUDGE_COOLDOWN_S:
                    return PolicyVerdict.allow(
                        metadata={
                            "policy": self.name,
                            "pending": pending,
                            "reason": "cooldown",
                        }
                    )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"last_nudge_ts": now}), encoding="utf-8")
        except OSError as exc:
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "error": f"nudge_check_failed:{type(exc).__name__}",
                }
            )
        return PolicyVerdict.warn(
            f"[codevira] {pending} user prompts captured since the last "
            f"preference distillation. Call the `distill_preferences` MCP "
            f"tool (dry_run=False) before finishing — it asks the host LLM "
            f"to extract durable communication/workflow preferences and "
            f"stores them in your cross-project memory.",
            metadata={"policy": self.name, "pending": pending, "nudged": True},
        )


# ----------------------------------------------------------------------
# Helpers — shared with mcp_server.tools.preferences
# ----------------------------------------------------------------------


def prompts_path(project_root: Path) -> Path:
    """Location of the captured-prompts file (per-machine cache)."""
    return project_root / _CACHE_REL / _PROMPTS_FILENAME


def _nudge_marker_path(project_root: Path) -> Path:
    return project_root / _CACHE_REL / _NUDGE_MARKER_FILENAME


def count_pending(project_root: Path) -> int:
    """Number of captured prompts awaiting distillation."""
    path = prompts_path(project_root)
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def read_pending(project_root: Path) -> list[dict[str, Any]]:
    """All captured prompts (bad lines skipped)."""
    path = prompts_path(project_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
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
                if isinstance(row, dict) and row.get("prompt"):
                    rows.append(row)
    except OSError:
        return []
    return rows


def clear_pending(project_root: Path) -> None:
    """Empty the capture file after a successful distillation (atomic)."""
    path = prompts_path(project_root)
    if not path.is_file():
        return
    tmp = path.with_suffix(path.suffix + ".clearing")
    tmp.write_text("", encoding="utf-8")
    os.replace(str(tmp), str(path))
