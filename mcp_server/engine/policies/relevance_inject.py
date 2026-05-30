"""
relevance_inject.py — v2.2.0 token-budget-aware UserPromptSubmit policy.

Replaces ``cross_session.py``. Same intent (surface relevant prior
decisions when the user submits a prompt) but with a hard token budget
and ZERO-token-on-off-topic guarantee — the v2.2.0 goal that made
"AGENTS.md as god-file" untenable.

Scoring:

  total_score(decision) = (tag_score + file_score + fts_score) * outcome_weight

  tag_score:    +0.4 per matching tag (extracted from prompt text)
  file_score:   +0.4 per file path match (substring against prompt)
  fts_score:    BM25 rank from FTS5 → normalized to [0, 0.2]
  outcome_weight: digest.weight ∈ [0, 1]
                   (kept=1.0, modified=0.6, reverted=0.2, archived=0.0,
                    no-outcome=0.5)

Cutoff:

- Off-topic prompts → 0 tokens injected (no `additionalContext` at all)
- On-topic prompts → top-K decisions ranked by score, capped at
  inject_max_decisions (default 3), then trimmed line-by-line until
  the rendered block fits under inject_max_tokens (default 600)

Cache-stable output:

- Decisions are emitted sorted by ID (deterministic)
- No timestamps in the output text (cache-friendly across runs)
- Block opens with ``<codevira-context cache_key="<sha256>">`` so the
  Anthropic prompt cache can detect identical injections

Config (per-project ``.codevira/config.yaml``):

    inject_max_decisions: 3        # cap on decisions in the block
    inject_max_tokens: 600         # hard budget (cuts decisions to fit)
    relevance_min_score: 0.10      # decisions below this don't inject

Env var overrides (machine-wide):

    CODEVIRA_INJECT_MODE         = "off" | "inject" (default "inject")
    CODEVIRA_INJECT_MAX_DECISIONS = int (1..20)
    CODEVIRA_INJECT_MAX_TOKENS    = int (50..5000)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import yaml

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext

logger = logging.getLogger(__name__)


# ─── Defaults (matched to v2.2.0 plan budgets) ─────────────────────────

_DEFAULT_MODE = "inject"
_DEFAULT_MAX_DECISIONS = 3
_DEFAULT_MAX_TOKENS = 600
# v3.1.x: raised from 0.10 → 0.25. Per-component weights are
# TAG=0.4, FILE=0.4, FTS=0.2; a single tag match × default outcome
# weight (0.5) = 0.20, which used to clear the old 0.10 threshold.
# That meant any decision tagged with a common token (e.g. "engine",
# "policy") surfaced on every prompt that mentioned the token, even
# tangentially. 0.25 requires either (a) two source matches OR
# (b) a single source match with a strong outcome weight (≥0.7).
# Override via .codevira/config.yaml: memory.relevance_min_score.
# Locked by D000010 (procedural: must run make test-e2e BEFORE commit).
_DEFAULT_MIN_SCORE = 0.25
_MIN_PROMPT_CHARS = 10  # ignore tiny prompts (e.g. "ok", "thanks")
_MODES = ("off", "inject")

# Per-component weights for the merged relevance score.
_TAG_WEIGHT = 0.4
_FILE_WEIGHT = 0.4
_FTS_WEIGHT = 0.2

# Tokenization for prompt → candidate tags + words.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
# File-path-like substring detection in prompt text.
_PATH_RE = re.compile(r"[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9]+|/)")


class RelevanceInject(Policy):
    """Surface relevant prior decisions on prompt submit, under a hard
    token budget."""

    name = "relevance_inject"
    handles = (EventType.USER_PROMPT_SUBMIT,)
    enabled_by_default = True
    # Same priority bucket as the old cross_session policy.
    priority = 30

    # ─── Config resolution ─────────────────────────────────────────────

    def _config(self) -> dict[str, Any]:
        """Env vars > project config.yaml > defaults."""
        mode_raw = os.environ.get("CODEVIRA_INJECT_MODE", "").strip().lower()
        max_decisions_raw = os.environ.get("CODEVIRA_INJECT_MAX_DECISIONS", "")
        max_tokens_raw = os.environ.get("CODEVIRA_INJECT_MAX_TOKENS", "")
        min_score_raw = os.environ.get("CODEVIRA_INJECT_MIN_SCORE", "")

        # Fall back to .codevira/config.yaml
        if not mode_raw or not max_decisions_raw or not max_tokens_raw:
            try:
                from mcp_server.storage import paths

                cfg_path = paths.config_path()
                if cfg_path.is_file():
                    cfg = yaml.safe_load(cfg_path.read_text()) or {}
                    if not mode_raw and "inject_mode" in cfg:
                        mode_raw = str(cfg["inject_mode"]).strip().lower()
                    if not max_decisions_raw and "inject_max_decisions" in cfg:
                        max_decisions_raw = str(cfg["inject_max_decisions"])
                    if not max_tokens_raw and "inject_max_tokens" in cfg:
                        max_tokens_raw = str(cfg["inject_max_tokens"])
                    if not min_score_raw and "relevance_min_score" in cfg:
                        min_score_raw = str(cfg["relevance_min_score"])
            except Exception:
                pass

        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE

        max_decisions = _DEFAULT_MAX_DECISIONS
        if max_decisions_raw:
            try:
                v = int(max_decisions_raw)
                if 1 <= v <= 20:
                    max_decisions = v
            except (ValueError, TypeError):
                pass

        max_tokens = _DEFAULT_MAX_TOKENS
        if max_tokens_raw:
            try:
                v = int(max_tokens_raw)
                if 50 <= v <= 5000:
                    max_tokens = v
            except (ValueError, TypeError):
                pass

        min_score: float = _DEFAULT_MIN_SCORE
        if min_score_raw:
            try:
                ms = float(min_score_raw)
                if 0.0 <= ms <= 1.0:
                    min_score = ms
            except (ValueError, TypeError):
                pass

        return {
            "mode": mode,
            "max_decisions": max_decisions,
            "max_tokens": max_tokens,
            "min_score": min_score,
        }

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_INJECT_MODE",
                "description": "off | inject",
            },
            "max_decisions": {
                "type": "integer",
                "default": _DEFAULT_MAX_DECISIONS,
                "env": "CODEVIRA_INJECT_MAX_DECISIONS",
                "description": "Cap on decisions per injection (1..20)",
            },
            "max_tokens": {
                "type": "integer",
                "default": _DEFAULT_MAX_TOKENS,
                "env": "CODEVIRA_INJECT_MAX_TOKENS",
                "description": "Hard token budget for the injection block",
            },
            "min_score": {
                "type": "float",
                "default": _DEFAULT_MIN_SCORE,
                "env": "CODEVIRA_INJECT_MIN_SCORE",
                "description": "Decisions below this score never inject",
            },
        }

    # ─── Evaluation ────────────────────────────────────────────────────

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Gate 1: event type
        if event.event_type != EventType.USER_PROMPT_SUBMIT:
            return PolicyVerdict.allow()

        # Gate 2: prompt sanity
        prompt = (event.prompt_text or "").strip()
        if len(prompt) < _MIN_PROMPT_CHARS:
            return PolicyVerdict.allow()

        # Gate 3: config
        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Gate 4: storage availability
        try:
            from mcp_server.storage import paths

            if not paths.is_initialized():
                # No .codevira/ in this project; nothing to inject.
                return PolicyVerdict.allow()
        except Exception:
            return PolicyVerdict.allow()

        # Stage 1: load manifest + digest
        manifest_data, digest_records = self._load_indexes()
        if not manifest_data.get("active_decisions"):
            # Empty project.
            return PolicyVerdict.allow()

        # Stage 2: gather candidate ids by source
        prompt_lower = prompt.lower()
        tag_candidates = self._tag_candidates(prompt_lower, manifest_data)
        file_candidates = self._file_candidates(prompt_lower, manifest_data)
        fts_candidates = self._fts_candidates(prompt, limit=config["max_decisions"] * 4)

        # Stage 3: score
        scored = self._score_candidates(
            tag_candidates=tag_candidates,
            file_candidates=file_candidates,
            fts_candidates=fts_candidates,
            digest_records=digest_records,
            min_score=config["min_score"],
        )
        if not scored:
            # Off-topic — emit ZERO tokens (the v2.2.0 promise).
            return PolicyVerdict.allow()

        # Stage 4: token-budget enforcement (trim to fit)
        block = self._render_block(
            scored,
            max_decisions=config["max_decisions"],
            max_tokens=config["max_tokens"],
        )
        if not block:
            return PolicyVerdict.allow()

        return PolicyVerdict.inject(
            context=block,
            metadata={
                "policy": self.name,
                "tags_matched": sorted(set(tag_candidates.keys())),
                "files_matched": sorted(set(file_candidates.keys())),
                "fts_matches": len(fts_candidates),
                "decisions_injected": [s["id"] for s in scored if s["_emitted"]],
                "tokens_estimated": sum(
                    s["_emitted_tokens"] for s in scored if s["_emitted"]
                ),
            },
        )

    # ─── Stage helpers ────────────────────────────────────────────────

    def _load_indexes(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read manifest + digest. Both files are small (KB range)."""
        try:
            from mcp_server.storage import jsonl_store, manifest as manifest_mod, paths

            manifest_data = manifest_mod.load(paths.manifest_path())
            digest_records = jsonl_store.read_all(paths.digest_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("relevance_inject._load_indexes failed: %s", exc)
            return ({}, [])
        return manifest_data, digest_records

    def _tag_candidates(
        self, prompt_lower: str, manifest_data: dict[str, Any]
    ) -> dict[str, list[str]]:
        """Return ``{tag_name: [decision_ids]}`` for tags mentioned in the prompt."""
        out: dict[str, list[str]] = {}
        for tag, ids in (manifest_data.get("tags") or {}).items():
            # Substring match — tag is already lowercase per manifest contract.
            if tag and tag in prompt_lower:
                out[tag] = list(ids)
        return out

    def _file_candidates(
        self, prompt_lower: str, manifest_data: dict[str, Any]
    ) -> dict[str, list[str]]:
        """Return ``{file_path: [decision_ids]}`` for paths mentioned in prompt.

        Matches either the full path OR just the basename (so prompts like
        "edit auth.py" match decisions touching "src/auth.py").
        """
        out: dict[str, list[str]] = {}
        for fp, ids in (manifest_data.get("files") or {}).items():
            if not fp:
                continue
            fp_lower = fp.lower()
            basename = fp_lower.rsplit("/", 1)[-1]
            if fp_lower in prompt_lower or (basename and basename in prompt_lower):
                out[fp] = list(ids)
        return out

    def _fts_candidates(self, prompt: str, *, limit: int) -> list[dict[str, Any]]:
        """FTS5 keyword search on prompt text; returns BM25-ranked hits."""
        try:
            from mcp_server.storage import decisions_store

            return decisions_store.search(prompt, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("relevance_inject._fts_candidates failed: %s", exc)
            return []

    def _score_candidates(
        self,
        *,
        tag_candidates: dict[str, list[str]],
        file_candidates: dict[str, list[str]],
        fts_candidates: list[dict[str, Any]],
        digest_records: list[dict[str, Any]],
        min_score: float,
    ) -> list[dict[str, Any]]:
        """Merge tag/file/FTS hits + outcome weight; return scored decisions
        sorted by score desc."""
        digest_by_id = {str(d.get("id")): d for d in digest_records}

        # Accumulate component scores per decision id.
        tag_score: dict[str, float] = {}
        for ids in tag_candidates.values():
            for did in ids:
                tag_score[did] = tag_score.get(did, 0.0) + _TAG_WEIGHT
        file_score: dict[str, float] = {}
        for ids in file_candidates.values():
            for did in ids:
                file_score[did] = file_score.get(did, 0.0) + _FILE_WEIGHT

        # FTS BM25 → [0, _FTS_WEIGHT]. Lower BM25 = better; we invert.
        # Simple normalization: top hit gets full _FTS_WEIGHT,
        # each next halves the contribution (geometric falloff).
        fts_score: dict[str, float] = {}
        for i, hit in enumerate(fts_candidates):
            did = str(hit.get("decision_id") or hit.get("id"))
            fts_score[did] = fts_score.get(did, 0.0) + _FTS_WEIGHT * (0.5**i)

        all_ids = set(tag_score) | set(file_score) | set(fts_score)
        scored: list[dict[str, Any]] = []
        for did in all_ids:
            base = (
                tag_score.get(did, 0.0)
                + file_score.get(did, 0.0)
                + fts_score.get(did, 0.0)
            )
            digest_rec = digest_by_id.get(did)
            if digest_rec is None:
                # Decision exists in manifest but not in digest — likely
                # stale digest. Use neutral weight.
                weight = 0.5
                summary = "(decision summary unavailable — try `codevira sync`)"
                do_not_revert = False
                file_path = None
                tags: list[str] = []
            else:
                weight = float(digest_rec.get("weight", 0.5))
                summary = str(digest_rec.get("summary", ""))
                do_not_revert = bool(digest_rec.get("do_not_revert", False))
                file_path = digest_rec.get("file")
                tags = list(digest_rec.get("tags", []))

            final = base * max(weight, 0.1)  # never zero-out a real match
            if final < min_score:
                continue

            scored.append(
                {
                    "id": did,
                    "score": final,
                    "summary": summary,
                    "do_not_revert": do_not_revert,
                    "file": file_path,
                    "tags": tags,
                    "_components": {
                        "tag": tag_score.get(did, 0.0),
                        "file": file_score.get(did, 0.0),
                        "fts": fts_score.get(did, 0.0),
                        "weight": weight,
                    },
                    "_emitted": False,
                    "_emitted_tokens": 0,
                }
            )

        # Sort: score desc, then id asc (deterministic for cache stability).
        scored.sort(key=lambda s: (-s["score"], s["id"]))
        return scored

    def _render_block(
        self,
        scored: list[dict[str, Any]],
        *,
        max_decisions: int,
        max_tokens: int,
    ) -> str:
        """Render the cache-stable injection block, enforcing token budget.

        Returns empty string if nothing fits in budget.
        """
        from mcp_server.storage import token_estimator

        # Take top-K, sorted by id for deterministic output (cache stable).
        top = scored[:max_decisions]
        # Re-sort the EMITTED set by ID so identical (id, summary) inputs
        # always produce identical bytes. Otherwise prompt-cache hit
        # rate suffers across runs that ranked the same set in
        # different orders.
        emitted_ordered = sorted(top, key=lambda s: s["id"])

        lines: list[str] = []
        body_buffer: list[str] = []
        for d in emitted_ordered:
            line = self._format_decision_line(d)
            trial = body_buffer + [line]
            trial_text = "\n".join(trial)
            if token_estimator.estimate_tokens(trial_text) > max_tokens:
                # Doesn't fit — stop here. The id we cut and below get
                # _emitted=False (metadata reflects what actually shipped).
                break
            body_buffer.append(line)
            d["_emitted"] = True
            d["_emitted_tokens"] = token_estimator.estimate_tokens(line)

        if not body_buffer:
            return ""

        body = "\n".join(body_buffer)
        cache_key = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

        # Cache-stable header + footer (no timestamps).
        lines.append(f'<codevira-context cache_key="{cache_key}">')
        lines.append("Prior decisions you may want to consider:")
        lines.append("")
        lines.append(body)
        lines.append("")
        lines.append(
            "If your current request conflicts with any of these, surface the "
            "conflict to the user before proceeding."
        )
        lines.append("</codevira-context>")
        return "\n".join(lines)

    def _format_decision_line(self, d: dict[str, Any]) -> str:
        """One line per decision. Cache-stable (no timestamp, no score)."""
        prefix = "🔒 " if d["do_not_revert"] else "• "
        file_part = f"  `{d['file']}`" if d.get("file") else ""
        return f"{prefix}**{d['id']}** {d['summary']}{file_part}"
