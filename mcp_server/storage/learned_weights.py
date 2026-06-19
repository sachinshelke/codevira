"""Learned hot-path ranking weights — Phase 13.

Persists the ``{tag, file, fts}`` vector the cold-path tuner
(``codevira tune-weights``) found and proved better than the shipped defaults
ON THE E3 OFFLINE PROXY, to ``.codevira/learned_weights.json``.

**Opt-in, and a heuristic — not a guarantee.** An adversarial review surfaced
these caveats; keep them honest before relying on the file:

* **Opt-in only.** The hot path applies this vector only when the user sets
  ``CODEVIRA_LEARNED_WEIGHTS`` truthy; the default is the shipped defaults.
  There is NO automatic Stop-hook re-tune — the file changes only when you
  re-run ``codevira tune-weights`` by hand. (An auto-tune-on-Stop loop was
  prototyped and reverted: Claude Code's Stop fires per-turn, so a re-tune
  would rewrite this file mid-conversation and bust the prompt cache.)
* **The "win" is on an offline PROXY.** The tuner maximizes the E3 objective
  (recall@k + MRR) over cases self-derived from this project's own decisions.
  That proxy does NOT model precision / noise — it is blind to codevira's
  documented signal-to-noise failure mode (D00005N) — and it diverges from the
  online ``relevance_inject._score_candidates`` scorer (token vs substring
  matching; FTS-only pool vs the tag/file/FTS union; synthesized vs real
  prompts). So a proxy-win can rank WORSE online: there is no hard
  "never worse than defaults" guarantee, which is why apply is opt-in.
* **The win is machine-LOCAL.** It is proven against the producing machine's
  decision corpus + FTS index; a vector tuned elsewhere is not re-verified
  here. Re-run the tuner on each machine rather than trusting a shared file.

A missing or corrupt file transparently falls back to the shipped defaults,
and keeping apply opt-in keeps the conservative default conservative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REQUIRED = {"tag", "file", "fts"}


def path() -> Path:
    from mcp_server.storage import paths

    return paths.decisions_path().parent / "learned_weights.json"


def load() -> dict[str, float] | None:
    """Return the learned ``{tag, file, fts}`` weights, or ``None`` if absent
    / malformed (caller falls back to defaults). Never raises."""
    try:
        p = path()
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        w = data.get("weights")
        if isinstance(w, dict) and _REQUIRED <= set(w):
            out = {k: float(w[k]) for k in _REQUIRED}
            if all(v >= 0 for v in out.values()) and any(v > 0 for v in out.values()):
                return out
    except Exception:  # noqa: BLE001 — hot path must never break on bad config
        pass
    return None


def save(
    weights: dict[str, float],
    *,
    metric: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> bool:
    """Atomically persist the learned weights + provenance. Returns success."""
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "weights": {k: float(weights[k]) for k in _REQUIRED},
            "metric": metric,
            "baseline": baseline,
            "_schema_v": 1,
        }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)  # atomic
        return True
    except Exception:  # noqa: BLE001
        return False
