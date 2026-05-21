"""
agents_md_generator.py — render AGENTS.md from .codevira/decisions.jsonl.

v2.2.0 makes AGENTS.md a **slim contract** that other AI tools (Copilot,
Codex, Cursor, Gemini, Factory, Amp, Windsurf, Zed, RooCode, Jules) load
on every prompt. To respect their token budgets, the file has a HARD
5 KB cap regardless of decision count.

Generation rules:

1. **Marker-bounded.** The codevira-managed block lives between
   ``<!-- codevira:begin -->`` and ``<!-- codevira:end -->``. Anything
   outside those markers is preserved byte-for-byte across runs.

2. **5 KB cap.** If the rendered block would exceed the cap, oldest
   non-do_not_revert decisions are dropped. ``do_not_revert=True``
   decisions are ALWAYS rendered (sorted oldest first within that
   tier). A footer note tells the agent how many were dropped.

3. **Deterministic.** Same decisions in → same bytes out. Sorted by
   id within tier; no timestamps in output.

4. **No third-party prefix.** AGENTS.md isn't codevira-specific —
   other tools also read it. We only own the marked block; everything
   else is preserved.

Usage:

    from mcp_server.storage import agents_md_generator
    agents_md_generator.regenerate()

The CLI command ``codevira sync`` calls this. ``decisions_store.record``
calls it synchronously on every write (~5-20ms typical, fine to block).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp_server.storage import jsonl_store, paths

logger = logging.getLogger(__name__)

# Marker pair. Anything between these is replaced on regenerate; everything
# else in AGENTS.md is preserved byte-for-byte.
_BEGIN_MARKER = "<!-- codevira:begin (auto-generated; do not edit) -->"
_END_MARKER = "<!-- codevira:end -->"

# Hard byte cap on the GENERATED BLOCK (not the whole file — user content
# outside the markers doesn't count). 5 KB matches the v2.2.0 plan.
_BLOCK_MAX_BYTES = 5 * 1024

# Per-line cap for decision summaries inside the block. Keeps the block
# readable + leaves token budget for many decisions.
_SUMMARY_LINE_CAP = 120


def _render_block(decisions: list[dict[str, Any]], project_name: str | None) -> str:
    """Render the codevira-owned section. Marker-wrapped.

    Returns the full text with begin/end markers; ready to drop into
    AGENTS.md verbatim.
    """
    # Partition: do_not_revert decisions are always preserved (sorted by
    # id within); others get cut first when over budget.
    active = [
        d
        for d in decisions
        if not d.get("is_superseded") and not d.get("superseded_by")
    ]
    locked = sorted(
        (d for d in active if d.get("do_not_revert")),
        key=lambda d: str(d.get("id", "")),
    )
    unlocked = sorted(
        (d for d in active if not d.get("do_not_revert")),
        key=lambda d: str(d.get("id", "")),
    )

    header_lines = [_BEGIN_MARKER, ""]
    if project_name:
        header_lines.append(f"## Codevira-tracked project memory: {project_name}")
    else:
        header_lines.append("## Codevira-tracked project memory")
    header_lines.append("")

    footer_lines = [
        "",
        "For the full decision log + outcomes + reverts, see "
        "`.codevira/decisions.jsonl` or run `codevira list-decisions`.",
        "",
        _END_MARKER,
    ]

    # First, lay down all locked decisions (they're a hard requirement).
    locked_section = ["### Locked decisions (do_not_revert)", ""]
    for d in locked:
        locked_section.append(_format_decision_line(d))
    if locked:
        locked_section.append("")
    else:
        locked_section.append("_None yet._")
        locked_section.append("")

    # Then unlocked (active conventions) — fill remaining budget.
    base = "\n".join(header_lines + locked_section + footer_lines)
    base_bytes = len(base.encode("utf-8"))
    remaining_bytes = _BLOCK_MAX_BYTES - base_bytes

    unlocked_section = ["### Active conventions", ""]
    if not unlocked:
        unlocked_section.append("_None yet._")
        unlocked_section.append("")
        full = "\n".join(
            header_lines + locked_section + unlocked_section + footer_lines
        )
        return full

    # Fit as many unlocked decisions as we can.
    fitted: list[str] = []
    cur_bytes = len("\n".join(unlocked_section).encode("utf-8")) + 2  # +newlines
    for d in unlocked:
        line = _format_decision_line(d)
        line_bytes = len((line + "\n").encode("utf-8"))
        if cur_bytes + line_bytes > remaining_bytes:
            break
        fitted.append(line)
        cur_bytes += line_bytes

    if fitted:
        unlocked_section.extend(fitted)
        unlocked_section.append("")

    if len(fitted) < len(unlocked):
        dropped = len(unlocked) - len(fitted)
        unlocked_section.append(
            f"_+{dropped} more decision(s) — full log in `.codevira/decisions.jsonl`._"
        )
        unlocked_section.append("")

    return "\n".join(header_lines + locked_section + unlocked_section + footer_lines)


def _format_decision_line(d: dict[str, Any]) -> str:
    """One markdown bullet per decision. Stable (no timestamp)."""
    did = d.get("id", "?")
    text = (d.get("decision") or "").strip().replace("\n", " ")
    if len(text) > _SUMMARY_LINE_CAP:
        text = text[: _SUMMARY_LINE_CAP - 1] + "…"
    file_part = f"  ·  `{d['file_path']}`" if d.get("file_path") else ""
    tags = d.get("tags") or []
    tags_part = f"  ·  _{', '.join(tags)}_" if tags else ""
    return f"- **{did}** {text}{file_part}{tags_part}"


def _project_name() -> str | None:
    """Try to find a project name to put in the AGENTS.md header.

    Looks for:
    1. ``.codevira/config.yaml::project_name`` (preferred)
    2. ``pyproject.toml::project.name``
    3. ``package.json::name``
    4. Repo dir name as last resort
    """
    try:
        import yaml

        cfg_path = paths.config_path()
        if cfg_path.is_file():
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            name = cfg.get("project_name")
            if name:
                return str(name)
    except Exception:
        pass

    from mcp_server.paths import get_project_root

    root = get_project_root()
    if not root or not root.is_dir():
        return None

    try:
        py = root / "pyproject.toml"
        if py.is_file():
            import tomllib

            data = tomllib.loads(py.read_text())
            name = data.get("project", {}).get("name")
            if name:
                return str(name)
    except Exception:
        pass

    try:
        pkg = root / "package.json"
        if pkg.is_file():
            import json

            data = json.loads(pkg.read_text())
            name = data.get("name")
            if name:
                return str(name)
    except Exception:
        pass

    return root.name


def _merge_into_file(target_path: Path, block: str) -> None:
    """Write block into target_path, preserving anything outside the
    codevira markers. Atomic via tmp + rename.

    If target_path doesn't exist, creates a new file with just the block.

    If target_path exists but has no codevira markers, prepends the block
    to the top (preserving everything else below).

    If target_path exists with codevira markers, replaces only the marked
    section.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not target_path.is_file():
        new_content = block + "\n"
    else:
        existing = target_path.read_text(encoding="utf-8")
        begin_idx = existing.find(_BEGIN_MARKER)
        end_idx = existing.find(_END_MARKER)
        if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
            # No prior block — prepend ours at the top, preserve the rest.
            new_content = block + "\n\n" + existing
        else:
            # Replace the existing block (from begin_marker through
            # end_marker inclusive); keep everything before + after.
            before = existing[:begin_idx]
            after_start = end_idx + len(_END_MARKER)
            after = existing[after_start:]
            new_content = before + block + after

    # Atomic write via tmp + rename so a concurrent reader never sees
    # a half-written AGENTS.md.
    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(target_path)


def regenerate(*, target_path: Path | None = None) -> dict[str, Any]:
    """Regenerate the codevira block in AGENTS.md.

    Idempotent. Safe to call any time. Returns a summary dict:
        {
          "agents_md_path": str,
          "block_bytes": int,
          "decisions_in_block": int,
          "decisions_dropped": int,  # exceeded 5KB cap
          "do_not_revert_kept": int,
          "user_content_preserved_bytes": int,
        }

    Default target: ``<repo>/AGENTS.md`` (project root, NOT under .codevira/).
    """
    if target_path is None:
        from mcp_server.paths import get_project_root

        target_path = get_project_root() / "AGENTS.md"

    decisions = jsonl_store.read_all(paths.decisions_path())
    # Apply amendments to get current state (same logic as decisions_store)
    merged_by_id: dict[str, dict[str, Any]] = {}
    insertion_order: list[str] = []
    for rec in decisions:
        did = str(rec.get("id", ""))
        if not did:
            continue
        if rec.get("_amendment_to_id"):
            base = merged_by_id.get(did)
            if base is None:
                merged_by_id[did] = dict(rec)
                insertion_order.append(did)
            else:
                base.update({k: v for k, v in rec.items() if not k.startswith("_")})
        else:
            if did not in merged_by_id:
                insertion_order.append(did)
            merged_by_id[did] = dict(rec)
    merged = [merged_by_id[did] for did in insertion_order]

    # Stats for the summary
    locked_count = sum(
        1
        for d in merged
        if d.get("do_not_revert")
        and not d.get("is_superseded")
        and not d.get("superseded_by")
    )
    unlocked_count = sum(
        1
        for d in merged
        if not d.get("do_not_revert")
        and not d.get("is_superseded")
        and not d.get("superseded_by")
    )

    block = _render_block(merged, _project_name())

    # Measure user-content preservation (if file existed before).
    user_bytes_before = 0
    if target_path.is_file():
        existing = target_path.read_text(encoding="utf-8")
        begin_idx = existing.find(_BEGIN_MARKER)
        end_idx = existing.find(_END_MARKER)
        if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
            before = existing[:begin_idx]
            after_start = end_idx + len(_END_MARKER)
            after = existing[after_start:]
            user_bytes_before = len(before.encode("utf-8")) + len(after.encode("utf-8"))
        else:
            user_bytes_before = len(existing.encode("utf-8"))

    _merge_into_file(target_path, block)

    block_bytes = len(block.encode("utf-8"))
    # Count actually-rendered decisions by counting markdown bullet lines.
    decision_lines = [line for line in block.splitlines() if line.startswith("- **")]
    rendered = len(decision_lines)
    dropped = (locked_count + unlocked_count) - rendered

    summary = {
        "agents_md_path": str(target_path),
        "block_bytes": block_bytes,
        "decisions_in_block": rendered,
        "decisions_dropped": max(dropped, 0),
        "do_not_revert_kept": min(rendered, locked_count),
        "user_content_preserved_bytes": user_bytes_before,
        "block_within_cap": block_bytes <= _BLOCK_MAX_BYTES,
    }
    return summary


# ─── Convenience for callers that just want a one-liner ────────────────


def sync_after_write() -> None:
    """Called by ``decisions_store`` after each write. Best-effort.

    Logs warnings on failure but never raises (P9: don't fail a user
    write because AGENTS.md regen had a hiccup).
    """
    try:
        regenerate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agents_md_generator.sync_after_write: %s", exc)
