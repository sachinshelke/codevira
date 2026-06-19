"""
cli_reflect.py — v3.1.0 M8: ``codevira reflect`` CLI.

Three modes (orthogonal):

  - **No flags**: build the source context + render the prompt;
    print it so the user can feed it to their own LLM.
  - ``--from-file <path>``: read an LLM response from the file,
    parse the YAML output, write to
    ``.codevira/reflection_proposals.jsonl`` for review.
  - ``--from-file <path> --apply [--yes]``: write directly to
    ``.codevira/reflections.jsonl``. ``--yes`` skips the interactive
    confirm.

The LLM response format follows the prompt template
(``reflection_v1.md``) — a single ``yaml`` fenced block with
``abstraction``, ``tags``, and ``confidence`` fields.

Offline behavior: CLI mode never calls ``sampling/createMessage``
(no MCP client attached when invoked from a plain terminal). The
``reflect()`` MCP tool returns ``sampling_supported: False`` in
v3.1.0; the CLI is the recommended interactive path until v3.2
wires the live sampling RPC.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp_server.storage import paths, reflections_store


def cmd_reflect(
    *,
    period_days: int = 7,
    from_file: str | None = None,
    apply: bool = False,
    yes: bool = False,
    from_sessions: bool = False,
) -> int:
    """Entry point for ``codevira reflect``.

    Returns 0 on success (including "no-op, here's the prompt").
    Non-zero on parse / storage failure.

    E2 (Phase 20): ``from_sessions=True`` (``--from-sessions``) folds a
    READ-ONLY scan of local IDE session transcripts (tool failures + user
    corrections) into the prompt as additional signal. Candidates only —
    nothing is committed without ``--apply``.
    """
    ctx = reflections_store.build_source_context(
        period_days=period_days, include_transcripts=from_sessions
    )

    if from_file is None:
        # Pure render mode — print the prompt and instruct.
        prompt = reflections_store.render_prompt(ctx)
        n_signals = len(ctx.get("transcript_signals") or [])
        sys.stdout.write(
            f"codevira reflect: built source context for the last "
            f"{period_days} day(s) "
            f"({len(ctx['sessions'])} session(s), "
            f"{len(ctx['decisions'])} decision(s), "
            f"{n_signals} transcript signal(s), "
            f"{ctx['envelope_bytes']} bytes).\n\n"
        )
        sys.stdout.write(
            "Feed the prompt below to your LLM, save its YAML response, then "
            "re-run with --from-file <path> (add --apply --yes to commit "
            "the reflection to .codevira/reflections.jsonl).\n\n"
        )
        sys.stdout.write("─" * 70 + "\n")
        sys.stdout.write(prompt)
        sys.stdout.write("\n" + "─" * 70 + "\n")
        return 0

    # Parse the LLM response file.
    try:
        with open(from_file, encoding="utf-8") as fh:
            response = fh.read()
    except OSError as exc:
        sys.stderr.write(f"codevira reflect: could not read {from_file}: {exc}\n")
        return 1

    parsed = _parse_response(response)
    if not parsed:
        sys.stderr.write(
            "codevira reflect: could not parse abstraction/tags/confidence "
            "from the response. The LLM should return a single yaml-fenced "
            "block per the prompt template (see "
            "mcp_server/data/prompts/reflection_v1.md).\n"
        )
        return 1

    abstraction = parsed.get("abstraction") or ""
    tags = parsed.get("tags") or []
    confidence = parsed.get("confidence")

    if not abstraction.strip():
        sys.stderr.write(
            "codevira reflect: response had an empty 'abstraction:' "
            "field; refusing to record an empty reflection.\n"
        )
        return 1

    # Confirm prompt unless --yes or proposal-only.
    if apply and not yes:
        sys.stdout.write("Proposed reflection:\n")
        sys.stdout.write(f"  tags: {tags}\n")
        sys.stdout.write(f"  confidence: {confidence}\n\n")
        sys.stdout.write(abstraction.strip() + "\n\n")
        sys.stdout.write("Commit to .codevira/reflections.jsonl? [y/N]: ")
        sys.stdout.flush()
        try:
            resp = input().strip().lower()
        except EOFError:
            resp = "n"
        if resp not in ("y", "yes"):
            sys.stdout.write("codevira reflect: not committed.\n")
            return 0

    target = "reflections" if apply else "proposals"
    rid = reflections_store.append(
        abstraction=abstraction,
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float, str)) and _is_floatable(confidence)
            else None
        ),
        tags=tags,
        period_start=ctx["period_start"],
        period_end=ctx["period_end"],
        source_session_ids=ctx["source_session_ids"],
        source_decision_ids=ctx["source_decision_ids"],
        target=target,
    )
    dest = paths.reflections_path() if apply else paths.reflection_proposals_path()
    sys.stdout.write(
        f"codevira reflect: wrote {rid} to {dest}\n"
        f"  ({'committed reflection' if apply else 'proposal for review'})\n"
    )
    return 0


# ──────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────


def _parse_response(text: str) -> dict[str, Any] | None:
    """Pull out the first yaml-fenced block + parse it.

    Falls back to whole-text parsing if no fence is found (some LLMs
    omit the fence even when asked). Returns None on hard failure.
    """
    import re

    try:
        import yaml
    except Exception:  # noqa: BLE001
        return None

    fence_match = re.search(r"```(?:yaml)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    block = fence_match.group(1) if fence_match else text
    try:
        data = yaml.safe_load(block)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def _is_floatable(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
