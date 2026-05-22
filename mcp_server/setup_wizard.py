"""
setup_wizard.py — `codevira setup` orchestrator.

Pillar 1 of v2.0. One command that detects every AI tool on the machine
and configures Codevira for all of them: MCP server entries (where the
IDE supports them), Claude Code lifecycle hooks (where the IDE supports
them), and per-IDE nudge files (always).

Architecture
------------

The flow is **four discrete stages**:

  1. resolve_setup_target() — figure out which project root to use,
     validating against v1.8.1's $HOME guard.
  2. detect_targets() — which IDEs are installed (filtered by --ide
     if the user passed it).
  3. build_setup_plan() — pure data, no I/O writes. Returns a
     ``SetupPlan`` describing every file that would be touched.
  4. execute_plan() — apply the plan. Each step is wrapped in
     try/except; one failed step does not abort the rest.

Then `cmd_setup()` glues these together with prompts, summaries, and
exit codes.

Why "plan as data"?
-------------------

Because:
  - --dry-run runs stages 1-3 and skips 4.
  - Tests can build a plan + assert on its shape without touching disk.
  - The summary printed at the end can quote the same plan we built
    during preview, so the user gets exactly what they previewed.
  - Future hooks-installer / agents-md / mcp-config additions just
    add new ``SetupStepKind`` values — the orchestration stays put.

Idempotency
-----------

Every step kind has its own idempotency contract:
  - mcp_config → existing _merge_mcp_config logic (server entry only)
  - hook       → file-existence + hash compare; settings.json merge
  - nudge_file → marker-based block replace via
                 ``mcp_server.storage.agents_md_generator.regenerate``
                 (preserves user content outside the marker boundaries)

A second `setup` invocation should produce all "no_change" actions on
a healthy install. The summary surfaces this so the user sees that
nothing was touched.

v2.2.0+ (2026-05-22 surface-cut audit): the per-IDE nudge file matrix
(``CLAUDE.md``, ``GEMINI.md``, ``.cursor/rules/codevira.mdc``,
``.windsurfrules``, ``.github/copilot-instructions.md``) was DELETED.
Every modern AI tool reads the AGENTS.md (Linux Foundation) standard
natively; the per-IDE duplicates were pure surface bloat the audit
named as a churn driver. Today the wizard writes exactly ONE nudge
file: ``<project>/AGENTS.md`` (managed via codevira:begin/end markers,
user content outside preserved byte-for-byte).

The per-IDE **MCP config** writes are intentionally retained — they're
the cross-IDE memory wedge (without ``~/.cursor/mcp.json`` entries,
Cursor agents can't see decisions even if AGENTS.md tells them about
codevira). Per-IDE *nudges* were duplicates; per-IDE *MCP configs* are
the load-bearing surface.
"""

from __future__ import annotations

import json
import os as _os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# =====================================================================
# Plan data structures
# =====================================================================

#: Discrete kinds of work the wizard can do. Each is dispatched in
#: ``execute_plan`` to a small handler function.
StepKind = Literal["mcp_config", "hook", "nudge_file"]

#: The Claude Code lifecycle events we install hook scripts for. Order
#: matters for the user-facing summary (we print them top-down in this
#: order). Each row is ``(event_name, script_filename, matcher)`` where
#: ``matcher`` is the optional Claude Code regex that scopes which tool
#: invocations fire the hook.
#:
#: - **PreToolUse / PostToolUse**: matched to ``Edit|Write|MultiEdit``.
#:   Hero policies that consume these only care about file modifications;
#:   firing on Read/Bash/Glob would burn ~50 ms shell startup per call
#:   for nothing. Caught by Week-3 R8 (multi-IDE schema verification).
#: - **SessionStart / UserPromptSubmit / Stop**: no matcher — these
#:   events have no tool name, they fire once per session phase.
_HOOK_EVENTS: tuple[tuple[str, str, str | None], ...] = (
    ("SessionStart", "session_start.sh", None),
    ("UserPromptSubmit", "user_prompt_submit.sh", None),
    ("PreToolUse", "pre_tool_use.sh", "Edit|Write|MultiEdit"),
    ("PostToolUse", "post_tool_use.sh", "Edit|Write|MultiEdit"),
    ("Stop", "stop.sh", None),
)


@dataclass(frozen=True)
class SetupStep:
    """One discrete action the wizard plans to take.

    ``preview`` is what we show the user before they confirm. It must
    be short (≤200 chars) and human-readable.
    """

    kind: StepKind
    ide: str
    target_path: Path
    target_path_existed: bool
    will_merge: bool
    preview: str


@dataclass(frozen=True)
class SetupPlan:
    """Whole-wizard plan for one invocation."""

    project_root: Path
    project_name: str
    detected_ides: tuple[str, ...]
    steps: tuple[SetupStep, ...]
    install_mcp: bool
    install_hooks: bool
    write_nudge_files: bool


# =====================================================================
# Execution result
# =====================================================================


@dataclass(frozen=True)
class StepResult:
    """Outcome of executing one step."""

    step: SetupStep
    succeeded: bool
    action: str  # "created" / "no_change" / "block_replaced" / "merged" / "skipped" / "failed"
    error: str | None = None
    bytes_written: int = 0


@dataclass(frozen=True)
class ExecuteResult:
    """Whole-plan execution outcome."""

    plan: SetupPlan
    steps: tuple[StepResult, ...] = field(default_factory=tuple)

    @property
    def all_succeeded(self) -> bool:
        return all(r.succeeded for r in self.steps)

    @property
    def any_failed(self) -> bool:
        return any(not r.succeeded for r in self.steps)

    @property
    def changes_made(self) -> int:
        """Steps where we actually wrote something to disk."""
        return sum(
            1
            for r in self.steps
            if r.succeeded and r.action not in ("no_change", "skipped")
        )


# =====================================================================
# Stage 1: resolve project root
# =====================================================================


def resolve_setup_target() -> Path:
    """Resolve the project root for this `codevira setup` invocation.

    Reuses ``mcp_server.paths.get_project_root`` (which honors
    ``--project-dir``, then cwd-walk to the nearest project marker)
    and applies the v1.8.1 ``is_invalid_project_root`` guard.

    Raises:
        SystemExit(1) if the resolved path is $HOME, a system dir,
        or otherwise rejected. Prints a friendly message first.
    """
    from mcp_server.paths import get_project_root, is_invalid_project_root

    project_root = get_project_root()
    rejection = is_invalid_project_root(project_root)
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into your project directory first, then re-run "
            "`codevira setup`.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return project_root


# =====================================================================
# Stage 2: detect targets
# =====================================================================


#: IDEs codevira knows how to configure. Used to validate ``--ide``
#: arguments — unknown names trip an error before we plan anything.
_KNOWN_IDES: frozenset[str] = frozenset(
    {
        "claude",
        "claude_desktop",
        "cursor",
        "windsurf",
        "antigravity",
        # The agents_md sentinel covers the universal AGENTS.md write
        # (the only nudge file v3.0.0 still emits).
        "agents_md",
    }
)


def detect_targets(
    project_root: Path,
    *,
    only_ides: tuple[str, ...] | None = None,
    force: bool = False,
) -> tuple[str, ...]:
    """Detect which AI tools are present on this machine.

    ``only_ides`` (if given) narrows the configured-IDE set. The
    v3.0.0 contract has two cases:

    1. Unknown name (typo / a totally fictional IDE) → raise
       ``ValueError`` immediately. We've never silently dropped these.

    2. Known name but NOT in the auto-detected set (e.g. user passes
       ``--ide cursor`` on a machine where Cursor isn't installed) →
       raise ``ValueError`` UNLESS ``force=True``. The v2.x code
       silently filtered these out, which produced the worst possible
       UX: ``codevira setup --ide cursor`` exited 0 with no output and
       no config written.  Now the user gets a clear "we couldn't see
       it; pass --force if you know better."

    Passing ``force=True`` is the escape hatch for cases where the
    detector misses an install (e.g. a portable binary not on PATH,
    a non-standard config location). It's intentionally noisy so
    users only reach for it when needed.
    """
    from mcp_server.ide_inject import detect_installed_ides

    detected = tuple(detect_installed_ides(project_root))

    if only_ides is None:
        return detected

    # Stage 1 — reject unknown names (typos / never-supported IDEs).
    unknown = [i for i in only_ides if i not in _KNOWN_IDES]
    if unknown:
        raise ValueError(
            f"unknown IDE(s) in --ide: {unknown}. " f"Supported: {sorted(_KNOWN_IDES)}"
        )

    # Stage 2 — reject known-but-not-detected unless --force.
    # The `agents_md` sentinel is always "available" (we write
    # AGENTS.md regardless of detected IDE), so it never trips this
    # branch.
    undetected = [i for i in only_ides if i != "agents_md" and i not in detected]
    if undetected and not force:
        detected_str = ", ".join(sorted(detected)) or "(none)"
        raise ValueError(
            f"--ide named IDE(s) we couldn't auto-detect on this "
            f"machine: {undetected}. Detected: {detected_str}. "
            f"If you're sure they're installed (e.g. portable binary "
            f"not on PATH), re-run with --force to configure anyway."
        )

    return tuple(i for i in only_ides if i in detected or force)


# =====================================================================
# Stage 3: build plan (pure data, no I/O)
# =====================================================================


def build_setup_plan(
    project_root: Path,
    *,
    detected_ides: tuple[str, ...],
    install_mcp: bool = True,
    install_hooks: bool = True,
    write_nudge_files: bool = True,
    project_name: str | None = None,
) -> SetupPlan:
    """Pure-data plan builder. Inspects the filesystem (read-only) to
    determine which steps are required, but does not write anything.
    """
    if project_name is None:
        project_name = project_root.name

    steps: list[SetupStep] = []

    # 3a. MCP config steps — global mode for tier-1 IDEs
    if install_mcp:
        steps.extend(_plan_mcp_steps(detected_ides))

    # 3b. Lifecycle hook steps — Claude Code only today
    if install_hooks and "claude" in detected_ides:
        steps.extend(_plan_hook_steps())

    # 3c. Nudge file steps — every IDE we have a template for
    if write_nudge_files:
        steps.extend(_plan_nudge_steps(project_root, detected_ides))

    return SetupPlan(
        project_root=project_root.resolve(),
        project_name=project_name,
        detected_ides=tuple(detected_ides),
        steps=tuple(steps),
        install_mcp=install_mcp,
        install_hooks=install_hooks,
        write_nudge_files=write_nudge_files,
    )


def _plan_mcp_steps(detected: tuple[str, ...]) -> list[SetupStep]:
    """One step per IDE that has a global-mode MCP config target.

    Tier-2 IDEs (codex, copilot, continue, aider) are skipped at
    this stage — they don't have an MCP config injection path; the
    nudge file is their integration.
    """
    steps: list[SetupStep] = []

    for ide in detected:
        config_path = _mcp_config_path_for(ide)
        if config_path is None:
            continue
        existed = config_path.exists()
        steps.append(
            SetupStep(
                kind="mcp_config",
                ide=ide,
                target_path=config_path,
                target_path_existed=existed,
                will_merge=existed,
                preview=(
                    f"Add codevira to {_ide_display_name(ide)} MCP config "
                    f"({'merge' if existed else 'create'}: {config_path})"
                ),
            )
        )

    return steps


def _plan_hook_steps() -> list[SetupStep]:
    """One step per Claude Code lifecycle hook + one step for the
    settings.json hook registration.

    Hook scripts go to ``~/.claude/hooks/codevira-<event>.sh``.
    """
    hooks_dir = Path.home() / ".claude" / "hooks"
    steps: list[SetupStep] = []

    for event_name, source_filename, _matcher in _HOOK_EVENTS:
        target = hooks_dir / f"codevira-{source_filename}"
        existed = target.exists()
        steps.append(
            SetupStep(
                kind="hook",
                ide="claude",
                target_path=target,
                target_path_existed=existed,
                will_merge=False,
                preview=(f"Install Claude Code {event_name} hook → {target.name}"),
            )
        )

    # The settings.json registration step
    settings_path = Path.home() / ".claude" / "settings.json"
    steps.append(
        SetupStep(
            kind="hook",
            ide="claude",
            target_path=settings_path,
            target_path_existed=settings_path.exists(),
            will_merge=settings_path.exists(),
            preview=(f"Register codevira hooks in {settings_path.name} (merge)"),
        )
    )

    return steps


def _plan_nudge_steps(
    project_root: Path,
    detected: tuple[str, ...],
) -> list[SetupStep]:
    """One nudge-file step: AGENTS.md (regardless of IDE mix).

    v2.2.0+ (2026-05-22 surface-cut audit): the per-IDE matrix
    (``CLAUDE.md``, ``GEMINI.md``, ``.cursor/rules/codevira.mdc``,
    ``.windsurfrules``, ``.github/copilot-instructions.md``) was
    deleted — every modern AI tool reads ``AGENTS.md`` natively. The
    ``detected`` arg is preserved for signature stability but no
    longer drives the result.
    """
    target = project_root / "AGENTS.md"
    existed = target.is_file()
    return [
        SetupStep(
            kind="nudge_file",
            ide="agents_md",
            target_path=target,
            target_path_existed=existed,
            will_merge=existed,
            preview=(
                "Regenerate codevira block in AGENTS.md "
                f"({'update' if existed else 'create'} → AGENTS.md)"
            ),
        ),
    ]


# =====================================================================
# Stage 4: execute plan
# =====================================================================


def execute_plan(plan: SetupPlan, *, dry_run: bool = False) -> ExecuteResult:
    """Apply the plan. Each step is wrapped in try/except; one failure
    does not abort the others. Caller should print the summary.
    """
    results: list[StepResult] = []

    cmd_path: str | None = None
    python_exe: str | None = None
    if plan.install_mcp:
        try:
            from mcp_server.ide_inject import _resolve_command

            cmd_path, python_exe = _resolve_command()
        except Exception:  # noqa: BLE001 — fall through; mcp_config steps will fail individually
            cmd_path, python_exe = None, None

    for step in plan.steps:
        results.append(_execute_step(step, plan, cmd_path, python_exe, dry_run=dry_run))

    return ExecuteResult(plan=plan, steps=tuple(results))


def _execute_step(
    step: SetupStep,
    plan: SetupPlan,
    cmd_path: str | None,
    python_exe: str | None,
    *,
    dry_run: bool,
) -> StepResult:
    try:
        if step.kind == "mcp_config":
            return _execute_mcp_config(step, cmd_path, python_exe, dry_run=dry_run)
        if step.kind == "hook":
            return _execute_hook(step, dry_run=dry_run)
        if step.kind == "nudge_file":
            return _execute_nudge(step, plan.project_root, dry_run=dry_run)
        return StepResult(
            step, False, "failed", error=f"unknown step kind: {step.kind}"
        )
    except Exception as e:  # noqa: BLE001
        return StepResult(step, False, "failed", error=f"{type(e).__name__}: {e}")


def _execute_mcp_config(
    step: SetupStep,
    cmd_path: str | None,
    python_exe: str | None,
    *,
    dry_run: bool,
) -> StepResult:
    if cmd_path is None or python_exe is None:
        return StepResult(
            step, False, "failed", error="codevira binary not found on PATH"
        )

    if dry_run:
        return StepResult(
            step, True, "would_merge" if step.will_merge else "would_create"
        )

    from mcp_server.ide_inject import (
        inject_global_claude_code,
        inject_global_claude_desktop,
        inject_global_cursor,
        inject_global_windsurf,
        inject_global_antigravity,
    )

    handler = {
        "claude": lambda: inject_global_claude_code(cmd_path, python_exe),
        "claude_desktop": lambda: inject_global_claude_desktop(cmd_path, python_exe),
        "cursor": lambda: inject_global_cursor(cmd_path, python_exe),
        "windsurf": lambda: inject_global_windsurf(cmd_path, python_exe),
        "antigravity": lambda: inject_global_antigravity(cmd_path, python_exe),
    }.get(step.ide)
    if handler is None:
        return StepResult(
            step, True, "skipped", error=f"no MCP-config handler for {step.ide}"
        )

    # Detect ACTUAL content change so idempotent re-runs report
    # ``no_change`` rather than ``merged`` (which the summary counts
    # as a change). Caught by Week-4 integration round I1: a clean
    # re-run was reporting "4 changes" instead of "0 changes".
    before_bytes: bytes | None = None
    if step.target_path.exists():
        try:
            before_bytes = step.target_path.read_bytes()
        except OSError:
            before_bytes = None

    handler()

    after_bytes: bytes | None = None
    try:
        after_bytes = step.target_path.read_bytes()
    except OSError:
        after_bytes = None

    if before_bytes is not None and before_bytes == after_bytes:
        return StepResult(step, True, "no_change")
    if not step.will_merge:
        return StepResult(step, True, "created")
    return StepResult(step, True, "merged")


def _execute_hook(step: SetupStep, *, dry_run: bool) -> StepResult:
    """Install a Claude Code lifecycle hook script OR update
    settings.json registration.

    The step's target_path is either a hook script path under
    ``~/.claude/hooks/`` (in which case we copy from the bundled
    ``mcp_server/data/hooks/``) or it's settings.json (in which
    case we merge our hook entries into the existing config).
    """
    if step.target_path.name == "settings.json":
        return _install_hook_registrations(step, dry_run=dry_run)
    return _install_hook_script(step, dry_run=dry_run)


def _install_hook_script(step: SetupStep, *, dry_run: bool) -> StepResult:
    """Copy one bundled hook script to the user's ~/.claude/hooks dir."""
    # Recover source filename: target is "codevira-<source>.sh"
    target_name = step.target_path.name
    source_filename = target_name[len("codevira-") :]

    source = Path(__file__).resolve().parent / "data" / "hooks" / source_filename
    if not source.exists():
        return StepResult(
            step, False, "failed", error=f"bundled hook script missing: {source}"
        )

    if dry_run:
        return StepResult(
            step,
            True,
            "would_create" if not step.target_path_existed else "would_overwrite",
        )

    step.target_path.parent.mkdir(parents=True, exist_ok=True)

    # Idempotency: if the target already exists with identical content,
    # don't touch mtime.
    if step.target_path_existed:
        try:
            existing = step.target_path.read_bytes()
            new = source.read_bytes()
            if existing == new:
                # Still ensure executable bit (the user may have stripped it)
                _ensure_executable(step.target_path)
                return StepResult(step, True, "no_change")
        except OSError:
            pass

    shutil.copy2(source, step.target_path)
    _ensure_executable(step.target_path)
    return StepResult(
        step,
        True,
        "created" if not step.target_path_existed else "overwritten",
        bytes_written=step.target_path.stat().st_size,
    )


def _ensure_executable(path: Path) -> None:
    """Add owner-execute bit if missing. Idempotent."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _install_hook_registrations(step: SetupStep, *, dry_run: bool) -> StepResult:
    """Merge codevira's hook entries into ~/.claude/settings.json.

    Schema (per Claude Code docs):
        {"hooks": {"<EventName>": [{"hooks": [{"type": "command",
                                              "command": "..."}]}]}}

    We add ONE entry per event, with our command. If the user already
    has hooks for that event, we PREPEND ours (never replace).
    """
    if dry_run:
        return StepResult(
            step, True, "would_merge" if step.target_path_existed else "would_create"
        )

    settings_path = step.target_path
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            # Don't clobber unreadable settings — bail out as a soft fail
            return StepResult(
                step, False, "failed", error="settings.json is not valid JSON"
            )

    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = existing["hooks"] = {}

    changed = False
    for event_name, source_filename, matcher in _HOOK_EVENTS:
        target_script = (
            Path.home() / ".claude" / "hooks" / f"codevira-{source_filename}"
        )
        our_command = f"bash {target_script}"

        event_list = hooks.setdefault(event_name, [])
        if not isinstance(event_list, list):
            event_list = hooks[event_name] = []

        # Already registered? Look for our exact command in any entry's
        # nested ``hooks`` list, or as a top-level "command".
        if _hook_command_already_registered(event_list, our_command):
            continue

        # Build the entry. ``matcher`` (when present) scopes the hook
        # to specific tools (e.g. only Edit/Write/MultiEdit) — saves
        # ~50 ms shell startup on every Read/Bash/Glob invocation.
        # Caught by Week-3 R8 multi-IDE schema verification.
        entry: dict = {
            "hooks": [{"type": "command", "command": our_command}],
        }
        if matcher is not None:
            entry["matcher"] = matcher

        # Prepend so codevira fires first when multiple hooks are registered.
        event_list.insert(0, entry)
        changed = True

    if not changed:
        return StepResult(step, True, "no_change")

    serialized = json.dumps(existing, indent=2) + "\n"
    # Atomic write — Ctrl-C mid-write would otherwise corrupt
    # ~/.claude/settings.json and break Claude Code's startup until
    # the user manually fixes it. (I7 integration finding C.2.)
    n = _atomic_write_text(settings_path, serialized)
    return StepResult(
        step,
        True,
        "merged" if step.target_path_existed else "created",
        bytes_written=n,
    )


def _hook_command_already_registered(event_list: list, command: str) -> bool:
    """True if `command` appears anywhere in this event's hook list.

    Tolerates both the modern ``{"hooks": [{"command": ...}]}`` shape
    and the older flat ``{"command": ...}`` shape.
    """
    for entry in event_list:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for h in nested:
                if isinstance(h, dict) and h.get("command") == command:
                    return True
        if entry.get("command") == command:
            return True
    return False


def _execute_nudge(step: SetupStep, project_root: Path, *, dry_run: bool) -> StepResult:
    """Regenerate the codevira block in AGENTS.md.

    Delegates to ``mcp_server.storage.agents_md_generator.regenerate``
    which (a) reads the project's current decisions, (b) renders the
    slim ≤5 KB contract block, and (c) merges into AGENTS.md via
    ``<!-- codevira:begin -->`` / ``<!-- codevira:end -->`` markers
    (user content outside is preserved byte-for-byte).

    Idempotency contract: if the file already has the EXACT bytes
    we'd write, return ``no_change`` so back-to-back ``setup`` runs
    surface zero noise. This was the I1 finding for MCP config steps;
    nudge files get the same treatment.

    Dry-run path: report ``would_create`` / ``would_replace`` (the
    project-wide convention from ``_execute_mcp_config``); no I/O.
    """
    if dry_run:
        action = "would_replace" if step.target_path_existed else "would_create"
        return StepResult(step, True, action, bytes_written=0)

    # Snapshot before so we can detect a true no-op.
    before_bytes: bytes | None = None
    if step.target_path.is_file():
        try:
            before_bytes = step.target_path.read_bytes()
        except OSError:
            before_bytes = None

    try:
        from mcp_server.storage.agents_md_generator import regenerate

        summary = regenerate(target_path=step.target_path)
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            step,
            False,
            "failed",
            error=f"AGENTS.md regen failed: {exc}",
        )

    bytes_written = int(summary.get("block_bytes", 0))
    if before_bytes is not None:
        try:
            after_bytes = step.target_path.read_bytes()
        except OSError:
            after_bytes = b""
        if after_bytes == before_bytes:
            return StepResult(step, True, "no_change", bytes_written=0)
    action = "block_replaced" if step.target_path_existed else "created"
    return StepResult(step, True, action, bytes_written=bytes_written)


# =====================================================================
# Helpers
# =====================================================================


def _mcp_config_path_for(ide: str) -> Path | None:
    """Return the global MCP config path for an IDE, or None if the
    IDE doesn't support global-mode MCP config injection.

    Tier-1 IDEs with a global-mode helper return their config path.
    Tier-2 IDEs (codex/copilot/continue/aider) return None — their
    integration is the nudge file.
    """
    # IMPORTANT: these paths must match the locations the
    # ``mcp_server.ide_inject.inject_global_*`` helpers actually write
    # to. Mismatch would show the user a misleading preview AND make
    # idempotent-detection fail because we'd check the wrong file.
    # (Caught by Week-4 integration round I1 — the wizard claimed
    # ~/.gemini/settings.json but the inject function wrote to
    # ~/.gemini/antigravity/mcp_config.json.)
    from mcp_server.ide_inject import (
        _claude_global_config_path,
        _claude_desktop_config_path,
        _cursor_global_config_path,
        _windsurf_global_config_path,
        _antigravity_config_path,
    )

    if ide == "claude":
        return _claude_global_config_path()
    if ide == "claude_desktop":
        return _claude_desktop_config_path()
    if ide == "cursor":
        return _cursor_global_config_path()
    if ide == "windsurf":
        return _windsurf_global_config_path()
    if ide == "antigravity":
        return _antigravity_config_path()
    # Tier 2 — no MCP config injection
    return None


_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "claude_desktop": "Claude Desktop",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "antigravity": "Antigravity",
    "codex": "OpenAI Codex",
    "copilot": "GitHub Copilot",
    "continue": "Continue.dev",
    "aider": "Aider",
    "agents_md": "AGENTS.md (universal fallback)",
}


def _ide_display_name(ide: str) -> str:
    return _DISPLAY_NAMES.get(ide, ide)


def _atomic_write_text(target: Path, content: str) -> int:
    """Write ``content`` to ``target`` atomically.

    Strategy: write to a temp file in the SAME directory (so the
    rename is on the same filesystem and ``os.replace`` is atomic),
    then ``os.replace`` the temp into place. ``os.replace`` is atomic
    on POSIX and approximates atomicity on Windows (replaces the
    existing file in one call rather than the dangerous unlink-then-
    rename pattern).

    Why: a Ctrl-C between ``write_text`` chunks leaves the target
    half-written, and the next ``codevira setup`` run sees a corrupt
    file. The settings.json merge would either fail-open (lose the
    codevira hook block) or fail-closed (truncate user content).
    Caught by Week-1-through-4 integration round I7.

    Used by the settings.json merge step. Inlined here in v2.2.0+
    after the 2026-05-22 surface-cut audit deleted the legacy
    ``mcp_server/agents_md.py`` module that previously owned this
    helper. Setup wizard is the only remaining caller.

    Returns the number of bytes written.
    """
    encoded = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)

    # Use mkstemp in the same dir as target so the rename stays on
    # one filesystem.
    fd, _raw_tmp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path: str | None = _raw_tmp
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(encoded)
            assert tmp_path is not None
            # Flush to disk before rename so a crash doesn't leave the
            # destination pointing at empty content.
            f.flush()
            try:
                _os.fsync(f.fileno())
            except OSError:
                pass  # some filesystems / containers don't support fsync
        _os.replace(tmp_path, target)
        tmp_path = None  # ownership transferred — don't clean up below
    finally:
        if tmp_path is not None:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
    return len(encoded)


# =====================================================================
# CLI orchestrator (cmd_setup)
# =====================================================================


def cmd_setup(
    *,
    yes: bool = False,
    dry_run: bool = False,
    only_ides: tuple[str, ...] | None = None,
    force: bool = False,
    install_mcp: bool = True,
    install_hooks: bool = True,
    write_nudge_files: bool = True,
) -> int:
    """`codevira setup` orchestrator. Returns POSIX exit code:
    0 on success / dry-run / user-declined
    1 on bad project root or unrecoverable startup failure
    2 on partial failure (some steps succeeded, some failed)

    v3.0.0 contract: by default the wizard ONLY configures IDEs whose
    install is auto-detected on this machine. ``--ide X`` for a
    non-detected IDE raises a clear error pointing at ``--force`` as
    the override. The v2.x silent-filter behavior (which made
    ``setup --ide cursor`` on a Cursor-less machine succeed with no
    output and no config) was deleted in the surface-cut audit.
    """
    started = time.perf_counter()

    # Stage 1
    project_root = resolve_setup_target()

    # Stage 2
    try:
        detected = detect_targets(project_root, only_ides=only_ides, force=force)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "  → check your --ide value against `codevira setup --help`. "
            "Valid IDEs: claude, cursor, windsurf, antigravity, agents_md.",
            file=sys.stderr,
        )
        return 1

    if not detected:
        print("No supported AI tools detected on this machine.")
        print("Install Claude Code, Cursor, Windsurf, Antigravity, or Codex,")
        print("then re-run `codevira setup`. To configure an IDE we missed,")
        print("pass `--ide <name> --force`.")
        return 0

    # Stage 3
    plan = build_setup_plan(
        project_root,
        detected_ides=detected,
        install_mcp=install_mcp,
        install_hooks=install_hooks,
        write_nudge_files=write_nudge_files,
    )

    _print_plan(plan)

    # User confirmation
    if dry_run:
        print()
        print("(dry run) — no changes made.")
        return 0
    if not yes:
        if not _confirm("Proceed?"):
            print("No changes made.")
            return 0

    # Stage 4
    result = execute_plan(plan)

    # Summary + exit code
    elapsed = time.perf_counter() - started
    _print_summary(result, elapsed_seconds=elapsed)

    if result.any_failed:
        return 2
    return 0


def _print_plan(plan: SetupPlan) -> None:
    print()
    print(f"  Codevira setup — {plan.project_name}")
    print("  " + "─" * 44)
    print(f"  Detected: {', '.join(_ide_display_name(i) for i in plan.detected_ides)}")
    print()
    print(f"  Plan ({len(plan.steps)} steps):")
    for step in plan.steps:
        print(f"    • {step.preview}")
    # P1-6 (rc.5): if every feature flag was disabled the plan is empty.
    # Surface that loudly so the user knows nothing will happen instead of
    # silently reporting "Already up to date".
    if not plan.steps:
        print()
        print(
            "  ⚠  Plan is empty — all of --no-mcp / --no-hooks / --no-nudge-files "
            "appear to be set."
        )
        print("     Re-run without those flags to actually configure your IDE(s).")
    print()


def _ghost_advisory_for_current_project() -> str:
    """P2-5 (rc.5): if the current project's data_dir is a 'ghost' shape
    (graph dir exists but config.yaml/metadata.json missing), return a
    one-line advisory the caller can print after the "IDE setup up to
    date" line. Returns empty string when the project is healthy.
    """
    try:
        from mcp_server.paths import get_data_dir

        d = get_data_dir()
        if not d.is_dir():
            return ""
        has_config = (d / "config.yaml").is_file()
        has_metadata = (d / "metadata.json").is_file()
        if has_config and has_metadata:
            # Also check graph has nodes (ghost variant where graph empty)
            graph_db = d / "graph" / "graph.db"
            if not graph_db.is_file():
                return "this project has no graph index yet — run `codevira index`."
            return ""
        # Missing one of config/metadata → classic ghost.
        return (
            "this project's data dir is incomplete (missing config.yaml or "
            "metadata.json). Run `codevira init` to populate it, or "
            "`codevira projects --ghosts-only` to see the full picture."
        )
    except Exception:
        return ""


def _print_summary(result: ExecuteResult, *, elapsed_seconds: float) -> None:
    print()
    succeeded_count = sum(1 for r in result.steps if r.succeeded)
    failed_count = sum(1 for r in result.steps if not r.succeeded)
    no_change_count = sum(1 for r in result.steps if r.action == "no_change")

    if no_change_count == len(result.steps):
        # P2-5 (rc.5): setup is "up to date" only at the IDE-config level.
        # The current project may still be a ghost (no config.yaml / no graph).
        # Inspect the current project's data dir and warn if it's incomplete.
        ghost_note = _ghost_advisory_for_current_project()
        if ghost_note:
            print(f"  ✓ IDE setup up to date ({len(result.steps)} steps, no changes).")
            print(f"  ⚠  However: {ghost_note}")
        else:
            print(
                f"  ✓ Already up to date ({len(result.steps)} steps, no changes needed)."
            )
    elif failed_count == 0:
        print(
            f"  ✓ Done in {elapsed_seconds:.1f}s. {result.changes_made} changes; "
            f"{no_change_count} already current."
        )
    else:
        print(
            f"  ⚠  Partial: {succeeded_count} of {len(result.steps)} steps "
            f"succeeded ({elapsed_seconds:.1f}s)."
        )
        for r in result.steps:
            if not r.succeeded:
                print(f"    ✗ {r.step.preview}")
                if r.error:
                    print(f"      → {r.error}")
        print()
        print("  Re-run `codevira setup` to retry the failed steps.")

    # Restart hint when we just touched Claude Code hooks
    if any(
        r.step.kind == "hook" and r.action not in ("no_change", "skipped", "failed")
        for r in result.steps
    ):
        print()
        print("  Restart Claude Code to pick up the new lifecycle hooks.")
    print()


def _confirm(question: str) -> bool:
    """Yes/no prompt. Defaults to yes on bare Enter.

    Bug 22 (rc.4): delegates to the shared :func:`mcp_server._prompts.confirm`
    helper so the prompt flushes stdout, loops on unrecognized input, and
    handles Ctrl+C cleanly instead of silently returning False for any
    non-matching answer (which surfaced as "I typed Y and nothing happened").
    """
    from mcp_server._prompts import confirm

    return confirm(question, default=True)
