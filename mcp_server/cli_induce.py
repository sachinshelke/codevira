"""
cli_induce.py — v3.1.0 M5: ``codevira induce-skills`` CLI.

Walks through ``sessions.jsonl`` looking for productive clusters
(same ``task_type``, similar tag set, ≥80% of decisions classified
``kept``) and proposes induced skills the user can review + commit
to ``skills.jsonl``.

# Pipeline (matches the plan's M5 spec)

  1. Filter to sessions that have ``task_type`` set AND at least one
     decision in ``decision_ids`` whose outcome (per outcomes.jsonl)
     is classified — and ≥80% of those classified outcomes are
     ``kept``.
  2. Group by ``task_type``.
  3. Within each group, cluster sessions by tag-Jaccard ≥ 0.5
     (greedy single-pass agglomeration).
  4. Keep clusters with ≥3 sessions.
  5. Render a candidate skill per cluster:
       name      = "<task_type>: <top-3 tags>"
       procedure = bullet-summary of session.task lines + truncated
                   decision text (deterministic — no LLM in v3.1).
  6. Without ``--apply``: write proposals to
     ``.codevira/induction_proposals.jsonl`` for human review.
  7. With ``--apply``: review interactively unless ``--yes``,
     then ``skills_store.record(source='induced',
     source_session_ids=[...])``.

# Deterministic-only ranking

v3.1.0 induction does NOT call an LLM. Procedure text is rendered
from existing session/decision strings. M5+ (v3.2 opt-in) can
substitute an LLM-rendered procedure behind a feature flag.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import jsonl_store, paths


_KEPT_THRESHOLD = 0.80
_MIN_CLUSTER_SIZE = 3
_TAG_JACCARD_THRESHOLD = 0.5
_PROCEDURE_LINE_CAP = 30  # cap total lines in the rendered procedure
_PROCEDURE_DECISION_TRUNC = 120  # per-decision truncation


def cmd_induce_skills(*, apply: bool = False, yes: bool = False) -> int:
    """Entry point for ``codevira induce-skills``.

    Returns 0 on success (including no candidates found). Non-zero on
    storage / parse errors.
    """
    proposals = _build_proposals()
    if not proposals:
        sys.stdout.write(
            "codevira induce-skills: no induced-skill candidates "
            "found (need ≥3 productive sessions sharing a task_type "
            "and a ≥0.5 tag-Jaccard cluster).\n"
        )
        return 0

    if not apply:
        return _write_proposals(proposals)

    return _apply_proposals(proposals, yes=yes)


# ──────────────────────────────────────────────────────────────────────
# Pipeline stages
# ──────────────────────────────────────────────────────────────────────


def _build_proposals() -> list[dict[str, Any]]:
    """Stage 1-5 of the pipeline. Returns the proposal list (possibly
    empty)."""
    sessions = jsonl_store.read_all(paths.sessions_path())
    if not sessions:
        return []

    # Build the {decision_id → outcome_type} index from outcomes.jsonl.
    outcomes_by_decision: dict[str, str] = {}
    try:
        for row in jsonl_store.read_all(paths.outcomes_path()):
            did = row.get("decision_id")
            otype = row.get("outcome_type")
            if isinstance(did, str) and isinstance(otype, str):
                outcomes_by_decision[did] = otype  # last write wins (newest)
    except Exception:  # noqa: BLE001
        pass

    # Build the {decision_id → decision_row} index from decisions.jsonl
    # via the merged-amendment view so we see the latest decision text.
    decisions_by_id: dict[str, dict[str, Any]] = {}
    try:
        for r in jsonl_store.read_merged(paths.decisions_path()):
            did = r.get("id")
            if isinstance(did, str):
                decisions_by_id[did] = r
    except Exception:  # noqa: BLE001
        pass

    # Stage 1: filter productive sessions.
    productive: list[dict[str, Any]] = []
    for s in sessions:
        if s.get("_amendment_to_id"):
            continue  # session-log amendments don't drive induction
        task_type = s.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            continue
        decision_ids = s.get("decision_ids") or []
        if not isinstance(decision_ids, list) or not decision_ids:
            continue
        kept = 0
        classified = 0
        for did in decision_ids:
            if not isinstance(did, str):
                continue
            outcome = outcomes_by_decision.get(did)
            if outcome is None:
                continue
            classified += 1
            if outcome == "kept":
                kept += 1
        if classified == 0:
            continue
        if kept / classified < _KEPT_THRESHOLD:
            continue
        productive.append(s)

    if not productive:
        return []

    # Stage 2: group by task_type.
    by_task_type: dict[str, list[dict[str, Any]]] = {}
    for s in productive:
        by_task_type.setdefault(s["task_type"], []).append(s)

    # Stage 3-4: cluster + filter.
    proposals: list[dict[str, Any]] = []
    for task_type, group in by_task_type.items():
        sessions_with_tags: list[tuple[dict[str, Any], set[str]]] = []
        for s in group:
            tags: set[str] = set()
            for did in s.get("decision_ids") or []:
                if not isinstance(did, str):
                    continue
                d = decisions_by_id.get(did)
                if d is None:
                    continue
                for t in d.get("tags") or []:
                    if isinstance(t, str) and t:
                        tags.add(t)
            sessions_with_tags.append((s, tags))

        clusters: list[dict[str, Any]] = []
        for s, tags in sessions_with_tags:
            matched = False
            for cluster in clusters:
                if _jaccard(tags, cluster["tags"]) >= _TAG_JACCARD_THRESHOLD:
                    cluster["sessions"].append(s)
                    cluster["tags"] = cluster["tags"] | tags
                    matched = True
                    break
            if not matched:
                clusters.append(
                    {"sessions": [s], "tags": set(tags), "task_type": task_type}
                )

        for cluster in clusters:
            if len(cluster["sessions"]) < _MIN_CLUSTER_SIZE:
                continue
            proposals.append(_render_proposal(cluster, decisions_by_id=decisions_by_id))

    return proposals


def _render_proposal(
    cluster: dict[str, Any], *, decisions_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Stage 5: deterministic procedure rendering."""
    task_type = cluster["task_type"]
    tags = sorted(cluster["tags"])
    top_tags = tags[:3]
    name = (
        f"{task_type}: {', '.join(top_tags)}" if top_tags else f"{task_type}: induced"
    )
    lines: list[str] = []
    for s in cluster["sessions"]:
        task_line = (s.get("task") or "").strip()
        if task_line:
            lines.append(f"- {task_line}")
        for did in s.get("decision_ids") or []:
            if not isinstance(did, str):
                continue
            d = decisions_by_id.get(did)
            if d is None:
                continue
            text = (d.get("decision") or "").strip()
            if not text:
                continue
            snippet = (
                text
                if len(text) <= _PROCEDURE_DECISION_TRUNC
                else text[: _PROCEDURE_DECISION_TRUNC - 1] + "…"
            )
            lines.append(f"  • {snippet}")
            if len(lines) >= _PROCEDURE_LINE_CAP:
                break
        if len(lines) >= _PROCEDURE_LINE_CAP:
            break
    procedure = "\n".join(lines).strip() or "(no rendered procedure body)"
    return {
        "name": name,
        "summary": (
            f"Induced from {len(cluster['sessions'])} productive "
            f"{task_type} session(s) sharing tags: {', '.join(top_tags)}"
        ),
        "procedure": procedure,
        "task_type": task_type,
        "tags": tags,
        "source_session_ids": [str(s.get("session_id")) for s in cluster["sessions"]],
        "session_count": len(cluster["sessions"]),
    }


# ──────────────────────────────────────────────────────────────────────
# Dry-run / Apply
# ──────────────────────────────────────────────────────────────────────


def _write_proposals(proposals: list[dict[str, Any]]) -> int:
    """Stage 6: write proposals to .codevira/induction_proposals.jsonl."""
    paths.ensure_dirs()
    dest = paths.induction_proposals_path()
    try:
        ts = datetime.now(timezone.utc).isoformat()
        for p in proposals:
            jsonl_store.append(
                dest,
                {
                    "ts": ts,
                    "name": p["name"],
                    "summary": p.get("summary"),
                    "procedure": p["procedure"],
                    "task_type": p["task_type"],
                    "tags": p["tags"],
                    "source_session_ids": p["source_session_ids"],
                    "session_count": p["session_count"],
                    "_schema_v": 1,
                },
            )
    except OSError as exc:
        sys.stderr.write(f"codevira induce-skills: could not write proposals: {exc}\n")
        return 1
    sys.stdout.write(
        f"codevira induce-skills: wrote {len(proposals)} proposal(s) to "
        f"{dest}.\n  Review them and re-run with --apply (add --yes for "
        f"non-interactive commit) to record into skills.jsonl.\n"
    )
    return 0


def _apply_proposals(proposals: list[dict[str, Any]], *, yes: bool) -> int:
    """Stage 7: record proposals as induced skills."""
    from mcp_server.storage import skills_store

    paths.ensure_dirs()
    recorded = 0
    skipped = 0

    for p in proposals:
        if not yes:
            sys.stdout.write("\n" + "─" * 70 + "\n")
            sys.stdout.write(f"Proposed skill: {p['name']}\n")
            sys.stdout.write(f"  ({p['summary']})\n\n")
            sys.stdout.write(p["procedure"] + "\n\n")
            sys.stdout.write("Record this skill? [y/N]: ")
            sys.stdout.flush()
            try:
                resp = input().strip().lower()
            except EOFError:
                resp = "n"
            if resp not in ("y", "yes"):
                skipped += 1
                continue

        try:
            kid = skills_store.record(
                name=p["name"],
                procedure=p["procedure"],
                summary=p.get("summary"),
                triggers={"tags": p["tags"], "file_patterns": []},
                source=skills_store.SOURCE_INDUCED,
                source_session_ids=p["source_session_ids"],
            )
            sys.stdout.write(f"  ✓ recorded {kid}\n")
            recorded += 1
        except ValueError as exc:
            sys.stderr.write(f"  ✗ skipped: {exc}\n")
            skipped += 1
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"  ✗ unexpected error: {exc}\n")
            skipped += 1

    sys.stdout.write(
        f"\ncodevira induce-skills: recorded {recorded} / {len(proposals)} "
        f"({skipped} skipped).\n"
    )
    return 0 if recorded > 0 else (1 if proposals else 0)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0  # two empty sets cluster together
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)
