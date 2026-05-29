"""
cli_consensus.py — v3.1.0 M6 Phase B: ``codevira consensus check`` CLI.

Read-only scan that materializes cross-IDE conflicts to
``.codevira/pending_conflicts.jsonl`` for human review. Calls into
``consensus_store.scan_and_materialize`` so the same path also
backs the ``consensus_check`` MCP tool.
"""

from __future__ import annotations

import sys


def cmd_consensus_check(*, verbose: bool = False) -> int:
    """Entry point for ``codevira consensus check``.

    Returns 0 on success (including no conflicts found). Non-zero only
    on storage / IO errors raised by the scan.
    """
    try:
        from mcp_server.storage import consensus_store
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"codevira consensus check: consensus_store import failed: {exc}\n"
        )
        return 1

    try:
        summary = consensus_store.scan_and_materialize()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"codevira consensus check: scan failed: {exc}\n")
        return 1

    if summary.get("skipped_reason"):
        sys.stdout.write(
            f"codevira consensus check: skipped — {summary['skipped_reason']}.\n"
            f"  Set CODEVIRA_IDE in your MCP config (ide_inject.py handles "
            f"this for newly-injected IDE configs) and re-run.\n"
        )
        return 0

    sys.stdout.write(
        f"codevira consensus check: scanned {summary.get('scanned', 0)} "
        f"decision(s) since last checkpoint "
        f"(foreign-IDE: {summary.get('foreign', 0)}; "
        f"conflicts recorded: {summary.get('conflicts_recorded', 0)}).\n"
        f"  Checkpoint advanced to "
        f"{summary.get('new_checkpoint') or '<none>'}.\n"
    )
    if summary.get("conflicts_recorded"):
        from mcp_server.storage import paths

        sys.stdout.write(f"  Review: {paths.pending_conflicts_path()}\n")
    return 0
