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
  - nudge_file → marker-based replace via agents_md.write_nudge_file

A second `setup` invocation should produce all "no_change" actions on
a healthy install. The summary surfaces this so the user sees that
nothing was touched.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp_server.agents_md import (
    NudgeWriteResult,
    SUPPORTED_IDES as NUDGE_IDES,
    target_path_for as nudge_target_path_for,
    write_nudge_file,
)


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
    ("SessionStart",       "session_start.sh",       None),
    ("UserPromptSubmit",   "user_prompt_submit.sh",  None),
    ("PreToolUse",         "pre_tool_use.sh",        "Edit|Write|MultiEdit"),
    ("PostToolUse",        "post_tool_use.sh",       "Edit|Write|MultiEdit"),
    ("Stop",               "stop.sh",                None),
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
            1 for r in self.steps
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


def detect_targets(
    project_root: Path,
    *,
    only_ides: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Detect which AI tools are present on this machine.

    ``only_ides`` (if given) filters the detection result. Unknown IDEs
    in the filter raise ``ValueError`` — we want the user to see a
    typo immediately rather than silently producing an empty plan.
    """
    from mcp_server.ide_inject import detect_installed_ides

    detected = tuple(detect_installed_ides(project_root))

    if only_ides is None:
        return detected

    valid = set(detected) | set(NUDGE_IDES) | {"claude_desktop"}
    unknown = [i for i in only_ides if i not in valid]
    if unknown:
        raise ValueError(
            f"unknown IDE(s) in --ide: {unknown}. "
            f"Supported: {sorted(valid)}"
        )
    return tuple(i for i in only_ides if i in detected)


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
        steps.append(SetupStep(
            kind="mcp_config",
            ide=ide,
            target_path=config_path,
            target_path_existed=existed,
            will_merge=existed,
            preview=(
                f"Add codevira to {_ide_display_name(ide)} MCP config "
                f"({'merge' if existed else 'create'}: {config_path})"
            ),
        ))

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
        steps.append(SetupStep(
            kind="hook",
            ide="claude",
            target_path=target,
            target_path_existed=existed,
            will_merge=False,
            preview=(
                f"Install Claude Code {event_name} hook → {target.name}"
            ),
        ))

    # The settings.json registration step
    settings_path = Path.home() / ".claude" / "settings.json"
    steps.append(SetupStep(
        kind="hook",
        ide="claude",
        target_path=settings_path,
        target_path_existed=settings_path.exists(),
        will_merge=settings_path.exists(),
        preview=(
            f"Register codevira hooks in {settings_path.name} (merge)"
        ),
    ))

    return steps


def _plan_nudge_steps(
    project_root: Path,
    detected: tuple[str, ...],
) -> list[SetupStep]:
    """One nudge-file step per detected IDE that has a template.

    Always emit AGENTS.md as a tier-2 fallback so any MCP-compatible
    tool the user adds later inherits codevira behavior automatically
    (Linux Foundation standard).
    """
    steps: list[SetupStep] = []
    targeted: set[str] = set()
    resolved_root = project_root.resolve()

    for ide in detected:
        if ide in NUDGE_IDES:
            target = nudge_target_path_for(ide, project_root)
            existed = target.exists()
            try:
                rel_display = str(target.relative_to(resolved_root))
            except ValueError:
                rel_display = str(target)
            steps.append(SetupStep(
                kind="nudge_file",
                ide=ide,
                target_path=target,
                target_path_existed=existed,
                will_merge=existed,
                preview=(
                    f"Write codevira nudge for {_ide_display_name(ide)} "
                    f"→ {rel_display}"
                ),
            ))
            targeted.add(ide)

    # AGENTS.md as universal fallback (skip if already targeted via codex)
    if "agents_md" not in targeted and "codex" not in detected:
        target = nudge_target_path_for("agents_md", project_root)
        existed = target.exists()
        steps.append(SetupStep(
            kind="nudge_file",
            ide="agents_md",
            target_path=target,
            target_path_existed=existed,
            will_merge=existed,
            preview=f"Write AGENTS.md (universal fallback) → AGENTS.md",
        ))

    return steps


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
        except Exception as e:  # noqa: BLE001 — fall through; mcp_config steps will fail individually
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
        return StepResult(step, False, "failed", error=f"unknown step kind: {step.kind}")
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
        return StepResult(step, False, "failed",
                          error="codevira binary not found on PATH")

    if dry_run:
        return StepResult(step, True, "would_merge" if step.will_merge else "would_create")

    from mcp_server.ide_inject import (
        inject_global_claude_code, inject_global_cursor,
        inject_global_windsurf, inject_global_antigravity,
    )

    handler = {
        "claude": lambda: inject_global_claude_code(cmd_path, python_exe),
        "cursor": lambda: inject_global_cursor(cmd_path, python_exe),
        "windsurf": lambda: inject_global_windsurf(cmd_path, python_exe),
        "antigravity": lambda: inject_global_antigravity(cmd_path, python_exe),
    }.get(step.ide)
    if handler is None:
        return StepResult(step, True, "skipped",
                          error=f"no MCP-config handler for {step.ide}")

    handler()
    return StepResult(step, True, "merged" if step.will_merge else "created")


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
    source_filename = target_name[len("codevira-"):]

    source = Path(__file__).resolve().parent / "data" / "hooks" / source_filename
    if not source.exists():
        return StepResult(step, False, "failed",
                          error=f"bundled hook script missing: {source}")

    if dry_run:
        return StepResult(step, True,
                          "would_create" if not step.target_path_existed else "would_overwrite")

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
        step, True,
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
        return StepResult(step, True,
                          "would_merge" if step.target_path_existed else "would_create")

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
            return StepResult(step, False, "failed",
                              error=f"settings.json is not valid JSON")

    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = existing["hooks"] = {}

    changed = False
    for event_name, source_filename, matcher in _HOOK_EVENTS:
        target_script = Path.home() / ".claude" / "hooks" / f"codevira-{source_filename}"
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
    settings_path.write_text(serialized, encoding="utf-8")
    return StepResult(
        step, True,
        "merged" if step.target_path_existed else "created",
        bytes_written=len(serialized.encode("utf-8")),
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
    result: NudgeWriteResult = write_nudge_file(
        step.ide, project_root, dry_run=dry_run,
    )
    succeeded = True
    return StepResult(
        step, succeeded, result.action, bytes_written=result.bytes_written,
    )


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
    home = Path.home()
    if ide == "claude":
        return home / ".claude" / "settings.json"
    if ide == "cursor":
        return home / ".cursor" / "mcp.json"
    if ide == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    if ide == "antigravity":
        return home / ".gemini" / "settings.json"
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


# =====================================================================
# CLI orchestrator (cmd_setup)
# =====================================================================


def cmd_setup(
    *,
    yes: bool = False,
    dry_run: bool = False,
    only_ides: tuple[str, ...] | None = None,
    install_mcp: bool = True,
    install_hooks: bool = True,
    write_nudge_files: bool = True,
) -> int:
    """`codevira setup` orchestrator. Returns POSIX exit code:
        0 on success / dry-run / user-declined
        1 on bad project root or unrecoverable startup failure
        2 on partial failure (some steps succeeded, some failed)
    """
    started = time.perf_counter()

    # Stage 1
    project_root = resolve_setup_target()

    # Stage 2
    try:
        detected = detect_targets(project_root, only_ides=only_ides)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not detected:
        print("No supported AI tools detected on this machine.")
        print("Install Claude Code, Cursor, Windsurf, Antigravity, or Codex,")
        print("then re-run `codevira setup`.")
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
    print()


def _print_summary(result: ExecuteResult, *, elapsed_seconds: float) -> None:
    print()
    succeeded_count = sum(1 for r in result.steps if r.succeeded)
    failed_count = sum(1 for r in result.steps if not r.succeeded)
    no_change_count = sum(1 for r in result.steps if r.action == "no_change")

    if no_change_count == len(result.steps):
        print(f"  ✓ Already up to date ({len(result.steps)} steps, no changes needed).")
    elif failed_count == 0:
        print(f"  ✓ Done in {elapsed_seconds:.1f}s. {result.changes_made} changes; "
              f"{no_change_count} already current.")
    else:
        print(f"  ⚠  Partial: {succeeded_count} of {len(result.steps)} steps "
              f"succeeded ({elapsed_seconds:.1f}s).")
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
    """Yes/no prompt. Defaults to yes on bare Enter."""
    try:
        answer = input(f"  {question} [Y/n] ").strip().lower()
    except EOFError:
        # Non-interactive context: refuse to proceed without --yes.
        print()
        print("  Non-interactive shell — pass --yes to skip the prompt.")
        return False
    return answer in ("", "y", "yes")
