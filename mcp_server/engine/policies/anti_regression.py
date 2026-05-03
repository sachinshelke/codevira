"""
anti_regression.py — Hero 2: Anti-Regression Memory policy.

Fires on PreToolUse Edit/Write/MultiEdit. Reads the project's
``fix_history.db`` (populated by Week-2's ``scan_git_log`` and
``codevira fix-noted``), checks whether the AI's proposed change
looks like a revert of a previously-fixed bug, and blocks if so.

The bulk of the heuristic complexity lives in
``indexer.fix_history.is_revert`` (Week 2). Hero 2 is the policy
that calls it.

See ``docs/heroes/02-anti-regression.md`` for the spec.
"""
from __future__ import annotations

import os
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "block"
_MODES = ("off", "warn", "block")

#: Per-file cap on fixes we'll evaluate. If a project has 200 recorded
#: fixes for one file, evaluating ``is_revert`` against each on every
#: Edit blows the latency budget. Take the most-recent 20; older ones
#: are less likely to still be relevant. (Documented in the spec; if
#: a real user reports missed regressions, lift to 50.)
_MAX_FIXES_PER_FILE = 20


class AntiRegression(Policy):
    """Block edits that look like reverts of previously-fixed bugs."""

    name = "anti_regression"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    # Between Decision Lock (100) and Blast-Radius (50). Anti-regression
    # is a hard signal (the bug WAS fixed, the fix is in git) but lower
    # than Decision Lock (architectural choice locked by the user).
    priority = 80

    # ---- Configuration ----

    def _config(self) -> dict[str, Any]:
        mode_raw = os.environ.get(
            "CODEVIRA_ANTI_REGRESSION_MODE", _DEFAULT_MODE,
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        return {"mode": mode}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_ANTI_REGRESSION_MODE",
                "description": "off | warn | block",
            },
        }

    # ---- Evaluation ----

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

        if signals is None:
            return PolicyVerdict.allow()

        # Stage 2: pull recorded fixes for this file.
        # Empty-fixes early-return is a fast-path; the loop below
        # would also produce allow with empty fixes (zero iterations
        # → empty `reverting` → final `if not reverting` gate). This
        # check exists for clarity and to skip the diff parse when
        # there's nothing to check against. Honest note: mutation
        # testing exposed it as observably redundant with the
        # empty-reverting gate at line ~115. Keep it for clarity.
        fixes = signals.fixes(event.target_file)
        if not fixes:
            return PolicyVerdict.allow()

        # Stage 3: need a diff to do revert detection
        diff = event.proposed_diff
        if diff is None:
            # Full Write — Hero 4 (Blast-Radius) handles that case.
            # Anti-regression with no diff has nothing to compare.
            return PolicyVerdict.allow()

        # Stage 4: check each (top-N) fix for revert match
        from indexer.fix_history import is_revert
        # Newest fixes first (signals.fixes already returns newest-first
        # per Week-2 SQL ORDER BY).
        candidates = fixes[:_MAX_FIXES_PER_FILE]
        reverting: list[dict[str, Any]] = []
        for fix in candidates:
            try:
                if is_revert(diff, fix):
                    reverting.append(fix)
            except Exception:  # noqa: BLE001
                # Per-fix failure (malformed fix record, etc.) MUST NOT
                # crash evaluation. Skip and continue.
                continue

        if not reverting:
            return PolicyVerdict.allow()

        return self._make_verdict(
            event=event, config=config,
            reverting=reverting, total_fixes_count=len(fixes),
        )

    # ---- Verdict construction ----

    def _make_verdict(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        reverting: list[dict[str, Any]],
        total_fixes_count: int,
    ) -> PolicyVerdict:
        target_name = (
            event.target_file.name if event.target_file else "<unknown>"
        )

        # Top-3 reverting fixes for the message
        sample_lines: list[str] = []
        for fix in reverting[:3]:
            sha = fix.get("commit_sha", "")
            sha_short = (sha[:8] + "...") if sha else "(manual flag)"
            description = (fix.get("description") or "").strip()
            if len(description) > 120:
                description = description[:117] + "..."
            line_range = ""
            line_start = fix.get("line_start", 0)
            line_end = fix.get("line_end", 0)
            if line_start or line_end:
                line_range = f" (lines {line_start}-{line_end})"
            sample_lines.append(
                f"  • {sha_short}: {description!r}{line_range}"
            )
        more = (
            f"\n  ... and {len(reverting) - 3} more"
            if len(reverting) > 3 else ""
        )

        message = (
            f"🛑 Anti-regression veto on {target_name}: this edit "
            f"appears to revert {len(reverting)} previously-fixed "
            f"bug(s).\n\n"
            f"Past fixes that may be at risk:\n"
            f"{chr(10).join(sample_lines)}{more}\n\n"
            f"To proceed safely:\n"
            f"  1. Confirm with the user that the bug condition is no "
            f"longer relevant (e.g. the threading model changed, the\n"
            f"     code path is now unreachable, etc.).\n"
            f"  2. If the user confirms, override this policy session "
            f"with CODEVIRA_ANTI_REGRESSION_MODE=warn (warns instead\n"
            f"     of blocks) or =off (disables this policy)."
        )

        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "mode": config["mode"],
            "reverting_count": len(reverting),
            "total_fixes_for_file": total_fixes_count,
            "reverting_commit_shas": [
                f.get("commit_sha") for f in reverting if f.get("commit_sha")
            ],
        }

        if config["mode"] == "block":
            return PolicyVerdict.block(message=message, metadata=metadata)
        return PolicyVerdict.warn(message=message, metadata=metadata)
