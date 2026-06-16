"""Learned hot-path ranking weights — Phase 13.

Persists the weight vector the cold-path tuner found (and proved better than
the shipped defaults via the E3 objective) to ``.codevira/learned_weights.json``.
Committed + team-shared like the rest of ``.codevira/`` canonical state, and —
critically — STABLE: it changes only when the tuner re-runs (debounced at the
Stop hook), so the hot path's injection stays cache-stable within a session.

The hot path reads this at most once per process and only when the user opts
in (``CODEVIRA_LEARNED_WEIGHTS``); a missing/corrupt file transparently falls
back to the shipped defaults — learned weights can never make the read surface
worse than it ships.
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
