"""Claude Code session parser — E2 (Phase 20). READ-ONLY.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<slug>/<session-uuid>.jsonl`` where ``<slug>`` is the
project's absolute path with ``/`` replaced by ``-``. Each line is one
record; the ones we care about:

* ``assistant`` → ``message.content[]`` carries ``tool_use`` blocks
  ``{name, input, id}``.
* ``user``      → ``message.content[]`` carries ``tool_result`` blocks
  ``{tool_use_id, is_error, content}``; a STRING / text content is a human
  turn (a correction candidate).

Grounded in real local logs (2026-06-16). Defensive throughout: a record
that doesn't match is skipped, never raised.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server.ingest import heuristics as H
from mcp_server.ingest.models import CorrectionTurn, SessionDigest, ToolEvent

SOURCE = "claude_code"


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _slug(project_root: Path) -> str:
    # Claude Code's encoding: absolute path with every "/" → "-".
    return str(project_root).replace("/", "-")


def find_session_files(project_root: Path, root: Path | None = None) -> list[Path]:
    """All session JSONL files for ``project_root`` (newest first). Empty on
    any error or missing directory."""
    base = (root or default_root()) / _slug(project_root)
    try:
        if not base.is_dir():
            return []
        files = [p for p in base.glob("*.jsonl") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files
    except OSError:
        return []


def _text_of(content: object) -> str:
    """Flatten a Claude message ``content`` (str | list[block]) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return " ".join(parts)
    return ""


def parse_file(path: Path) -> SessionDigest | None:
    """Reduce one session log to a digest, or ``None`` if it can't be read /
    doesn't look like a Claude Code transcript."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    tool_by_id: dict[str, str] = {}  # tool_use id → tool name
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
        rtype = rec.get("type")
        if rtype in ("assistant", "user", "system"):
            saw_known_record = True
        if started_at is None and isinstance(rec.get("timestamp"), str):
            started_at = rec["timestamp"]
        if rec.get("sessionId"):
            session_id = str(rec["sessionId"])

        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None

        if rtype == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")
                    n_tool_calls += 1
                    last_tool = name
                    bid = block.get("id")
                    if bid:
                        tool_by_id[str(bid)] = name

        elif rtype == "user":
            # tool_result blocks (failures) OR a human turn (correction).
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                for block in content:
                    if not (
                        isinstance(block, dict) and block.get("type") == "tool_result"
                    ):
                        continue
                    if not block.get("is_error"):
                        continue
                    name = tool_by_id.get(
                        str(block.get("tool_use_id")), last_tool or "tool"
                    )
                    if len(failures) < H.MAX_FAILURES_PER_SESSION:
                        failures.append(
                            ToolEvent(
                                tool=name,
                                error_excerpt=H.excerpt(_text_of(block.get("content"))),
                                seq=n_tool_calls,
                            )
                        )
            elif not rec.get("isMeta"):
                text = _text_of(content)
                if (
                    H.looks_like_correction(text)
                    and len(corrections) < H.MAX_CORRECTIONS_PER_SESSION
                ):
                    corrections.append(
                        CorrectionTurn(
                            excerpt=H.excerpt(text),
                            after_tool=last_tool,
                            seq=n_tool_calls,
                        )
                    )

    if not saw_known_record:
        return None  # not a Claude Code transcript

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
