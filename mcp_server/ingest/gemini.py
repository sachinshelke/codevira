"""Gemini CLI session parser — E2 (Phase 20). READ-ONLY.

Gemini CLI stores chat history as JSON under
``~/.gemini/tmp/<project-hash>/chats/*.json``. A chat is a list of messages
(or an object wrapping one under ``messages``/``history``) in the Gemini
``Content`` shape: ``{role: "user"|"model", parts: [...]}`` where a part is
one of ``{text}`` / ``{functionCall: {name, args}}`` /
``{functionResponse: {name, response}}``.

No local samples were available when this was written, so the parser is
built from the documented format and is strictly defensive: any file that
doesn't match the expected shape returns ``None`` (skipped), never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server.ingest import heuristics as H
from mcp_server.ingest.models import CorrectionTurn, SessionDigest, ToolEvent

SOURCE = "gemini"


def default_root() -> Path:
    return Path.home() / ".gemini" / "tmp"


def find_session_files(project_root: Path, root: Path | None = None) -> list[Path]:
    base = root or default_root()
    try:
        if not base.is_dir():
            return []
        files = [
            p
            for p in base.rglob("*.json")
            if p.is_file()
            and ("chat" in p.name.lower() or "chats" in str(p.parent).lower())
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files
    except OSError:
        return []


def _messages(data: object) -> list | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "history", "contents", "turns"):
            seq = data.get(key)
            if isinstance(seq, list):
                return seq
    return None


def _text_of(parts: object) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        out = []
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
        return " ".join(out)
    return ""


def parse_file(path: Path) -> SessionDigest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    messages = _messages(data)
    if not messages:
        return None

    n_tool_calls = 0
    failures: list[ToolEvent] = []
    corrections: list[CorrectionTurn] = []
    last_tool = ""
    saw_known_record = False

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        parts = msg.get("parts")
        if not isinstance(parts, list):
            # Some exports use {role, content} with a plain string.
            if role == "user" and H.looks_like_correction(msg.get("content")):
                saw_known_record = True
                corrections.append(
                    CorrectionTurn(
                        excerpt=H.excerpt(msg.get("content")), after_tool=last_tool
                    )
                )
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "functionCall" in part and isinstance(part["functionCall"], dict):
                saw_known_record = True
                name = str(part["functionCall"].get("name") or "tool")
                n_tool_calls += 1
                last_tool = name
            elif "functionResponse" in part and isinstance(
                part["functionResponse"], dict
            ):
                saw_known_record = True
                fr = part["functionResponse"]
                if H.output_looks_failed(fr.get("response")):
                    name = str(fr.get("name") or last_tool or "tool")
                    if len(failures) < H.MAX_FAILURES_PER_SESSION:
                        failures.append(
                            ToolEvent(
                                tool=name,
                                error_excerpt=H.excerpt(str(fr.get("response"))),
                                seq=n_tool_calls,
                            )
                        )
        if role == "user":
            text = _text_of(parts)
            if (
                H.looks_like_correction(text)
                and len(corrections) < H.MAX_CORRECTIONS_PER_SESSION
            ):
                saw_known_record = True
                corrections.append(
                    CorrectionTurn(excerpt=H.excerpt(text), after_tool=last_tool)
                )

    if not saw_known_record:
        return None

    return SessionDigest(
        source=SOURCE,
        session_id=path.stem,
        path=str(path),
        started_at=None,
        n_tool_calls=n_tool_calls,
        n_failures=len(failures),
        n_corrections=len(corrections),
        failures=tuple(failures),
        corrections=tuple(corrections),
    )
