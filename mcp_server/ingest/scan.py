"""Session-scan orchestrator — E2 (Phase 20). READ-ONLY.

Runs every registered per-tool parser over its local logs, keeps only the
sessions with something worth reflecting on (a failure or a correction),
and returns a recency-capped list of normalized digests. Each parser is
isolated: a parser that raises or finds nothing simply contributes nothing
— one IDE's broken log format never breaks the scan.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from mcp_server.ingest import claude_code, codex, gemini
from mcp_server.ingest.models import SessionDigest

# (source name, find_session_files, parse_file). Add an IDE = add a row.
_PARSERS: tuple[tuple[str, Callable, Callable], ...] = (
    (claude_code.SOURCE, claude_code.find_session_files, claude_code.parse_file),
    (codex.SOURCE, codex.find_session_files, codex.parse_file),
    (gemini.SOURCE, gemini.find_session_files, gemini.parse_file),
)

DEFAULT_SINCE_DAYS = 30
DEFAULT_MAX_SESSIONS = 20
MAX_FILES_PER_SOURCE = 200  # bound the scan on a huge history


def scan_sessions(
    project_root: Path,
    *,
    sources: list[str] | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    roots: dict[str, Path] | None = None,
    now: float | None = None,
) -> list[SessionDigest]:
    """Return interesting session digests across all (or selected) IDEs.

    Newest-first, capped at ``max_sessions``. Never raises — a parser that
    errors on a file is skipped. ``roots`` overrides per-source log dirs
    (test seam). ``now`` overrides the recency clock (test seam).
    """
    now_ts = now if now is not None else time.time()
    cutoff = now_ts - max(since_days, 1) * 86400
    digests: list[tuple[float, SessionDigest]] = []

    for source, find_files, parse in _PARSERS:
        if sources and source not in sources:
            continue
        root_override = roots.get(source) if roots else None
        try:
            files = find_files(project_root, root_override)
        except Exception:  # noqa: BLE001 — one IDE never breaks the scan
            continue
        for path in files[:MAX_FILES_PER_SOURCE]:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            try:
                digest = parse(path)
            except Exception:  # noqa: BLE001 — defensive per-file isolation
                continue
            if digest is not None and digest.is_interesting:
                digests.append((mtime, digest))

    digests.sort(key=lambda pair: pair[0], reverse=True)
    return [d for _, d in digests[:max_sessions]]


def to_reflection_signals(digests: list[SessionDigest]) -> list[dict[str, Any]]:
    """Compact, already-sanitized dicts for the reflect prompt. Excerpts were
    scrubbed at parse time (:func:`heuristics.excerpt`)."""
    out: list[dict[str, Any]] = []
    for d in digests:
        out.append(
            {
                "source": d.source,
                "session_id": d.session_id,
                "tool_calls": d.n_tool_calls,
                "failures": [
                    {"tool": f.tool, "error": f.error_excerpt} for f in d.failures
                ],
                "corrections": [
                    {"after_tool": c.after_tool, "said": c.excerpt}
                    for c in d.corrections
                ],
            }
        )
    return out
