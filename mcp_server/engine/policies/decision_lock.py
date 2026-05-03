"""
decision_lock.py — Hero 1: Active Decision Lock policy.

When the AI tries to Edit / Write / MultiEdit a file marked with
``do_not_revert`` in the graph, refuse the edit and surface the
locked decisions so the user can re-engage.

See ``docs/heroes/01-decision-lock.md`` for the spec — decision tree,
configuration knobs, and acceptance scenarios.
"""
from __future__ import annotations

import os
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "block"
_MODES = ("off", "warn", "block")


class DecisionLock(Policy):
    """Block edits to files with locked architectural decisions.

    The lock comes from the graph node's ``do_not_revert`` flag. Per-file
    granularity (every decision attached to a locked file inherits the
    lock); per-decision locking is deferred to v2.1.
    """

    name = "decision_lock"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    # Higher priority than Hero 4 — Decision Lock is a HARD lock. Both
    # can fire on the same event; the runner combines verdicts (any
    # block wins). Priority only affects ordering in warn/inject
    # composition.
    priority = 100

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        """Read env-var configuration. v2.0-alpha.2; YAML config in v2.1.

        Invalid values fall back to defaults; we never crash on bad config.
        """
        mode_raw = os.environ.get("CODEVIRA_DECISION_LOCK_MODE", _DEFAULT_MODE).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {"mode": mode}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_DECISION_LOCK_MODE",
                "description": "off | warn | block",
            },
        }

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self, event: HookEvent, signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 1: structural filters
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: signal lookup
        if signals is None:
            return PolicyVerdict.allow()

        # The graph stores file_path as project-relative. Convert.
        try:
            target_rel = str(event.target_file.relative_to(event.project_root))
        except ValueError:
            target_rel = str(event.target_file)

        # First — is the file locked at all? Check via decisions(file=X,
        # locked_only=True). Empty result = either unlocked OR no decisions.
        # We need to distinguish those for edge case #5.
        locked_decisions = signals.decisions(
            file=target_rel, locked_only=True, limit=20,
        )
        if locked_decisions:
            return self._make_verdict(
                event=event, config=config, decisions=locked_decisions,
                target_rel=target_rel,
            )

        # No locked decisions — but is the file marked do_not_revert
        # without any recorded rationale? (Edge case #5: surface a
        # gentler warn so the user understands what's happening.)
        if self._file_is_locked_without_decisions(signals, target_rel):
            return self._make_verdict_no_rationale(
                event=event, config=config, target_rel=target_rel,
            )

        return PolicyVerdict.allow()

    def _file_is_locked_without_decisions(
        self, signals: SignalContext, target_rel: str,
    ) -> bool:
        """True if the file's graph node has do_not_revert=1 but no
        decisions are attached. Used for edge case #5 (surface a warn
        even in block mode — blocking on no-rationale would be confusing).
        """
        try:
            graph = signals.graph
            if graph is None:
                return False
            row = graph.conn.execute(
                "SELECT do_not_revert FROM nodes WHERE file_path = ? LIMIT 1",
                (target_rel,),
            ).fetchone()
            if row is None:
                return False
            return bool(row["do_not_revert"])
        except Exception:  # noqa: BLE001 — signal layer must never break a policy
            return False

    # ------------------------------------------------------------------
    # Verdict construction
    # ------------------------------------------------------------------

    def _make_verdict(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        decisions: list[dict[str, Any]],
        target_rel: str,
    ) -> PolicyVerdict:
        """Build the warn-or-block verdict for the locked-with-decisions case."""
        target_name = (
            event.target_file.name if event.target_file else target_rel
        )

        # Top-3 decisions for the message
        sample_lines: list[str] = []
        for d in decisions[:3]:
            decision_text = (d.get("decision") or "").strip()
            # Truncate long decisions to keep message readable
            if len(decision_text) > 120:
                decision_text = decision_text[:117] + "..."
            ts = self._format_timestamp(d.get("timestamp"))
            did = d.get("id", "?")
            sample_lines.append(f"  • #{did}: {decision_text!r}{ts}")
        more = (
            f"\n  ... and {len(decisions) - 3} more"
            if len(decisions) > 3 else ""
        )

        message = (
            f"🔒 Decision-lock veto on {target_name}: this file is marked "
            f"do_not_revert with {len(decisions)} locked decision(s).\n\n"
            f"Locked decisions:\n"
            f"{chr(10).join(sample_lines)}{more}\n\n"
            f"To proceed safely:\n"
            f"  1. Surface the decision(s) to the user. The decision was\n"
            f"     locked for a reason — they may have context the AI lacks.\n"
            f"  2. If the user confirms the decision should be revisited,\n"
            f"     unlock the file first via codevira's CLI / API, OR\n"
            f"     override this policy session with\n"
            f"     CODEVIRA_DECISION_LOCK_MODE=warn (warns instead of blocks)\n"
            f"     or =off (disables this policy)."
        )

        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "target_rel": target_rel,
            "mode": config["mode"],
            "locked_decision_count": len(decisions),
            "locked_decision_ids": [d.get("id") for d in decisions[:20]],
        }

        if config["mode"] == "block":
            return PolicyVerdict.block(message=message, metadata=metadata)
        return PolicyVerdict.warn(message=message, metadata=metadata)

    def _make_verdict_no_rationale(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        target_rel: str,
    ) -> PolicyVerdict:
        """Edge case #5: file is locked but no decisions are recorded.

        We deliberately downgrade block → warn here. Blocking with no
        rationale to surface would just frustrate the user. They get a
        warning so they know the file IS locked, with a note that they
        should record their reasoning.
        """
        target_name = (
            event.target_file.name if event.target_file else target_rel
        )
        message = (
            f"⚠️  Decision-lock warn on {target_name}: this file is marked "
            f"do_not_revert but has no recorded decisions.\n\n"
            f"Codevira would normally block, but blocking with no rationale\n"
            f"to show would be unhelpful. Recommendation: record at least\n"
            f"one decision on this file (codevira's record_decision tool)\n"
            f"so future AI sessions understand WHY the lock exists."
        )
        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "target_rel": target_rel,
            "mode": config["mode"],
            "locked_decision_count": 0,
            "locked_without_rationale": True,
        }
        # Always warn, never block — even in block mode.
        return PolicyVerdict.warn(message=message, metadata=metadata)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_timestamp(ts: Any) -> str:
        """Format a stored timestamp for the user-visible message.
        Returns " (locked YYYY-MM-DD)" or empty string if unparseable.
        """
        if ts is None:
            return ""
        try:
            # signals.decisions returns the raw timestamp from SQL —
            # could be epoch seconds OR ISO string depending on how
            # the decision was recorded. Try both.
            from datetime import datetime, timezone
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            else:
                # ISO-ish string
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return f" (locked {dt.date().isoformat()})"
        except Exception:  # noqa: BLE001
            return ""
