"""
blast_radius.py — Hero 4: Blast-Radius Veto policy.

When the AI tries to Edit / Write / MultiEdit a file with N downstream
callers AND the change modifies a public signature, refuse the edit
with a clear diagnostic.

See ``docs/heroes/04-blast-radius.md`` for the spec, including the
decision tree, configuration knobs, and acceptance scenarios.
"""

from __future__ import annotations

import os
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext
from mcp_server.engine.policies._signature_detect import (
    change_touches_signature,
    language_for_path,
    signature_change_summary,
)


# Defaults match the spec; overridden via env vars (see below).
_DEFAULT_BLOCK_THRESHOLD = 5
_DEFAULT_WARN_THRESHOLD = 3
_DEFAULT_MODE = "block"

# Allowed modes
_MODES = ("off", "warn", "block")


class BlastRadiusVeto(Policy):
    """Block / warn on Edits to high-impact files that change public
    signatures. v2.0 hero #4.
    """

    name = "blast_radius_veto"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    # Mid-priority: runs after hard locks (Decision Lock would be ~100)
    # but before advisory policies. The runner combines verdicts so the
    # priority only affects ordering of warn/inject messages, not
    # block precedence (any block is final).
    priority = 50

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        """Read configuration. Env-var-only for v2.0-alpha; YAML config
        integration deferred to v2.1.

        Returns a dict with ``mode``, ``block_threshold``, ``warn_threshold``.
        Invalid values fall back to defaults; we never crash on bad config.
        """
        mode_raw = (
            os.environ.get("CODEVIRA_BLAST_RADIUS_MODE", _DEFAULT_MODE).strip().lower()
        )
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE

        block_threshold = self._safe_int_env(
            "CODEVIRA_BLAST_RADIUS_THRESHOLD",
            _DEFAULT_BLOCK_THRESHOLD,
        )
        warn_threshold = self._safe_int_env(
            "CODEVIRA_BLAST_RADIUS_WARN_THRESHOLD",
            _DEFAULT_WARN_THRESHOLD,
        )

        # Defensive: keep warn ≤ block. If the user sets warn=10 and
        # block=5, that's nonsense — clamp warn down so the policy
        # behaves predictably.
        if warn_threshold > block_threshold:
            warn_threshold = block_threshold

        return {
            "mode": mode,
            "block_threshold": block_threshold,
            "warn_threshold": warn_threshold,
        }

    @staticmethod
    def _safe_int_env(key: str, default: int) -> int:
        """Parse env var as int with defensive clamps. Returns default
        on missing, negative, or non-int values.
        """
        raw = os.environ.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return default
        # Clamp to a sane range — no one needs a threshold of 10**9.
        if value < 1:
            return default
        if value > 10_000:
            return 10_000
        return value

    def config_schema(self) -> dict[str, Any]:
        """Surface config knobs to ``codevira doctor`` etc."""
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_BLAST_RADIUS_MODE",
                "description": "off | warn | block",
            },
            "block_threshold": {
                "type": "integer",
                "default": _DEFAULT_BLOCK_THRESHOLD,
                "env": "CODEVIRA_BLAST_RADIUS_THRESHOLD",
                "description": "Min blast radius (caller count) to trigger block in 'block' mode.",
            },
            "warn_threshold": {
                "type": "integer",
                "default": _DEFAULT_WARN_THRESHOLD,
                "env": "CODEVIRA_BLAST_RADIUS_WARN_THRESHOLD",
                "description": "Min blast radius to trigger warn in 'warn' mode.",
            },
        }

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        """The decision tree from docs/heroes/04-blast-radius.md.

        ``signals`` is optional only for backwards-compat with the
        Week-1 engine API; the runner always supplies it. When called
        directly (e.g. unit tests), pass a fake context.
        """
        # Stage 1: structural filters — no signal access needed.
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: graph signal — may return empty if uninitialized.
        if signals is None:
            return PolicyVerdict.allow()  # nothing we can do

        impact = signals.impact(event.target_file)
        if not impact or not impact.get("found"):
            return PolicyVerdict.allow()

        radius = int(impact.get("blast_radius", 0))
        threshold = (
            config["block_threshold"]
            if config["mode"] == "block"
            else config["warn_threshold"]
        )
        if radius < threshold:
            return PolicyVerdict.allow()

        # Stage 3: signature analysis — only meaningful if we have a diff.
        diff = event.proposed_diff
        if diff is None:
            # No diff = full Write replace; treat as signature change.
            # If we wanted finer-grained behavior, we could fetch the
            # current file and synthesize a diff, but for v2.0-alpha
            # we err on the side of blocking — a Write of a high-impact
            # file usually IS a signature change.
            return self._make_verdict(
                event=event,
                config=config,
                radius=radius,
                summary={"added": [], "removed": [], "modified": ["(full Write)"]},
                impact=impact,
            )

        language = language_for_path(str(event.target_file))
        if not change_touches_signature(diff, language=language):
            return PolicyVerdict.allow()

        summary = signature_change_summary(diff, language=language)
        # v3.3.0 Phase 7: purely-ADDED signatures cannot break existing
        # callers — nothing depends on a function that didn't exist yet.
        # Only removed/modified signatures are caller-breaking. (Found by
        # dogfooding 2026-06-12: the veto blocked two legitimate edits
        # that added private helpers to high-fan-in files.)
        if not summary.get("removed") and not summary.get("modified"):
            return PolicyVerdict.allow(
                metadata={
                    "policy": self.name,
                    "target_file": str(event.target_file),
                    "blast_radius": radius,
                    "reason": "signature_changes_purely_additive",
                    "signature_changes": summary,
                }
            )
        return self._make_verdict(
            event=event,
            config=config,
            radius=radius,
            summary=summary,
            impact=impact,
        )

    # ------------------------------------------------------------------
    # Verdict construction
    # ------------------------------------------------------------------

    def _make_verdict(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        radius: int,
        summary: dict[str, list[str]],
        impact: dict[str, Any],
    ) -> PolicyVerdict:
        """Build the warn-or-block verdict with a useful diagnostic."""
        target_name = event.target_file.name if event.target_file else "<unknown>"

        # Top-3 signature changes (added + removed + modified) for the
        # message — the user wants to see WHICH change is high-risk,
        # not a generic "something changed."
        change_lines: list[str] = []
        for kind in ("modified", "removed", "added"):
            for line in summary.get(kind, []):
                change_lines.append(f"  {kind}: {line.strip()}")
        sample = "\n".join(change_lines[:3])
        more = (
            f"\n  ... and {len(change_lines) - 3} more" if len(change_lines) > 3 else ""
        )

        # Top-3 affected files for context. We compute "more" against
        # the LIST length (not blast_radius) so the message stays
        # consistent even if signals.impact() ever returns a partial
        # affected list — the user sees what we ACTUALLY have, not what
        # the count claims (Week-4 R1 #1 defensive nit).
        # NOTE: signals.impact() / get_impact return the list under
        # "affected_files" (each item keyed "file") — NOT "affected". The
        # old key silently rendered the "Affected files" list empty while
        # the block itself stayed correct.
        affected_full = impact.get("affected_files", [])
        affected = affected_full[:3]
        affected_lines = "\n".join(f"  • {a.get('file')}" for a in affected)
        n_more = max(0, len(affected_full) - 3)
        affected_more = f"\n  ... and {n_more} more" if n_more else ""

        message = (
            f"🛑 Blast-radius veto on {target_name}: {radius} downstream "
            f"file(s) depend on this code, and your edit modifies a public "
            f"signature.\n\n"
            f"Signature changes detected:\n{sample or '  (none parsed)'}{more}\n\n"
            f"Affected files (top 3):\n{affected_lines}{affected_more}\n\n"
            f"To proceed safely:\n"
            f"  1. Read the affected files (Grep / Read) and propose a\n"
            f"     MultiEdit covering all of them, OR\n"
            f"  2. If you've confirmed callers won't break, override with\n"
            f"     CODEVIRA_BLAST_RADIUS_MODE=warn (warns instead of blocks)\n"
            f"     or =off (disables this policy)."
        )

        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "blast_radius": radius,
            "mode": config["mode"],
            "threshold": (
                config["block_threshold"]
                if config["mode"] == "block"
                else config["warn_threshold"]
            ),
            "signature_changes": summary,
        }

        if config["mode"] == "block":
            return PolicyVerdict.block(message=message, metadata=metadata)
        return PolicyVerdict.warn(message=message, metadata=metadata)
