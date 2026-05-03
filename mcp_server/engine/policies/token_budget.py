"""
token_budget.py — Hero 6: Token Budget Live View persistence policy.

Fires on STOP. Persists the current session's TokenMeter summary to
``<data_dir>/logs/token_budget.jsonl`` via the Week-2 plumbing
(``end_session(session_id, project_root=...)``).

This policy NEVER blocks/warns/injects — it's pure telemetry. The
companion ``codevira budget`` CLI reads the same JSONL.

See ``docs/heroes/06-token-budget.md`` for the spec.
"""
from __future__ import annotations

import os
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "persist"
_MODES = ("off", "persist")


class TokenBudgetPersist(Policy):
    """Persist a session's token-meter summary at session end."""

    name = "token_budget_persist"
    handles = (EventType.STOP,)
    enabled_by_default = True
    # Lowest priority — pure post-session telemetry. Other policies
    # that handle STOP (none today; future heroes might) run first.
    priority = 10

    def _config(self) -> dict[str, Any]:
        mode_raw = os.environ.get(
            "CODEVIRA_TOKEN_BUDGET_MODE", _DEFAULT_MODE,
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {"mode": mode}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_TOKEN_BUDGET_MODE",
                "description": "off | persist",
            },
        }

    def evaluate(
        self, event: HookEvent, signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 1: structural filters
        if event.event_type != EventType.STOP:
            return PolicyVerdict.allow()
        if event.session_id is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: persist via Week-2 plumbing
        try:
            from mcp_server.engine.token_meter import end_session
            summary = end_session(
                event.session_id,
                project_root=event.project_root,
            )
        except Exception:  # noqa: BLE001 — never crash on telemetry
            return PolicyVerdict.allow(metadata={
                "policy": self.name,
                "persisted": False,
                "error": "end_session_raised",
            })

        if summary is None:
            # No active meter for this session — common when the AI
            # session never recorded any tokens. Not an error.
            return PolicyVerdict.allow(metadata={
                "policy": self.name,
                "persisted": False,
                "reason": "no_meter_for_session",
            })

        # Stage 3: success — record telemetry in metadata for `codevira
        # doctor` and crash logs to surface.
        return PolicyVerdict.allow(metadata={
            "policy": self.name,
            "persisted": True,
            "session_id": event.session_id,
            "injected_total": summary.get("injected_total", 0),
            "used_total": summary.get("used_total", 0),
            "efficiency": summary.get("efficiency", 0.0),
        })
