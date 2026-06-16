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

# E4 (Phase 22): managed memory files beyond AGENTS.md. The marker-block
# writer is already file-generic; this registry says WHICH files to maintain
# and HOW:
#   shared_md  — a markdown file the user may also edit; merge our block and
#                preserve everything OUTSIDE the markers (AGENTS/CLAUDE/GEMINI).
#   owned_mdc  — a dedicated codevira file we fully own (a Cursor rule); write
#                YAML frontmatter + block, no user content to preserve.
# Default is AGENTS.md ONLY — writing to a user's CLAUDE.md / .cursor / GEMINI.md
# unprompted is surprising, so the rest are OPT-IN via
# ``.codevira/config.yaml: managed_files: [...]``.
_DEFAULT_MANAGED_FILES = ("AGENTS.md",)
_TARGET_MODES = {
    "AGENTS.md": "shared_md",
    "CLAUDE.md": "shared_md",
    "GEMINI.md": "shared_md",
    ".cursor/rules/codevira.mdc": "owned_mdc",
}


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
            try:
                import tomllib  # Python 3.11+
            except ModuleNotFoundError:  # Python 3.10 — stdlib tomllib absent
                import tomli as tomllib  # type: ignore[no-redef]

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

    existing_text: str | None = None
    if not target_path.is_file():
        new_content = block + "\n"
    else:
        existing_text = target_path.read_text(encoding="utf-8")
        begin_idx = existing_text.find(_BEGIN_MARKER)
        end_idx = existing_text.find(_END_MARKER)
        if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
            # No prior block — prepend ours at the top, preserve the rest.
            new_content = block + "\n\n" + existing_text
        else:
            # Replace the existing block (from begin_marker through
            # end_marker inclusive); keep everything before + after.
            before = existing_text[:begin_idx]
            after_start = end_idx + len(_END_MARKER)
            after = existing_text[after_start:]
            new_content = before + block + after

    # v3.1.x: idempotency check kills the auto-regenerate churn.
    # Previously every codevira write → sync → unconditional rewrite,
    # bumping mtime + producing a perpetual uncommitted diff even
    # when no content actually changed.
    if existing_text is not None and existing_text == new_content:
        return  # no-op, no mtime bump, no churn

    # v3.0.0 round-3: atomic write via shared storage.atomic helper.
    # Round-2 fixed the fixed-suffix tmp race inline; round-3
    # consolidated into the helper so every write site shares one
    # crash-safety contract.
    from mcp_server.storage import atomic

    atomic.atomic_write_text(target_path, new_content)


def _merged_decisions() -> list[dict[str, Any]]:
    """Current decision state with amendments applied (same logic as
    ``decisions_store``). Shared by the single- and multi-file writers."""
    decisions = jsonl_store.read_all(paths.decisions_path())
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
    return [merged_by_id[did] for did in insertion_order]


def _managed_targets() -> list[tuple[str, str]]:
    """``(relpath, mode)`` for each file to maintain. Default: AGENTS.md only;
    extend via ``.codevira/config.yaml: managed_files: [...]``. Never raises —
    bad config falls back to the default."""
    relpaths: tuple[str, ...] = _DEFAULT_MANAGED_FILES
    try:
        cfg_path = paths.config_path()
        if cfg_path.is_file():
            import yaml

            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            mf = cfg.get("managed_files")
            if isinstance(mf, list) and mf:
                relpaths = tuple(str(x).strip() for x in mf if str(x).strip())
    except Exception:  # noqa: BLE001 — bad config never breaks the writer
        relpaths = _DEFAULT_MANAGED_FILES
    targets: list[tuple[str, str]] = []
    for rp in relpaths:
        mode = _TARGET_MODES.get(rp) or (
            "owned_mdc" if rp.endswith(".mdc") else "shared_md"
        )
        targets.append((rp, mode))
    return targets


def _mdc_document(block: str, project_name: str | None) -> str:
    """A complete Cursor ``.mdc`` rule file: YAML frontmatter (which MUST be at
    byte 0 for Cursor to parse it) followed by the marker block. codevira owns
    this file entirely, so there is no out-of-marker user content to preserve."""
    name = project_name or "project"
    return (
        "---\n"
        f"description: Codevira persistent memory for {name}\n"
        "alwaysApply: true\n"
        "---\n\n" + block + "\n"
    )


def _write_owned_mdc(target_path: Path, block: str, project_name: str | None) -> None:
    """Write a fully codevira-owned ``.mdc`` (frontmatter + block); atomic +
    idempotent (no mtime churn when unchanged)."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    new_content = _mdc_document(block, project_name)
    if target_path.is_file() and target_path.read_text(encoding="utf-8") == new_content:
        return
    from mcp_server.storage import atomic

    atomic.atomic_write_text(target_path, new_content)


def regenerate_all() -> dict[str, Any]:
    """Regenerate the codevira block in EVERY configured managed file
    (E4, Phase 22).

    The default config writes ``AGENTS.md`` only — byte-for-byte identical to
    the pre-E4 behavior. Opt into more via ``.codevira/config.yaml``::

        managed_files:
          - AGENTS.md
          - CLAUDE.md
          - GEMINI.md
          - .cursor/rules/codevira.mdc

    Each target is independent: one failing target never blocks the others.
    Returns ``{targets: [{path, mode, ok, ...}], count}``.
    """
    from mcp_server.paths import get_project_root

    root = get_project_root()
    pname = _project_name()
    results: list[dict[str, Any]] = []
    owned_block: str | None = None  # render once, reuse for owned targets
    for relpath, mode in _managed_targets():
        target = root / relpath
        try:
            if mode == "owned_mdc":
                if owned_block is None:
                    owned_block = _render_block(_merged_decisions(), pname)
                _write_owned_mdc(target, owned_block, pname)
                results.append({"path": str(target), "mode": mode, "ok": True})
            else:
                summary = regenerate(target_path=target)
                results.append(
                    {"path": str(target), "mode": mode, "ok": True, **summary}
                )
        except Exception as exc:  # noqa: BLE001 — one bad target never blocks others
            logger.warning("regenerate_all: %s failed: %s", target, exc)
            results.append(
                {"path": str(target), "mode": mode, "ok": False, "error": str(exc)}
            )
    return {"targets": results, "count": len(results)}


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

    merged = _merged_decisions()

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

    E4 (Phase 22): regenerates EVERY configured managed file (default config
    = AGENTS.md only, so unchanged for existing users). Logs warnings on
    failure but never raises (P9: don't fail a user write because a memory-
    file regen had a hiccup).
    """
    try:
        regenerate_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agents_md_generator.sync_after_write: %s", exc)
