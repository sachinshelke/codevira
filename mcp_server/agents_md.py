"""
agents_md.py — universal AI-tool nudge-file generator.

Pillar 1 of v2.0. Every AI coding tool the user has installed needs a
nudge file telling it which Codevira MCP tools exist and when to call
each. This module generates those files.

Architecture
------------

There is **one canonical content block** stored in:
    mcp_server/data/templates/canonical_block.md

Every per-IDE file is just that block wrapped in IDE-specific framing
(YAML frontmatter for Cursor MDC; plain markdown for everything else).
The wrapper templates live alongside in the same directory:

    claude_md.tmpl              → CLAUDE.md
    agents_md.tmpl              → AGENTS.md (Linux Foundation standard)
    cursor_rules.mdc.tmpl       → .cursor/rules/codevira.mdc
    windsurfrules.tmpl          → .windsurfrules
    gemini_md.tmpl              → GEMINI.md
    copilot_instructions.tmpl   → .github/copilot-instructions.md

Each template contains the literal placeholder ``{{CODEVIRA_BLOCK}}``
which we substitute with the canonical block content.

Idempotency contract
--------------------

The codevira block is delimited by markers so we can update it without
touching the user's other content:

    <!-- codevira:start -->
    ...generated content...
    <!-- codevira:end -->

On regenerate:
  1. Existing file with markers → replace block content only.
  2. Existing file without markers → append a new block (preserving
     all existing user content).
  3. Missing file → create it from the template.

Markers use HTML-comment syntax because:
  - Markdown ignores them, so they don't render.
  - YAML frontmatter parsers see them as part of body, not metadata.
  - All 6 target file formats accept HTML comments without complaint.

Public API
----------

    canonical_block_text() -> str
        Read and return the canonical block content (no IDE framing).

    render_for_ide(ide: str) -> str
        Return the full content for the given IDE's nudge file
        (canonical block wrapped in IDE-specific framing).

    write_nudge_file(ide: str, target_path: Path,
                     dry_run: bool = False) -> NudgeWriteResult
        Idempotent write. If the target exists with markers, replace
        only the codevira block. If it exists without markers, append.
        If missing, create. Always preserves user content.

The companion ``setup_wizard`` module orchestrates the per-IDE detection
+ multi-file write; this module only knows how to render and write one
file at a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

#: All IDEs we generate nudge files for. The string keys match
#: ``detect_installed_ides`` output where applicable, and the
#: per-IDE config below maps each key to the right template + target
#: path layout. Keep this list in sync with the SUPPORTED IDE matrix
#: in docs/heroes/pillar-1-setup.md.
SUPPORTED_IDES: tuple[str, ...] = (
    "claude",  # Claude Code → <project>/CLAUDE.md
    "cursor",  # Cursor → <project>/.cursor/rules/codevira.mdc
    "windsurf",  # Windsurf → <project>/.windsurfrules
    "antigravity",  # Antigravity / Gemini CLI → <project>/GEMINI.md
    "codex",  # OpenAI Codex CLI → <project>/AGENTS.md
    "copilot",  # GitHub Copilot → <project>/.github/copilot-instructions.md
    "agents_md",  # Tier-2 fallback for any tool that reads AGENTS.md
)

#: HTML-comment markers that delimit the codevira-generated block
#: inside any nudge file. We match them via regex on regenerate.
START_MARKER = "<!-- codevira:start -->"
END_MARKER = "<!-- codevira:end -->"

#: Regex used to find-and-replace the codevira block.
#:
#: Markers MUST be anchored to line boundaries — ``^...marker...$`` with
#: ``re.MULTILINE`` — to prevent a malicious or accidental occurrence of
#: the marker substring inside user prose from being treated as the
#: real codevira section. Without anchoring, "as I noted in my
#: <!-- codevira:start --> comment yesterday..." would be matched as
#: a marker and the user's whole prose between such occurrences would
#: be replaced. Anchoring requires the marker to be the only content on
#: its line (apart from optional surrounding whitespace).
#:
#: ``re.DOTALL`` lets ``.*?`` cross newlines for the body; non-greedy
#: keeps the match linear-time.
_BLOCK_RE = re.compile(
    rf"^[ \t]*{re.escape(START_MARKER)}[ \t]*$.*?^[ \t]*{re.escape(END_MARKER)}[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

#: Placeholder in each per-IDE template that gets substituted with the
#: canonical block content.
_PLACEHOLDER = "{{CODEVIRA_BLOCK}}"


# Per-IDE config: template filename + relative path under project root.
# (relative path uses forward slashes; converted to platform paths at use.)
@dataclass(frozen=True)
class _IDESpec:
    template: str
    rel_path: str  # forward-slash relative path


_IDE_SPECS: dict[str, _IDESpec] = {
    "claude": _IDESpec("claude_md.tmpl", "CLAUDE.md"),
    "cursor": _IDESpec("cursor_rules.mdc.tmpl", ".cursor/rules/codevira.mdc"),
    "windsurf": _IDESpec("windsurfrules.tmpl", ".windsurfrules"),
    "antigravity": _IDESpec("gemini_md.tmpl", "GEMINI.md"),
    "codex": _IDESpec("agents_md.tmpl", "AGENTS.md"),
    "copilot": _IDESpec("copilot_instructions.tmpl", ".github/copilot-instructions.md"),
    "agents_md": _IDESpec("agents_md.tmpl", "AGENTS.md"),
}


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def supported_ides() -> tuple[str, ...]:
    """Return the tuple of IDE keys we know how to generate for."""
    return SUPPORTED_IDES


def canonical_block_text() -> str:
    """Read the canonical block content (no IDE framing).

    The block is loaded via ``importlib.resources`` so it works
    regardless of whether codevira is installed via pipx, editable,
    or as a wheel.
    """
    return _read_template_resource("canonical_block.md").rstrip("\n")


def template_for(ide: str) -> str:
    """Return the raw template string (with ``{{CODEVIRA_BLOCK}}``
    placeholder) for the given IDE key. Raises ``ValueError`` for an
    unknown IDE.
    """
    spec = _IDE_SPECS.get(ide)
    if spec is None:
        raise ValueError(f"unknown IDE: {ide!r}; supported: {SUPPORTED_IDES}")
    return _read_template_resource(spec.template)


def render_for_ide(ide: str) -> str:
    """Return the full content for the given IDE's nudge file.

    This is the canonical block wrapped in the IDE's per-tool framing
    (e.g. YAML frontmatter for Cursor, plain markdown for Claude/AGENTS).
    The output always begins with the start marker (or the IDE's
    frontmatter, then the start marker) and ends with the end marker.
    """
    template = template_for(ide)
    block = canonical_block_text()
    if _PLACEHOLDER not in template:
        raise RuntimeError(
            f"template for {ide!r} is missing the {_PLACEHOLDER} placeholder; "
            f"this is a bug in the bundled template — please report."
        )
    return template.replace(_PLACEHOLDER, block)


def target_path_for(ide: str, project_root: Path) -> Path:
    """Return the absolute target path for the given IDE under the given
    project root. Raises ``ValueError`` for an unknown IDE.
    """
    spec = _IDE_SPECS.get(ide)
    if spec is None:
        raise ValueError(f"unknown IDE: {ide!r}; supported: {SUPPORTED_IDES}")
    return project_root.resolve() / Path(spec.rel_path)


# ---------------------------------------------------------------------
# Write semantics
# ---------------------------------------------------------------------


WriteAction = Literal[
    "created",  # file did not exist; we wrote it from template
    "block_replaced",  # existing file had markers; replaced block only
    "block_appended",  # existing file had no markers; appended new block
    "no_change",  # existing file already had identical content
    "would_create",  # dry-run: would create
    "would_replace",  # dry-run: would replace block
    "would_append",  # dry-run: would append
    "would_be_no_change",  # dry-run: would be no-op
]


@dataclass(frozen=True)
class NudgeWriteResult:
    """Outcome of one ``write_nudge_file`` call."""

    ide: str
    target_path: Path
    action: WriteAction
    bytes_written: int = 0


def write_nudge_file(
    ide: str,
    project_root: Path,
    *,
    dry_run: bool = False,
) -> NudgeWriteResult:
    """Idempotently write the codevira nudge block for an IDE.

    See module docstring for the full idempotency contract:
      - missing → create
      - has markers → replace block content
      - no markers → append block
      - already current → no-op

    Security: refuses to write if the resolved target path lies outside
    ``project_root`` (e.g. a symlink at the target points to /etc/passwd).
    This is defense-in-depth — codevira's hard-coded relative paths can't
    be made to traverse out by themselves, but a malicious or accidental
    pre-existing symlink at the target path could redirect our write
    arbitrarily.

    Args:
        ide: one of ``SUPPORTED_IDES``.
        project_root: absolute project root path.
        dry_run: if True, compute the action that would be taken
                 but do not write to disk.

    Returns:
        ``NudgeWriteResult`` describing the action.

    Raises:
        ValueError: if the target resolves outside ``project_root``.
    """
    target = target_path_for(ide, project_root)
    _enforce_target_inside_project(target, project_root)
    desired_block = _build_block_with_markers()

    # Decide which path we're on.
    # All file writes use _atomic_write_text (write-tmp + os.replace) so
    # a Ctrl-C / SIGKILL mid-write never leaves the target half-written.
    # I7 integration finding C.2.
    if not target.exists():
        # Brand-new file: full template (frontmatter + block + markers).
        new_content = render_for_ide(ide).rstrip("\n") + "\n"
        if dry_run:
            return NudgeWriteResult(ide, target, "would_create")
        n = _atomic_write_text(target, new_content)
        return NudgeWriteResult(ide, target, "created", n)

    existing = _read_text_safely(target)
    if existing is None:
        # Couldn't read; treat as missing for safety, but write the full
        # template anyway. (Disk full / permission errors will surface
        # at write time and propagate.)
        new_content = render_for_ide(ide).rstrip("\n") + "\n"
        if dry_run:
            return NudgeWriteResult(ide, target, "would_create")
        n = _atomic_write_text(target, new_content)
        return NudgeWriteResult(ide, target, "created", n)

    if _BLOCK_RE.search(existing):
        # Replace path: swap in the freshly-rendered block.
        replaced = _BLOCK_RE.sub(desired_block, existing, count=1)
        if replaced == existing:
            action: Any = "would_be_no_change" if dry_run else "no_change"
            return NudgeWriteResult(ide, target, action)
        if dry_run:
            return NudgeWriteResult(ide, target, "would_replace")
        n = _atomic_write_text(target, replaced)
        return NudgeWriteResult(ide, target, "block_replaced", n)

    # Append path: keep all user content, add the codevira block at end.
    suffix_separator = "" if existing.endswith("\n") else "\n"
    appended = existing + suffix_separator + "\n" + desired_block + "\n"
    if dry_run:
        return NudgeWriteResult(ide, target, "would_append")
    n = _atomic_write_text(target, appended)
    return NudgeWriteResult(ide, target, "block_appended", n)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _build_block_with_markers() -> str:
    """Return the codevira block (markers + canonical content) as a
    single string with no leading/trailing newlines. This is what gets
    substituted into the markers section of an existing file on
    regenerate, so we don't want extra whitespace creeping in.
    """
    return f"{START_MARKER}\n{canonical_block_text()}\n{END_MARKER}"


def _read_template_resource(name: str) -> str:
    """Read a packaged template by filename, supporting both editable
    and installed (wheel/sdist) layouts.
    """
    # importlib.resources.files() works for both packaged + editable
    # installs since Python 3.9.
    try:
        return (
            resources.files("mcp_server.data.templates")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback for environments where resources doesn't see the
        # subpackage (e.g. some custom build layouts). Resolve against
        # this module's directory.
        here = Path(__file__).resolve().parent / "data" / "templates"
        return (here / name).read_text(encoding="utf-8")


def _atomic_write_text(target: Path, content: str) -> int:
    """Write ``content`` to ``target`` atomically.

    Strategy: write to a temp file in the SAME directory (so the rename
    is on the same filesystem and ``os.replace`` is atomic), then
    ``os.replace`` the temp into place. ``os.replace`` is atomic on
    POSIX and approximates atomicity on Windows (replaces the existing
    file in one call rather than the dangerous unlink-then-rename
    pattern).

    Why: a Ctrl-C between ``write_text`` chunks leaves the target
    half-written, and the next ``codevira setup`` run sees a corrupt
    file. The marker-based regex replace can then either fail-open
    (lose the codevira block) or fail-closed (truncate user content).
    Caught by Week-1-through-4 integration round I7.

    Returns the number of bytes written.
    """
    import os as _os
    import tempfile

    encoded = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)

    # Use NamedTemporaryFile in the same dir as target so the rename
    # stays on one filesystem.
    fd, _raw_tmp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path: str | None = _raw_tmp
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(encoded)  # tmp_path is guaranteed non-None at this point
            assert tmp_path is not None
            # Flush to disk before rename so a crash doesn't leave the
            # destination pointing at empty content.
            f.flush()
            try:
                _os.fsync(f.fileno())
            except OSError:
                pass  # some filesystems / containers don't support fsync
        _os.replace(tmp_path, target)
        tmp_path = None  # ownership transferred — don't try to clean up below
    finally:
        if tmp_path is not None:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
    return len(encoded)


def _enforce_target_inside_project(target: Path, project_root: Path) -> None:
    """Raise ValueError if the target's resolved path is outside the
    resolved project root, OR if any ancestor of the target is a
    symlink that escapes the project. (We can't just call
    ``target.resolve()`` because it follows symlinks transparently —
    we want to detect them.)
    """
    proj_resolved = project_root.resolve(strict=False)
    # If the target itself is a symlink, refuse — we don't follow.
    if target.is_symlink():
        raise ValueError(
            f"refusing to write through symlink at {target} — codevira "
            f"never follows symlinks for nudge files. Remove the symlink "
            f"and re-run."
        )
    # Walk every parent that exists; if any is a symlink pointing
    # outside the project, refuse.
    for ancestor in [target, *target.parents]:
        if not ancestor.exists():
            continue
        if ancestor.is_symlink():
            real = ancestor.resolve(strict=False)
            try:
                real.relative_to(proj_resolved)
            except ValueError:
                raise ValueError(
                    f"refusing to write: symlink at {ancestor} resolves "
                    f"to {real}, outside project {proj_resolved}."
                )
        if ancestor == proj_resolved:
            break  # don't walk past the project root


def _read_text_safely(path: Path) -> str | None:
    """Read a file's text, returning None on any IO/decode error.

    Used in write_nudge_file to detect "exists but unreadable" — we
    treat that as a write-from-scratch case rather than crashing the
    setup wizard.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
