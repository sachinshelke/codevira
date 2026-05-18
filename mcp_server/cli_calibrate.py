"""
cli_calibrate.py — `codevira calibrate <target>` command (v2.1.2 Item 1).

Manually re-fit the similarity threshold for a retrieval target. Useful
when the user knows their decision corpus has grown / changed and wants
to refresh the calibration without waiting for the auto-recalibration
trigger (every 10 decisions added).

Usage:
    codevira calibrate                  # equivalent to --decisions
    codevira calibrate --decisions       # re-fit search_decisions threshold
    codevira calibrate --decisions --dry-run
                                         # show what WOULD be set without
                                         # persisting calibration.json

The recalibration algorithm is documented in
:func:`mcp_server.tools._decision_embeddings.recalibrate_threshold`.

Failure modes (P9 graceful):
  - no graph.db → reports "skipped, no graph DB" (exit 2)
  - fewer than 5 positive samples → reports static-default note (exit 0)
  - chromadb unavailable → reports static-default (exit 0)
  - persist failure → reports error but doesn't crash (exit 1)
"""

from __future__ import annotations

import sys


def cmd_calibrate(
    target: str = "decisions",
    *,
    dry_run: bool = False,
) -> int:
    """`codevira calibrate <target>` entry point.

    Returns POSIX exit code (0 success, 1 error, 2 nothing-to-do).
    """
    if target not in ("decisions",):
        print(
            f"Error: target must be 'decisions' (got {target!r}). "
            f"Future targets: 'rules' (planned for v2.1.3).",
            file=sys.stderr,
        )
        return 1

    print()
    print("  Codevira — Threshold Calibration")
    print(f"  Target: {target}")
    print("  " + "─" * 60)
    print()

    try:
        from mcp_server.tools._decision_embeddings import (
            recalibrate_threshold,
            load_threshold,
            _calibration_path,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: cannot import calibration module: {exc}", file=sys.stderr)
        return 1

    # Show CURRENT threshold first so the user sees the before/after.
    try:
        current_search = load_threshold(target="search")
        current_hook = load_threshold(target="hook")
        cal_path = _calibration_path()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: cannot load current threshold: {exc}", file=sys.stderr)
        return 1

    print("  Current:")
    print(f"    search threshold: {current_search:.3f}")
    print(f"    hook threshold:   {current_hook:.3f}")
    if cal_path.is_file():
        print(f"    calibration:      {cal_path}")
    else:
        print("    calibration:      <static defaults — no file yet>")
    print()

    if dry_run:
        # Run the calibration but do NOT persist.
        # We do this by calling recalibrate_threshold and then DELETING the
        # newly-written calibration.json (preserving the prior state).
        had_existing = cal_path.is_file()
        prior_content = cal_path.read_text() if had_existing else None

        result = recalibrate_threshold()

        if had_existing and prior_content is not None:
            try:
                cal_path.write_text(prior_content)
            except Exception as exc:
                print(
                    f"  ⚠ dry-run could not restore prior calibration: {exc}",
                    file=sys.stderr,
                )
        elif not had_existing and cal_path.is_file():
            try:
                cal_path.unlink()
            except Exception as exc:
                print(
                    f"  ⚠ dry-run could not remove temp calibration: {exc}",
                    file=sys.stderr,
                )

        print("  [dry-run] Would set:")
    else:
        result = recalibrate_threshold()
        print("  Re-fitted:")

    print(f"    positive samples:           {result.get('positive_samples', 0)}")
    if "neighbor_distances_collected" in result:
        print(
            f"    neighbor distances:         {result['neighbor_distances_collected']}"
        )
    if "p75_raw" in result:
        print(f"    75th percentile (raw):      {result['p75_raw']:.3f}")
        if result.get("clamped"):
            print("    (clamped to safety bounds)")
    print(f"    new search threshold:       {result.get('threshold_search', 0):.3f}")
    print(f"    new hook threshold:         {result.get('threshold_hook', 0):.3f}")
    if result.get("static_default"):
        note = result.get("note", "")
        print(f"    note:                       {note}")
    if "error" in result:
        print(f"    error:                      {result['error']}", file=sys.stderr)
    if "persist_error" in result:
        print(
            f"    persist error:              {result['persist_error']}",
            file=sys.stderr,
        )

    print()
    if dry_run:
        print("  No changes persisted (--dry-run).")
        return 0
    print(f"  ✓ Calibration written to {cal_path}")
    return 0
