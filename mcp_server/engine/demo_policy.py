"""
demo_policy.py — minimal policy that proves the engine wiring end-to-end.

Behavior: blocks Edit/Write of any file ending in ``.py.bak``. That's it.

This policy is not a hero. It exists so the engine sprint has an
acceptance test target: register this policy, fire a synthetic
PRE_TOOL_USE event for an Edit on ``foo.py.bak``, and the engine's
verdict must be ``block`` with the right message.

Heroes 1-10 will be MUCH bigger; this file is the "hello world" that
verifies events → policies → verdict combination → wiring response all
work together. Once the real heroes ship, this file is deleted (or
moved to tests).

Activated only when CODEVIRA_DEMO_POLICY=1 is set in the env. We don't
want a stray ``.py.bak`` block firing in production.
"""
from __future__ import annotations

import os

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.runner import register_policy


class BackupExtensionGuard(Policy):
    """Block edits to files ending in .py.bak (demo policy for engine wiring)."""

    name = "demo_backup_guard"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = False  # opt-in via env var
    priority = -100  # low priority — real heroes outrank it

    def evaluate(self, event: HookEvent) -> PolicyVerdict:
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()
        if event.target_file.name.endswith(".py.bak"):
            return PolicyVerdict.block(
                message=(
                    f"Edit blocked: {event.target_file.name} is a backup file. "
                    "Refusing to modify .py.bak — work on the original .py "
                    "source instead. (demo_backup_guard policy)"
                ),
                metadata={"target_file": str(event.target_file)},
            )
        return PolicyVerdict.allow()


def maybe_register() -> None:
    """Register the demo policy IFF the env var is set.

    Called from engine/__init__.py path? No — we keep this opt-in via
    a separate import. Tests call this directly. The CLI hook entry
    in mcp_server.cli flips this on if CODEVIRA_DEMO_POLICY=1.
    """
    if os.environ.get("CODEVIRA_DEMO_POLICY", "0") != "1":
        return
    register_policy(BackupExtensionGuard())
