"""Codex CLI session parser — E2 (Phase 20). READ-ONLY.

Codex writes JSONL session logs under ``~/.codex/sessions/<date dirs>/``.
Each line is ``{type, timestamp, payload}``; the records we read are
``response_item`` whose ``payload.type`` is one of:

* ``function_call`` / ``custom_tool_call``         → a tool call ``{name, call_id}``
* ``function_call_output`` / ``custom_tool_call_output`` → its result ``{call_id, output}``
* ``message`` with ``role == "user"``               → a human turn (correction candidate)

Codex has no explicit ``is_error`` flag on outputs, so failures are detected
heuristically from the output text (:func:`heuristics.output_looks_failed`).
Grounded in real local logs (2026-06-16); defensive throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server.ingest import heuristics as H
from mcp_server.ingest.models import CorrectionTurn, SessionDigest, ToolEvent

SOURCE = "codex"

_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call"}
_OUTPUT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
}


def default_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def find_session_files(project_root: Path, root: Path | None = None) -> list[Path]:
    """Codex sessions aren't keyed by project on disk, so we return all
    recent session logs (newest first); the digest carries provenance and
    the reflect step is project-scoped by the decisions/sessions it joins."""
    base = root or default_root()
    try:
        if not base.is_dir():
            return []
        files = [p for p in base.rglob("*.jsonl") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files
    except OSError:
        return []


def _user_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "input_text",
                "text",
                "output_text",
            ):
                parts.append(str(block.get("text") or ""))
        return " ".join(parts)
    return ""


def parse_file(path: Path) -> SessionDigest | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    call_by_id: dict[str, str] = {}
    n_tool_calls = 0
    failures: list[ToolEvent] = []
    corrections: list[CorrectionTurn] = []
    last_tool = ""
    started_at: str | None = None
    session_id = path.stem
    saw_known_record = False

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if started_at is None and isinstance(rec.get("timestamp"), str):
            started_at = rec["timestamp"]

        rtype = rec.get("type")
        if rtype == "session_meta":
            saw_known_record = True
            payload = rec.get("payload")
            if isinstance(payload, dict) and payload.get("id"):
                session_id = str(payload["id"])
            continue
        if rtype != "response_item":
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if ptype in _CALL_TYPES:
            saw_known_record = True
            name = str(payload.get("name") or "tool")
            n_tool_calls += 1
            last_tool = name
            cid = payload.get("call_id")
            if cid:
                call_by_id[str(cid)] = name
        elif ptype in _OUTPUT_TYPES:
            saw_known_record = True
            if H.output_looks_failed(payload.get("output")):
                name = call_by_id.get(str(payload.get("call_id")), last_tool or "tool")
                if len(failures) < H.MAX_FAILURES_PER_SESSION:
                    failures.append(
                        ToolEvent(
                            tool=name,
                            error_excerpt=H.excerpt(
                                _user_text(payload.get("output"))
                                or str(payload.get("output"))
                            ),
                            seq=n_tool_calls,
                        )
                    )
        elif ptype == "message" and payload.get("role") == "user":
            saw_known_record = True
            text = _user_text(payload.get("content"))
            if (
                H.looks_like_correction(text)
                and len(corrections) < H.MAX_CORRECTIONS_PER_SESSION
            ):
                corrections.append(
                    CorrectionTurn(
                        excerpt=H.excerpt(text), after_tool=last_tool, seq=n_tool_calls
                    )
                )

    if not saw_known_record:
        return None

    return SessionDigest(
        source=SOURCE,
        session_id=session_id,
        path=str(path),
        started_at=started_at,
        n_tool_calls=n_tool_calls,
        n_failures=len(failures),
        n_corrections=len(corrections),
        failures=tuple(failures),
        corrections=tuple(corrections),
    )
