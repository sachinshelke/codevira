"""
git_hooks.py — enforce decisions at the commit boundary (4.0 Step 8).

Codevira's enforcement has always lived in an IDE hook. README.md:99-101
says so plainly: only Claude Code's ``PreToolUse`` hard-blocks, because
only its edits route through the engine. In Cursor, Codex, Copilot and
Gemini a locked decision is advisory context the agent may decline to
read — 2 of ~7 supported surfaces actually enforce.

That is not a gap you close by asking six vendors for a hook contract.
Every one of those editors commits with git.

So this adapter feeds ``git diff --cached`` through the SAME
``dispatch()`` every IDE hook uses. No new policy, no second scoring
path, no vendor cooperation — and it covers editors that do not exist
yet. ``dispatch`` was already a pure ``(HookEvent) -> PolicyVerdict``;
this is the fourth caller.

Deliberate limits:

* **Merge commits are never blocked.** A merge is not an authored change,
  and blocking one leaves the user in a half-finished merge with no good
  move. Detected via ``MERGE_HEAD``.
* **``--no-verify`` is the escape hatch**, and it is git's, not ours. We
  do not reimplement an override.
* **Fail open.** Any error in this path allows the commit. A memory tool
  that wedges someone's commit on its own bug has done more damage than
  the decision it was protecting.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import PolicyVerdict

logger = logging.getLogger(__name__)

#: Only files the graph can reason about are worth a verdict.
_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".sh",
    }
)

#: Bound the work: a 300-file refactor should not spawn 300 evaluations
#: on a pre-commit hook. Blocking is about protecting decisions, and a
#: sample that large has already told us what we need.
_MAX_FILES = 40

_MODE_ENV = "CODEVIRA_GIT_HOOK_MODE"
_MODES = ("off", "warn", "block")
#: Sachin's call (D00012P): commit-time blocking is default ON.
_DEFAULT_MODE = "block"


def _run(args: list[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=10
        )
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def mode() -> str:
    raw = os.environ.get(_MODE_ENV, _DEFAULT_MODE).strip().lower()
    return raw if raw in _MODES else _DEFAULT_MODE


def is_merge_commit(project_root: Path) -> bool:
    """True during a merge. Never block one — see module docstring."""
    return (project_root / ".git" / "MERGE_HEAD").exists()


def staged_files(project_root: Path) -> list[str]:
    """Staged, non-deleted, reviewable paths — capped at ``_MAX_FILES``."""
    out = _run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=d"], project_root
    )
    files = [
        ln.strip()
        for ln in out.splitlines()
        if ln.strip() and Path(ln.strip()).suffix in _TEXT_SUFFIXES
    ]
    return files[:_MAX_FILES]


def _versions(project_root: Path, rel: str) -> tuple[str, str]:
    """(HEAD content, staged content) for a path. Empty string when absent."""
    before = _run(["git", "show", f"HEAD:{rel}"], project_root)
    after = _run(["git", "show", f":{rel}"], project_root)
    return before, after


def evaluate(project_root: Path) -> list[PolicyVerdict]:
    """Run the engine over the staged change set. Never raises."""
    verdicts: list[PolicyVerdict] = []
    try:
        if is_merge_commit(project_root):
            return verdicts
        from mcp_server.engine import register_default_policies
        from mcp_server.engine.runner import dispatch

        register_default_policies()

        for rel in staged_files(project_root):
            before, after = _versions(project_root, rel)
            if before == after:
                continue
            # `target_file` is NOT derived by HookEvent — every wiring
            # layer sets it explicitly, and a policy with target_file=None
            # silently allows. Missing this is why the first run of these
            # tests produced zero verdicts.
            event = HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=project_root,
                session_id=os.environ.get("CODEVIRA_SESSION_ID") or "git-commit",
                tool_name="Edit",
                tool_input={
                    "file_path": str(project_root / rel),
                    "old_string": before,
                    "new_string": after,
                },
                target_file=project_root / rel,
            )
            v = dispatch(event)
            if v.action in ("block", "warn"):
                verdicts.append(v)
    except Exception as exc:  # noqa: BLE001 — never wedge a commit
        logger.debug("git_hooks.evaluate failed (allowing commit): %s", exc)
        return []
    return verdicts


def handle(project_root: Path | None = None) -> int:
    """pre-commit entry point. 0 allows the commit, 1 blocks it."""
    import sys

    try:
        current = mode()
        if current == "off":
            return 0
        root = project_root or Path.cwd()

        verdicts = evaluate(root)
        blocking = [v for v in verdicts if v.action == "block"]
        if not verdicts:
            return 0

        for v in verdicts:
            sys.stderr.write((v.message or "") + "\n\n")

        if current == "block" and blocking:
            sys.stderr.write(
                "codevira blocked this commit. To proceed:\n"
                "  1. Surface the decision(s) above to whoever owns them.\n"
                "  2. If the decision should change, supersede it — do not\n"
                "     work around it silently.\n"
                "  3. To override once: git commit --no-verify\n"
                f"  4. To disable here: {_MODE_ENV}=warn (or =off)\n"
            )
            return 1
        return 0
    except Exception:  # noqa: BLE001
        return 0


#: Marker so we can recognise (and safely re-install) our own hook, and so
#: we never clobber a hook somebody else wrote.
_MARKER = "# >>> codevira pre-commit >>>"

_HOOK_BODY = f"""{_MARKER}
# Enforces locked codevira decisions at the commit boundary. Unlike the
# IDE hooks this works in EVERY editor, because they all commit with git.
# Override once: git commit --no-verify
# Disable here:  export {_MODE_ENV}=off
if command -v codevira >/dev/null 2>&1; then
  codevira engine pre-commit || exit 1
fi
# <<< codevira pre-commit <<<
"""


def install_hook(project_root: Path | None = None) -> int:
    """Install (or refresh) the pre-commit hook. Returns a shell exit code.

    Appends to an existing hook rather than replacing it — someone else's
    pre-commit is not ours to delete — and is idempotent via ``_MARKER``.
    """
    import sys

    root = project_root or Path.cwd()
    hooks_dir = root / ".git" / "hooks"
    if not (root / ".git").is_dir():
        sys.stderr.write(
            f"Error: {root} is not a git repository.\n"
            "  Fix: run this inside a repo, or `git init` first.\n"
        )
        return 1

    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"

    try:
        existing = hook.read_text(encoding="utf-8") if hook.is_file() else ""
    except OSError:
        existing = ""

    if _MARKER in existing:
        sys.stdout.write(f"✓ codevira pre-commit hook already installed at {hook}\n")
        return 0

    try:
        if existing.strip():
            # Preserve theirs, append ours.
            content = existing.rstrip("\n") + "\n\n" + _HOOK_BODY
            note = "appended to your existing pre-commit hook"
        else:
            content = "#!/bin/sh\n\n" + _HOOK_BODY
            note = "created"
        hook.write_text(content, encoding="utf-8")
        hook.chmod(0o755)
    except OSError as exc:
        sys.stderr.write(f"Error: could not write {hook}: {exc}\n")
        return 1

    sys.stdout.write(
        f"✓ codevira pre-commit hook {note}: {hook}\n"
        f"  Mode: {mode()} (set {_MODE_ENV}=warn or =off to change)\n"
        "  Override a single commit with: git commit --no-verify\n"
    )
    return 0
