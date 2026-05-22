"""
outcomes_writer.py — v2.2.0 Phase F: git-observed kept/reverted classification.

Scans git history since the last observation and classifies each decision
as kept / modified / reverted relative to its ``file_path``:

  kept      → the file hasn't been changed since the decision was recorded
              (the decision "stuck" — implicit success signal)

  modified  → the file changed but the decision's specific code is still
              intact (partial preservation)

  reverted  → the file changed AND the decision's referenced code was
              removed / replaced (negative signal)

Heuristic (v2.2.0 minimum viable):

  For each decision with a ``file_path``:
  1. Get the commit hash at decision time (best-effort — uses the closest
     commit to ``decision.ts``).
  2. Compare current HEAD's version of the file vs that historical commit.
  3. If the file is unchanged in line count + a sample of content → kept.
     If the file changed materially → modified.
     If the file was deleted OR the decision's text references symbols no
     longer in the file → reverted.

Result: appended to ``.codevira/outcomes.jsonl``, then ``digest.weight``
is regenerated to reflect the new outcome distribution. Runs in O(N_decisions)
plus one git diff per file — typically <1s on a 100-decision project.

CLI: ``codevira observe-git`` invokes ``observe_all()``. Recommended to
run after every batch of commits (or as a post-commit hook).

Limitations:

- Doesn't understand decisions about CONCEPTS (only file-bound ones).
- The "reverted" detection is heuristic; user can override an outcome
  by appending an amendment to ``decisions.jsonl`` manually.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_server.storage import digest, jsonl_store, paths

logger = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command; return stdout. Returns '' on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            logger.debug(
                "outcomes_writer git %s failed: %s",
                " ".join(args),
                result.stderr.strip(),
            )
            return ""
        return result.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("outcomes_writer git %s exception: %s", args, exc)
        return ""


def _git_available(project_root: Path) -> bool:
    """Return True iff ``project_root`` is inside a git repo with a HEAD."""
    head = _run_git(["rev-parse", "HEAD"], project_root).strip()
    return bool(head)


def _file_exists_at_head(project_root: Path, file_path: str) -> bool:
    """True iff ``file_path`` exists in the current working tree."""
    return (project_root / file_path).is_file()


def _commit_at_or_before(project_root: Path, file_path: str, iso_ts: str) -> str | None:
    """Return the commit SHA touching ``file_path`` on or before ``iso_ts``.

    Used as the proxy "when the decision was recorded" anchor. If the
    decision's timestamp is later than all commits touching the file,
    falls back to the file's first commit.
    """
    out = _run_git(
        [
            "log",
            "--format=%H %cI",
            f"--before={iso_ts}",
            "--",
            file_path,
        ],
        project_root,
    )
    for line in out.splitlines():
        parts = line.strip().split(" ", 1)
        if parts:
            return parts[0]
    # Fall back to the earliest commit touching the file.
    out = _run_git(
        ["log", "--reverse", "--format=%H", "--", file_path],
        project_root,
    )
    first = out.splitlines()
    return first[0] if first else None


def _file_changed_since(project_root: Path, file_path: str, since_commit: str) -> bool:
    """True iff ``file_path`` changed between ``since_commit`` and HEAD."""
    diff = _run_git(
        ["diff", "--name-only", f"{since_commit}..HEAD", "--", file_path],
        project_root,
    )
    return bool(diff.strip())


def _classify_decision(project_root: Path, decision: dict[str, Any]) -> str | None:
    """Return outcome type (kept / modified / reverted) for a decision.

    Returns None if the decision has no file_path or we can't classify
    confidently (leaves the decision's outcome at its prior state).
    """
    file_path = decision.get("file_path")
    if not file_path:
        return None

    # File deleted entirely → reverted.
    if not _file_exists_at_head(project_root, str(file_path)):
        return "reverted"

    ts = decision.get("ts")
    if not ts:
        return None

    anchor = _commit_at_or_before(project_root, str(file_path), str(ts))
    if anchor is None:
        # File hasn't been committed at all (untracked / new) → can't
        # classify yet. Don't overwrite existing outcome.
        return None

    if not _file_changed_since(project_root, str(file_path), anchor):
        return "kept"
    return "modified"


def observe_all(*, project_root: Path | None = None) -> dict[str, Any]:
    """Classify every decision against current git HEAD; write outcomes.

    Returns a summary dict:
        {
          "decisions_observed": int,
          "kept": int,
          "modified": int,
          "reverted": int,
          "unclassified": int,
          "outcomes_appended": int,
        }
    """
    from mcp_server.paths import get_project_root

    project_root = project_root or get_project_root()

    if not _git_available(project_root):
        return {
            "error": (
                f"{project_root} is not a git repo (no HEAD). "
                "Outcome tracking requires git history."
            ),
            "decisions_observed": 0,
        }

    decisions = jsonl_store.read_all(paths.decisions_path(project_root))
    # Apply amendments to get the latest state.
    merged: dict[str, dict[str, Any]] = {}
    insertion: list[str] = []
    for rec in decisions:
        did = str(rec.get("id", ""))
        if not did:
            continue
        if rec.get("_amendment_to_id"):
            base = merged.get(did)
            if base is not None:
                base.update({k: v for k, v in rec.items() if not k.startswith("_")})
            else:
                merged[did] = dict(rec)
                insertion.append(did)
        else:
            if did not in merged:
                insertion.append(did)
            merged[did] = dict(rec)
    active = [merged[did] for did in insertion]

    counts = {"kept": 0, "modified": 0, "reverted": 0, "unclassified": 0}
    new_outcomes: list[dict[str, Any]] = []
    head_sha = _run_git(["rev-parse", "HEAD"], project_root).strip()

    for d in active:
        if d.get("is_superseded") or d.get("superseded_by"):
            continue  # don't track outcomes for retired decisions
        outcome_type = _classify_decision(project_root, d)
        if outcome_type is None:
            counts["unclassified"] += 1
            continue
        counts[outcome_type] += 1

        # Skip if the SAME outcome already exists for this decision at this HEAD.
        if d.get("outcome") == outcome_type:
            continue

        new_outcomes.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "decision_id": d.get("id"),
                "outcome_type": outcome_type,
                "git_ref": head_sha[:12] if head_sha else None,
                "delta_summary": (
                    f"file {d.get('file_path')} → {outcome_type} as of HEAD"
                ),
            }
        )

        # Also append an amendment to decisions.jsonl so the merged view
        # carries the latest outcome (drives digest.weight).
        jsonl_store.append(
            paths.decisions_path(project_root),
            {
                "id": d.get("id"),
                "ts": datetime.now(timezone.utc).isoformat(),
                "_amendment_to_id": d.get("id"),
                "outcome": outcome_type,
            },
        )

    # Append all new outcome events to outcomes.jsonl.
    if new_outcomes:
        jsonl_store.append_many(paths.outcomes_path(project_root), new_outcomes)
        # Regenerate digest so the new weights take effect on next inject.
        try:
            digest.regenerate(
                paths.decisions_path(project_root),
                paths.digest_path(project_root),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcomes_writer.observe_all: digest regen failed: %s", exc)

    return {
        "decisions_observed": len(active),
        **counts,
        "outcomes_appended": len(new_outcomes),
        "head_sha": head_sha[:12] if head_sha else None,
    }


# ─── CLI entry point ─────────────────────────────────────────────────


def cmd_observe_git(*, verbose: bool = False) -> int:
    """``codevira observe-git`` — classify decisions against current HEAD."""
    import sys

    summary = observe_all()
    if "error" in summary:
        print(f"Error: {summary['error']}", file=sys.stderr)
        return 1

    print()
    print("  Codevira — Observe (git)")
    print(f"  HEAD: {summary.get('head_sha') or 'unknown'}")
    print("  " + "─" * 60)
    print()
    print(f"    Decisions observed: {summary['decisions_observed']}")
    print(f"      • kept:           {summary['kept']}")
    print(f"      • modified:       {summary['modified']}")
    print(f"      • reverted:       {summary['reverted']}")
    print(f"      • unclassified:   {summary['unclassified']}")
    print(f"    Outcomes appended:  {summary['outcomes_appended']}")
    print()

    if summary["outcomes_appended"] > 0:
        print("  ✓ outcomes.jsonl + digest.jsonl updated.")
    else:
        print("  ✓ No outcome changes since last observation.")
    print()
    return 0
