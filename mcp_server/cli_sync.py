"""
cli_sync.py — v2.2.0 ``codevira sync`` command.

Regenerates derived state from the canonical ``.codevira/decisions.jsonl``:

  - ``.codevira/manifest.yaml``        (tag/file → id index)
  - ``.codevira/digest.jsonl``         (slim per-decision records)
  - ``.codevira-cache/fts5.sqlite``    (BM25 search index)
  - ``AGENTS.md``                       (slim contract for other AI tools)

When to run it:

  - After hand-editing ``decisions.jsonl`` to apply your changes
  - After ``git pull`` if the cache files weren't pulled (they're
    gitignored; only the source-of-truth files come down)
  - When ``codevira doctor`` reports a stale or mismatched index
  - First time after upgrading to v2.2.0 (one-shot bootstrap)

You should NOT need to run it in normal use — every
``record_decision`` / ``record_decisions`` / ``supersede_decision`` /
``mark_decision_protected`` call regenerates these synchronously.
``sync`` is the manual / recovery path.
"""

from __future__ import annotations

import sys


def cmd_sync(*, dry_run: bool = False, verbose: bool = False) -> int:
    """Regenerate AGENTS.md + manifest + digest + FTS5 from decisions.jsonl.

    Args:
        dry_run: report what would change without writing.
        verbose: print per-step counts.

    Returns POSIX exit code (0 success, 1 error).
    """
    from mcp_server.storage import (
        agents_md_generator,
        decisions_store,
        jsonl_store,
        paths,
    )

    if not paths.is_initialized():
        print(
            "Error: no .codevira/ found in this project. "
            "Run `codevira init` first to scaffold it.",
            file=sys.stderr,
        )
        return 1

    decisions_count = jsonl_store.count(paths.decisions_path())
    print()
    print(f"  Codevira — Sync ({decisions_count} decision(s))")
    print(f"  Project: {paths.codevira_dir().parent}")
    print("  " + "─" * 60)
    print()

    if dry_run:
        print(f"  [dry-run] Would regenerate from {paths.decisions_path()}:")
        print("    .codevira/manifest.yaml")
        print("    .codevira/digest.jsonl")
        print("    .codevira-cache/fts5.sqlite")
        print("    AGENTS.md")
        print()
        return 0

    # Step 1: rebuild manifest + digest + FTS5 (decisions_store handles all 3).
    try:
        decisions_store.rebuild_indexes()
        if verbose:
            print("  ✓ Regenerated manifest.yaml + digest.jsonl + fts5.sqlite")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ index rebuild failed: {exc}", file=sys.stderr)
        return 1

    # Step 2: regenerate AGENTS.md
    try:
        summary = agents_md_generator.regenerate()
        if verbose:
            print(
                f"  ✓ Regenerated {summary['agents_md_path']} "
                f"({summary['block_bytes']:,} bytes; "
                f"{summary['decisions_in_block']} in block; "
                f"{summary['decisions_dropped']} dropped past 5 KB cap)"
            )
        else:
            print(f"  ✓ AGENTS.md regenerated ({summary['block_bytes']:,} bytes)")
        if summary["user_content_preserved_bytes"] > 0:
            print(
                f"    Preserved {summary['user_content_preserved_bytes']:,} bytes "
                f"of user content outside codevira markers."
            )
        if not summary["block_within_cap"]:
            print(
                f"  ⚠ Codevira block exceeded 5 KB cap "
                f"({summary['block_bytes']:,} bytes). Older decisions dropped.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ AGENTS.md regenerate failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("  ✓ Sync complete.")
    print()
    return 0
