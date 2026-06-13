"""_diff_envelope.py — synthesize the codevira ``--- before / --- after``
diff envelope from an IDE edit-tool payload.

Single source of truth shared by both engine wiring entry points
(``claude_code_hooks`` and ``mcp_dispatch``) so every edit reaches the
policies in the SAME parseable shape.

Why this exists (Phase 9 false-positive fix): the policy additive-edit
guards — ``decision_lock._is_pure_insertion``, ``blast_radius`` signature
analysis, and ``anti_regression.is_revert`` — all key on this envelope
(parsed by ``_signature_detect.parse_diff`` and
``fix_history._EDIT_FORMAT_RE``). ``Edit`` and ``MultiEdit`` always
produced the envelope from their in-payload ``old_string`` /
``new_string``. ``Write`` only carried raw file content with no
envelope, so ``parse_diff`` returned ``(None, None)``, every additive
guard was silently bypassed, and a purely-additive full-file Write to a
locked or high-fan-in file was hard-blocked as if it were destructive.
Reading the current on-disk content as the ``before`` block restores an
honest diff so the existing (correct) guards work for ``Write`` too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


#: Upper bound on the synthesized envelope size. Matches the cap
#: ``decision_lock._is_pure_insertion`` enforces (``len(diff) > 1_000_000``)
#: so we never build an envelope a downstream guard rejects for size.
#: Real source files are well under this (largest in-repo ~107 KB), so the
#: raw-content fallback only triggers for pathological inputs.
_MAX_ENVELOPE_BYTES = 1_000_000


def _read_current_content(target_file: Path | None) -> str:
    """Return the edit target's current on-disk content, or ``""``.

    ``""`` covers a brand-new file (nothing on disk yet — the Write is a
    create) and any unreadable target (permissions, binary, vanished).
    Best-effort by contract: the wiring layer must never raise and abort
    a user's tool call, so all errors degrade to ``""`` (treated as a
    create → the guards see a pure insertion).

    Args:
        target_file: Resolved path of the file being written, or None.

    Returns:
        The file's text content, or ``""`` when absent/unreadable.
    """
    if target_file is None:
        return ""
    try:
        if not target_file.is_file():
            return ""
        return target_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _envelope(before: str, after: str) -> str:
    """Wrap before/after blocks in the codevira diff envelope."""
    return f"--- before\n{before}\n--- after\n{after}\n"


def synthesize_proposed_diff(
    tool_name: str,
    tool_input: dict[str, Any],
    target_file: Path | None,
) -> str | None:
    """Build the ``--- before / --- after`` envelope for an edit tool.

    Per tool:
        - ``Edit``: from ``old_string`` / ``new_string`` (no I/O).
        - ``MultiEdit``: concatenated ``old_string`` / ``new_string``
          across edits (no I/O).
        - ``Write``: current on-disk content as ``before`` (``""`` for a
          new file) and the proposed ``content`` as ``after`` — one
          bounded file read. Falls back to raw content when the envelope
          would exceed ``_MAX_ENVELOPE_BYTES``.
        - ``NotebookEdit``: raw cell source (unchanged). A notebook's
          on-disk form is JSON, so reading it as ``before`` against a
          single cell's ``after`` would be misleading; the conservative
          raw-content behavior is preserved.

    Args:
        tool_name: The IDE tool being invoked.
        tool_input: The tool's argument dict.
        target_file: Resolved, in-project path of the edit target (or
            None when the tool has no file or the path was rejected).

    Returns:
        The envelope string, raw content for the oversized/notebook
        fallback, or None when there's nothing to diff.

    Example:
        >>> synthesize_proposed_diff(
        ...     "Edit",
        ...     {"old_string": "a", "new_string": "a\\nb"},
        ...     None,
        ... )
        '--- before\\na\\n--- after\\na\\nb\\n'
    """
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        return _envelope(old, new) if (old or new) else None

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        if not isinstance(edits, list) or not edits:
            return None
        # Concatenate each edit's old/new into one before/after pair.
        # Joining with newline preserves line-anchored regex behavior in
        # the signature detectors.
        old_parts: list[str] = []
        new_parts: list[str] = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            old_parts.append(str(e.get("old_string", "")))
            new_parts.append(str(e.get("new_string", "")))
        joined_old = "\n".join(old_parts)
        joined_new = "\n".join(new_parts)
        if joined_old or joined_new:
            return _envelope(joined_old, joined_new)
        return None

    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None
        before = _read_current_content(target_file)
        # Keep the envelope within the policy size cap; otherwise pass raw
        # content (legacy behavior → conservative block on locked / high-
        # fan-in files, acceptable for un-analyzable giant writes).
        if len(before) + len(content) > _MAX_ENVELOPE_BYTES:
            return content
        return _envelope(before, content)

    if tool_name == "NotebookEdit":
        content = (
            tool_input.get("new_source")
            or tool_input.get("cell_source")
            or tool_input.get("source")
        )
        return content if isinstance(content, str) else None

    return None
