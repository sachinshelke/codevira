"""
post_edit_refresh.py — v2.1.2 Item 4 — auto-refresh graph nodes after Edit/Write.

Problem (Report 2 §"stale graph"): when an AI tool edits a source file,
the file watcher reindexes the Chroma chunks but the graph DB's
``nodes`` row keeps its OLD ``dependencies`` list and the
``_check_staleness`` returns ``stale: True`` indefinitely. Until the
user runs ``codevira index --full`` the AI keeps seeing stale data.

Design: a Policy registered for POST_TOOL_USE on Edit / Write /
MultiEdit. After the tool succeeds, schedule a per-file graph node
refresh in a background daemon thread so the response isn't blocked.

P9 graceful: refresh failures are logged but never block the AI tool
response. The policy ALWAYS returns ``allow`` — it's strictly a side
effect.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

logger = logging.getLogger(__name__)


# Tools that mutate file content and therefore need a graph refresh.
_REFRESHABLE_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})


def _background_refresh(file_path: str) -> None:
    """Refresh one file's graph row. Always logs, never raises."""
    try:
        from mcp_server.tools.graph import refresh_graph

        # refresh_graph(file_paths=[...]) does a targeted single-file refresh.
        refresh_graph(file_paths=[file_path])
    except Exception as exc:  # noqa: BLE001
        logger.debug("post_edit_refresh: refresh failed for %s: %s", file_path, exc)


class PostEditGraphRefresh(Policy):
    """v2.1.2 Item 4: refresh graph nodes after Edit/Write/MultiEdit.

    Side-effect-only policy. Always returns allow. Schedules a daemon
    thread to update the file's graph row + dependency edges in the
    background so the AI's next ``get_node`` / ``get_impact`` call sees
    current data.
    """

    name = "post_edit_graph_refresh"
    handles = (EventType.POST_TOOL_USE,)
    enabled_by_default = True
    # Low priority — purely informational. Runs after meaningful policies.
    priority = 5

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Only Edit / Write / MultiEdit need graph refresh.
        if event.tool_name not in _REFRESHABLE_TOOLS:
            return PolicyVerdict.allow()

        # Extract the file path from tool_input.
        tool_input: dict[str, Any] = event.tool_input or {}
        file_path = tool_input.get("file_path")
        if not file_path or not isinstance(file_path, str):
            return PolicyVerdict.allow()

        # Schedule background refresh. Daemon so it never blocks shutdown.
        try:
            t = threading.Thread(
                target=_background_refresh,
                args=(file_path,),
                daemon=True,
                name=f"codevira-post-edit-refresh-{file_path[:30]}",
            )
            t.start()
        except Exception as exc:  # noqa: BLE001
            logger.debug("post_edit_refresh: thread spawn failed: %s", exc)

        return PolicyVerdict.allow(
            metadata={"_post_edit_refresh_scheduled_for": file_path},
        )
