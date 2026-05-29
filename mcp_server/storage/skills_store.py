"""
skills_store.py — v3.1.0 M3 Phase 1: the skill library storage layer.

Skills are reusable procedural patterns — "how to do thing X in this
project" — that the agent can record (explicitly via ``record_skill``
or induced via the M5 pipeline) and retrieve when a similar task
recurs. Unlike decisions (facts, "why we chose X") or working memory
(intra-session scratchpad), skills are *procedures* — they encode
"what to do" in markdown.

# Why a separate store

- **Procedural memory** in the cognitive-science taxonomy: distinct
  from episodic (decisions/sessions) and from working memory.
- **Team-shareable**: lives in ``.codevira/skills.jsonl`` (canonical,
  committed) so a teammate's induced skill helps everyone.
- **Reinforcement-aware**: each skill carries success/failure counts
  + a ``consecutive_failures`` watchdog. Stale or failing skills
  archive themselves during ``codevira sync``.
- **Supersession-chained**: a v2 of a skill points to v1 via
  ``supersedes`` / ``superseded_by`` so the audit trail is intact.

# Lifecycle states

  - ``active``     — default. Returned by ``get_skill``.
  - ``archived``   — low-value (5+ consecutive failures or
                     ``unused_days ≥ 90``, configurable). Not returned
                     by default; visible via ``list_skills(status="archived")``.
                     ``apply_outcome(skill_id, success=True)`` revives
                     it (resets ``consecutive_failures``, returns to
                     ``active``).
  - ``superseded`` — replaced by a successor. Carries
                     ``superseded_by``. Final state.

Skills with ``do_not_revert=True`` are EXEMPT from the auto-archive
sweep (mirrors decisions' do_not_revert semantics).

# Schema

::

    {
      "id":                  "K000001",
      "ts":                  "2026-05-28T10:00:00+00:00",
      "name":                "git-rebase-workflow",
      "summary":             "One-line: how we rebase against main in this repo",
      "procedure":           "<markdown, ≤ 2 KB>",
      "procedure_token_estimate": 0,
      "triggers": {
          "tags":            ["git", "rebase"],
          "file_patterns":   ["*.py", "Makefile"],
      },
      "source":              "explicit" | "induced",
      "source_session_ids":  [],
      "success_count":       0,
      "failure_count":       0,
      "consecutive_failures": 0,
      "last_used_at":        null,
      "unused_days":         0,
      "status":              "active" | "archived" | "superseded",
      "supersedes":          null,
      "superseded_by":       null,
      "do_not_revert":       false,
      "origin":              {ide, agent_model, host_hash, ts},
      "_schema_v":           1,
    }

# Amendment overlay

Mutations (mark_used / set_flag / mark_archived / supersede) append
amendment rows that share the base id. ``jsonl_store.read_merged``
folds them into the canonical view at read time. Underscored fields
(``_amendment_to_id``) do not overlay (matches decisions convention).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from mcp_server.storage import (
    fts5_index,
    jsonl_store,
    origin as origin_module,
    paths,
    sanitize,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

SCHEMA_V = 1

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
STATUS_SUPERSEDED = "superseded"
_VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_SUPERSEDED})

SOURCE_EXPLICIT = "explicit"
SOURCE_INDUCED = "induced"
_VALID_SOURCES = frozenset({SOURCE_EXPLICIT, SOURCE_INDUCED})

# Caps
_PROCEDURE_MAX_BYTES = 2048
_SUMMARY_MAX_BYTES = 256

# Auto-archive thresholds (configurable via .codevira/config.yaml in
# a later phase; defaults here are the plan's stated values).
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_UNUSED_ARCHIVE_DAYS = 90


# ──────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────


def record(
    name: str,
    procedure: str,
    *,
    summary: str | None = None,
    triggers: dict[str, list[str]] | None = None,
    source: str = SOURCE_EXPLICIT,
    source_session_ids: list[str] | None = None,
    do_not_revert: bool = False,
    origin_override: dict | None = None,
) -> str:
    """Append a new skill; return the generated K-id.

    Inputs are validated up front so the disk store never sees a
    malformed record.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("skills_store.record: name must be a non-empty string")
    if not isinstance(procedure, str) or not procedure.strip():
        raise ValueError("skills_store.record: procedure must be a non-empty string")
    procedure = procedure.strip()
    # Byte cap is checked on RAW input — otherwise a caller could bypass
    # the cap by tucking a giant blob between the sanitizer's secret
    # patterns (long b64 collapses to "<redacted:long-b64>").
    if len(procedure.encode("utf-8")) > _PROCEDURE_MAX_BYTES:
        raise ValueError(
            f"skills_store.record: procedure exceeds {_PROCEDURE_MAX_BYTES} "
            f"byte cap ({len(procedure.encode('utf-8'))} bytes given)"
        )
    # v3.1.x bug fix: scrub api-keys / Bearer / passwords / AWS AKIA /
    # long hex / long base64 BEFORE persisting. Without this, an agent
    # that pastes a stack trace or curl example into a procedure leaks
    # the secret into skills.jsonl, the FTS5 index, and any promoted
    # playbook markdown — all of which are committed surfaces.
    procedure = sanitize.scrub_sensitive(procedure)
    if summary is not None:
        if not isinstance(summary, str):
            raise ValueError("skills_store.record: summary must be a string or None")
        if len(summary.encode("utf-8")) > _SUMMARY_MAX_BYTES:
            raise ValueError(
                f"skills_store.record: summary exceeds {_SUMMARY_MAX_BYTES} byte cap"
            )
        summary = sanitize.scrub_sensitive(summary)
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"skills_store.record: source must be one of {sorted(_VALID_SOURCES)}; "
            f"got {source!r}"
        )

    # Triggers: normalize tags to lowercase + sort (mirrors decisions
    # convention); file_patterns kept verbatim (they're already
    # case-significant globs).
    # v3.1.x bug fix: triggers.tags MUST be a list (or None). A bare
    # string would silently iterate as characters and produce
    # {'g','i','t'}, so reject with ValueError.
    norm_tags: list[str] = []
    file_patterns: list[str] = []
    if triggers:
        raw_tags = triggers.get("tags")
        if raw_tags is None:
            raw_tags = []
        elif isinstance(raw_tags, str):
            raise ValueError(
                "skills_store.record: triggers.tags must be a list, not a str "
                f"(got {raw_tags!r}); pass [{raw_tags!r}] to add it as a single tag."
            )
        elif not isinstance(raw_tags, (list, tuple, set)):
            raise ValueError(
                f"skills_store.record: triggers.tags must be a list "
                f"(got {type(raw_tags).__name__})"
            )
        norm_tags = sorted({str(t).strip().lower() for t in raw_tags if str(t).strip()})
        raw_patterns = triggers.get("file_patterns") or []
        file_patterns = [str(p) for p in raw_patterns if isinstance(p, str)]

    paths.ensure_dirs()

    # Lazy import: token_estimator is optional infrastructure (heavy
    # tokenizer); failure here doesn't block the write.
    estimate = _safe_estimate_tokens(procedure)

    base_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": name.strip(),
        "summary": (summary or "").strip() or None,
        "procedure": procedure,
        "procedure_token_estimate": estimate,
        "triggers": {"tags": norm_tags, "file_patterns": file_patterns},
        "source": source,
        "source_session_ids": list(source_session_ids or []),
        "success_count": 0,
        "failure_count": 0,
        "consecutive_failures": 0,
        "last_used_at": None,
        "unused_days": 0,
        "status": STATUS_ACTIVE,
        "supersedes": None,
        "superseded_by": None,
        "do_not_revert": bool(do_not_revert),
        "origin": origin_override or origin_module.current_origin(),
        "_schema_v": SCHEMA_V,
    }

    skill_id = jsonl_store.append_with_generated_id(
        paths.skills_path(), base_record, prefix="K", width=6
    )
    base_record["id"] = skill_id

    # v3.1.0 M3 Phase 2: incrementally update the FTS5 skill_fts index
    # so searches don't wait for a sync. Best-effort (P9 — never fail
    # the write on a cache miss).
    try:
        fts5_index.add_skill(paths.fts5_path(), base_record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("skills_store.record: FTS5 add_skill failed: %s", exc)

    return skill_id


def mark_used(skill_id: str, *, success: bool) -> dict[str, Any]:
    """Apply one outcome to a skill — success or failure.

    Increments the relevant counter via amendment. On success, resets
    ``consecutive_failures`` to 0 AND revives an archived skill (back to
    ``status="active"``). On failure, increments ``consecutive_failures``;
    if it crosses ``DEFAULT_MAX_CONSECUTIVE_FAILURES`` AND the skill is
    not ``do_not_revert``, auto-archives.

    Returns the new (merged) view of the skill.
    """
    existing = get(skill_id)
    if existing is None:
        return {"success": False, "error": f"skill {skill_id} not found"}

    new_success_count = int(existing.get("success_count", 0))
    new_failure_count = int(existing.get("failure_count", 0))
    new_consecutive = int(existing.get("consecutive_failures", 0))
    new_status = existing.get("status", STATUS_ACTIVE)
    revived = False

    if success:
        new_success_count += 1
        new_consecutive = 0
        # Revive an archived skill if a fresh success comes in.
        if new_status == STATUS_ARCHIVED:
            new_status = STATUS_ACTIVE
            revived = True
    else:
        new_failure_count += 1
        new_consecutive += 1
        # Auto-archive on threshold unless do_not_revert is set.
        if (
            new_status == STATUS_ACTIVE
            and not existing.get("do_not_revert")
            and new_consecutive >= DEFAULT_MAX_CONSECUTIVE_FAILURES
        ):
            new_status = STATUS_ARCHIVED

    amendment = {
        "id": skill_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": skill_id,
        "success_count": new_success_count,
        "failure_count": new_failure_count,
        "consecutive_failures": new_consecutive,
        "last_used_at": datetime.now(timezone.utc).isoformat(),
        "unused_days": 0,
        "status": new_status,
    }
    paths.ensure_dirs()
    jsonl_store.append(paths.skills_path(), amendment)
    return {
        "success": True,
        "skill_id": skill_id,
        "status": new_status,
        "consecutive_failures": new_consecutive,
        "revived": revived,
    }


def set_flag(
    skill_id: str,
    *,
    do_not_revert: bool | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Lightweight in-place flag/tag updates via an amendment line.

    Mirrors ``decisions_store.set_flag`` semantics. Either or both of
    ``do_not_revert`` / ``tags`` may be supplied. No-op if neither is.
    """
    existing = get(skill_id)
    if existing is None:
        return {"success": False, "error": f"skill {skill_id} not found"}

    updates: dict[str, Any] = {}
    if do_not_revert is not None:
        updates["do_not_revert"] = bool(do_not_revert)
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return {"success": False, "error": "tags must be a list[str]"}
        # Mirror decisions normalization.
        norm_tags = sorted({str(t).strip().lower() for t in tags if str(t).strip()})
        # The triggers dict carries tags + file_patterns; merge.
        merged_triggers = dict(existing.get("triggers") or {})
        merged_triggers["tags"] = norm_tags
        updates["triggers"] = merged_triggers

    if not updates:
        return {"success": True, "skill_id": skill_id, "updates": {}}

    amendment = {
        "id": skill_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": skill_id,
        **updates,
    }
    paths.ensure_dirs()
    jsonl_store.append(paths.skills_path(), amendment)
    return {"success": True, "skill_id": skill_id, "updates": updates}


def mark_archived(skill_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """Manually archive a skill. Useful when the user knows a skill is
    obsolete but the auto-sweep hasn't fired yet.

    Refuses to archive ``do_not_revert=True`` skills — those represent
    canonical doctrine.
    """
    existing = get(skill_id)
    if existing is None:
        return {"success": False, "error": f"skill {skill_id} not found"}
    if existing.get("do_not_revert"):
        return {
            "success": False,
            "error": (
                f"skill {skill_id} is do_not_revert=true; refusing to archive. "
                f"Clear the flag first via set_flag(skill_id, do_not_revert=False)."
            ),
        }
    amendment = {
        "id": skill_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": skill_id,
        "status": STATUS_ARCHIVED,
    }
    if reason:
        amendment["_archive_reason"] = reason
    paths.ensure_dirs()
    jsonl_store.append(paths.skills_path(), amendment)
    return {"success": True, "skill_id": skill_id, "status": STATUS_ARCHIVED}


def supersede(
    old_id: str,
    *,
    name: str,
    procedure: str,
    summary: str | None = None,
    triggers: dict[str, list[str]] | None = None,
    reason: str = "",
    do_not_revert: bool = False,
) -> dict[str, Any]:
    """Append a new skill that supersedes ``old_id`` and amendment-mark
    the old one as ``superseded`` with a backref.

    The new skill inherits the old skill's tags/file_patterns when
    ``triggers`` is not supplied (matches decisions.supersede pattern
    for file_path / tags inheritance).
    """
    old = get(old_id)
    if old is None:
        return {"success": False, "error": f"skill {old_id} not found"}

    inherited_triggers = triggers or {
        "tags": (old.get("triggers") or {}).get("tags") or [],
        "file_patterns": (old.get("triggers") or {}).get("file_patterns") or [],
    }

    new_summary = summary if summary is not None else old.get("summary")

    new_id = record(
        name=name,
        procedure=procedure,
        summary=new_summary,
        triggers=inherited_triggers,
        source=SOURCE_EXPLICIT,
        do_not_revert=do_not_revert,
    )

    # Amend the old skill to mark it superseded.
    paths.ensure_dirs()
    amendment = {
        "id": old_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": old_id,
        "status": STATUS_SUPERSEDED,
        "superseded_by": new_id,
    }
    if reason:
        amendment["_supersede_reason"] = reason
    jsonl_store.append(paths.skills_path(), amendment)

    # Also amend the NEW skill to record the back-reference.
    back_ref = {
        "id": new_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "_amendment_to_id": new_id,
        "supersedes": old_id,
    }
    jsonl_store.append(paths.skills_path(), back_ref)
    return {"success": True, "old_id": old_id, "new_id": new_id}


# ──────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────


def get(skill_id: str) -> dict[str, Any] | None:
    """Return the merged record for ``skill_id``, or None."""
    for rec in jsonl_store.read_merged(paths.skills_path()):
        if str(rec.get("id")) == skill_id:
            return rec
    return None


def list_all(
    *,
    status: str | None = STATUS_ACTIVE,
    source: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List skills filtered by status / source / tags intersection.

    ``status=None`` returns every state. ``status=STATUS_ACTIVE``
    (default) returns only the actively-used set — the daily-driver
    surface. Tags filter is intersection: a skill matches only if it
    has ALL the requested tags.
    """
    merged = jsonl_store.read_merged(paths.skills_path())
    norm_tags_filter = (
        {str(t).strip().lower() for t in tags if str(t).strip()} if tags else None
    )

    out: list[dict[str, Any]] = []
    for r in merged:
        if status is not None and r.get("status", STATUS_ACTIVE) != status:
            continue
        if source is not None and r.get("source") != source:
            continue
        if norm_tags_filter:
            rec_tags = set((r.get("triggers") or {}).get("tags") or [])
            if not norm_tags_filter.issubset(rec_tags):
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# ──────────────────────────────────────────────────────────────────────
# Search (composite ranking)
# ──────────────────────────────────────────────────────────────────────


# Default tuning per the plan: 0.5 * BM25_norm + 0.3 * tag_jaccard +
# 0.2 * recency_decay (τ_days = 30). Overridable per-call so a
# config flag in M9 can tune without code changes.
DEFAULT_RANKING_WEIGHTS = {"bm25": 0.5, "tag": 0.3, "recency": 0.2}
_RECENCY_TAU_DAYS = 30.0
# How many FTS5 candidates to pull per requested top-K. Wider net so
# the tag+recency rerank can promote a strong candidate that BM25
# ranked just below the cut.
_CANDIDATE_OVERSAMPLE = 4


def search(
    query: str,
    *,
    top_k: int = 5,
    file_path: str | None = None,
    ranking_weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rank active skills against a query via the composite formula.

    ::

        score = 0.5 × BM25_norm + 0.3 × tag_jaccard + 0.2 × recency_decay

    Where:
      - ``BM25_norm = -bm25_raw / max(-bm25_raw)`` over the candidate
        set (FTS5 BM25 is a negative distance — lower = better; we
        flip the sign before normalizing).
      - ``tag_jaccard = |query_tokens ∩ skill_tags| / |union|`` over
        the query tokens (lowercased, ≥ 3 chars) and the skill's
        trigger tags.
      - ``recency_decay = exp(-Δdays_since_last_used / 30)`` where
        ``last_used_at`` (or ``ts`` if never used) is the reference.

    ``file_path`` (optional): filters out skills whose ``triggers.
    file_patterns`` don't match the path (fnmatch). Empty / absent
    patterns means the skill matches anything (no filter).

    Superseded / archived skills are excluded — search is the
    everyday surface, daily-driver only.

    Returns each result with ``score_breakdown`` so callers can
    inspect the composition (useful for debugging weight tuning).
    """
    weights = ranking_weights or DEFAULT_RANKING_WEIGHTS
    now_dt = now or datetime.now(timezone.utc)

    if not query or not query.strip():
        return []

    # Lazy rebuild — the FTS5 cache is stateless; rebuilding when
    # stale keeps writes cheap and reads correct.
    if fts5_index.skill_staleness_check(paths.skills_path(), paths.fts5_path()):
        try:
            fts5_index.rebuild_skills_from_jsonl(paths.skills_path(), paths.fts5_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("skills_store.search: FTS5 rebuild failed: %s", exc)

    hits = fts5_index.search_skills(
        paths.fts5_path(), query, limit=max(top_k * _CANDIDATE_OVERSAMPLE, top_k)
    )
    if not hits:
        return []

    # Flip BM25 (negative → positive) for normalization.
    raw_pos = [-h["score"] for h in hits]
    max_pos = max(raw_pos) if raw_pos else 1.0
    if max_pos <= 0:
        max_pos = 1.0  # all-zero corpus; avoid div-by-zero

    query_tokens = _tokenize_for_jaccard(query)

    merged = jsonl_store.read_merged(paths.skills_path())
    by_id = {str(s.get("id")): s for s in merged}

    out: list[dict[str, Any]] = []
    for hit, pos in zip(hits, raw_pos, strict=False):
        skill = by_id.get(hit["skill_id"])
        if skill is None:
            continue
        if skill.get("status", STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        if file_path is not None and not _matches_file_pattern(skill, file_path):
            continue

        bm25_norm = pos / max_pos
        tag_jaccard = _tag_jaccard(query_tokens, skill)
        recency = _recency_decay(skill, now=now_dt)
        composite = (
            weights.get("bm25", 0.0) * bm25_norm
            + weights.get("tag", 0.0) * tag_jaccard
            + weights.get("recency", 0.0) * recency
        )

        out.append(
            {
                "id": skill.get("id"),
                "name": skill.get("name"),
                "summary": skill.get("summary"),
                "procedure": skill.get("procedure"),
                "triggers": skill.get("triggers"),
                "status": skill.get("status"),
                "do_not_revert": skill.get("do_not_revert"),
                "score": round(composite, 4),
                "score_breakdown": {
                    "bm25_norm": round(bm25_norm, 4),
                    "tag_jaccard": round(tag_jaccard, 4),
                    "recency_decay": round(recency, 4),
                },
                "snippet": hit.get("snippet"),
            }
        )

    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:top_k]


# ──────────────────────────────────────────────────────────────────────
# Maintenance
# ──────────────────────────────────────────────────────────────────────


def decay_sweep(
    *,
    now: datetime | None = None,
    unused_archive_days: int = DEFAULT_UNUSED_ARCHIVE_DAYS,
) -> dict[str, Any]:
    """Auto-archive active skills that haven't been used in
    ``unused_archive_days``. Called by ``codevira sync``. Returns
    ``{archived, scanned, dry_run=False}``.

    Skills with ``do_not_revert=True`` are exempt (mirrors decisions).
    Skills with no ``last_used_at`` are considered "never used"; their
    age is computed against ``ts`` (creation).
    """
    now_dt = now or datetime.now(timezone.utc)
    cutoff_seconds = unused_archive_days * 86400

    skills = jsonl_store.read_merged(paths.skills_path())
    archived: list[str] = []
    scanned = 0

    for s in skills:
        scanned += 1
        if s.get("status", STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        if s.get("do_not_revert"):
            continue
        last_used = s.get("last_used_at") or s.get("ts")
        if not isinstance(last_used, str):
            continue
        try:
            ref = datetime.fromisoformat(last_used)
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if (now_dt - ref).total_seconds() >= cutoff_seconds:
            res = mark_archived(
                str(s["id"]), reason=f"unused for ≥ {unused_archive_days} days"
            )
            if res.get("success"):
                archived.append(str(s["id"]))

    return {"archived": archived, "archived_count": len(archived), "scanned": scanned}


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _tokenize_for_jaccard(query: str) -> set[str]:
    """Lowercased tokens ≥ 3 chars from the query, for tag-Jaccard
    overlap. Punctuation is stripped to match the FTS5 sanitizer's
    behavior loosely (not exactly — this is a relevance heuristic).
    """
    out: set[str] = set()
    for raw in query.split():
        t = raw.strip("\"'.,;:!?()[]{}").lower()
        if len(t) >= 3:
            out.add(t)
    return out


def _tag_jaccard(query_tokens: set[str], skill: dict[str, Any]) -> float:
    """``|A ∩ B| / |A ∪ B|`` over query tokens and skill tags.

    Returns 0.0 if either set is empty (no overlap signal available).
    """
    skill_tags = set((skill.get("triggers") or {}).get("tags") or [])
    if not query_tokens or not skill_tags:
        return 0.0
    inter = query_tokens & skill_tags
    union = query_tokens | skill_tags
    if not union:
        return 0.0
    return len(inter) / len(union)


def _recency_decay(
    skill: dict[str, Any],
    *,
    now: datetime,
    tau_days: float = _RECENCY_TAU_DAYS,
) -> float:
    """``exp(-Δdays_since_last_used / τ)`` per the plan.

    ``last_used_at`` is the reference. **Never-used skills score 0**
    — recency is a *usage* signal, not an existence signal. A
    freshly-recorded skill still surfaces via BM25 + tag-Jaccard;
    once it's used at least once, the recency component starts
    contributing.

    Malformed timestamps also score 0 — be conservative.
    """
    last_used = skill.get("last_used_at")
    if not isinstance(last_used, str):
        return 0.0
    try:
        ref = datetime.fromisoformat(last_used)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        delta_days = max(0.0, (now - ref).total_seconds() / 86400.0)
        return math.exp(-delta_days / tau_days)
    except (ValueError, TypeError):
        return 0.0


def _matches_file_pattern(skill: dict[str, Any], file_path: str) -> bool:
    """True if any of the skill's ``triggers.file_patterns`` fnmatches
    ``file_path`` — or if the skill has no patterns (no filter).
    """
    import fnmatch

    patterns = (skill.get("triggers") or {}).get("file_patterns") or []
    if not patterns:
        return True
    return any(fnmatch.fnmatch(file_path, p) for p in patterns)


def _safe_estimate_tokens(text: str) -> int:
    """Best-effort token count via token_estimator. Fallback: ~4
    chars/token rule-of-thumb so the field is always populated.
    """
    try:
        from mcp_server.storage.token_estimator import estimate_tokens

        return int(estimate_tokens(text))
    except Exception:  # noqa: BLE001
        # Conservative 1 token ≈ 4 bytes (UTF-8) estimate.
        return max(1, len(text.encode("utf-8")) // 4)
