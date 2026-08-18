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

#: Source and config files worth a verdict.
#:
#: This started as the handful of languages the graph parses, which quietly
#: made commit-time enforcement a no-op for most of the world: a Java or C#
#: team could lock a decision, install the hook, and never see it fire —
#: with nothing printed, so the silence read as "nothing was locked". Step
#: 8's done-when is "a locked decision blocks a commit in a non-Claude-Code
#: IDE", and decision_lock works off the decision's own `file_path`, not off
#: whether the graph can parse the file. So the list covers what people
#: actually commit; it exists only to skip binaries and lockfiles.
_TEXT_SUFFIXES = frozenset(
    {
        # graph-parsed
        ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
        ".go", ".rs",
        # JVM / .NET / native / scripting — decision_lock covers these fine
        ".java", ".kt", ".kts", ".scala", ".groovy",
        ".cs", ".fs", ".vb",
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
        ".m", ".mm", ".swift", ".rb", ".php", ".pl", ".lua", ".ex", ".exs",
        ".dart", ".r", ".jl", ".hs", ".clj", ".cljs", ".erl", ".zig",
        # markup / config / infra
        ".md", ".mdx", ".rst", ".txt",
        ".yaml", ".yml", ".toml", ".json", ".jsonc", ".ini", ".cfg", ".env",
        ".sh", ".bash", ".zsh", ".fish", ".ps1",
        ".sql", ".graphql", ".proto", ".tf", ".tfvars",
        ".html", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    }
)  # fmt: skip

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


def git_path(project_root: Path, name: str) -> Path | None:
    """Resolve a file inside the repo's git dir, or None.

    ``<root>/.git/<name>`` is WRONG in a linked worktree or a submodule,
    where ``.git`` is a FILE holding ``gitdir: <elsewhere>``. Asking git
    is the only thing that is right in every layout, and this repo uses
    worktrees for routine work — so the naive form was not a hypothetical
    edge case here.
    """
    out = _run(["git", "rev-parse", "--git-path", name], project_root).strip()
    if not out:
        return None
    p = Path(out)
    return p if p.is_absolute() else (project_root / p)


def is_merge_commit(project_root: Path) -> bool:
    """True during a merge. Never block one — see module docstring."""
    p = git_path(project_root, "MERGE_HEAD")
    return bool(p and p.exists())


def staged_files(
    project_root: Path, *, base: str | None = None, head: str | None = None
) -> tuple[list[str], int]:
    """``(reviewable changed paths, how many were dropped by the cap)``.

    Default (``base`` and ``head`` unset) reads the STAGED INDEX — the local
    pre-commit case, unchanged.

    Passing ``base``/``head`` reads a COMMIT RANGE instead. A pull request has
    no index, so CI cannot use ``--cached``; parameterising the diff source is
    what lets CI run the SAME evaluator rather than a second implementation
    that can drift out of agreement with the local hook.

    The cap is returned rather than applied silently. Enforcement is not
    sampling: if the file carrying a locked decision sorts past the cap,
    the change passes and an unreported truncation makes that
    indistinguishable from "nothing was locked". The caller says so on
    stderr.
    """
    if base and head:
        # Two-dot: exactly what changed between the two endpoints. The caller
        # supplies a merge-base for `base` when it wants PR semantics.
        cmd = ["git", "diff", "--name-only", "--diff-filter=d", base, head]
    else:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=d"]
    out = _run(cmd, project_root)
    files = [
        ln.strip()
        for ln in out.splitlines()
        if ln.strip() and Path(ln.strip()).suffix in _TEXT_SUFFIXES
    ]
    return files[:_MAX_FILES], max(0, len(files) - _MAX_FILES)


def _versions(
    project_root: Path,
    rel: str,
    *,
    base: str | None = None,
    head: str | None = None,
) -> tuple[str, str]:
    """(before, after) content for a path. Empty string when absent.

    Default is (HEAD, index) — the local pre-commit pair. With ``base``/``head``
    it is (base commit, head commit), which is the pull-request pair.
    """
    if base and head:
        return (
            _run(["git", "show", f"{base}:{rel}"], project_root),
            _run(["git", "show", f"{head}:{rel}"], project_root),
        )
    return (
        _run(["git", "show", f"HEAD:{rel}"], project_root),
        _run(["git", "show", f":{rel}"], project_root),
    )


def evaluate(
    project_root: Path,
    *,
    skipped: list[int] | None = None,
    base: str | None = None,
    head: str | None = None,
) -> list[PolicyVerdict]:
    """Run the engine over a change set. Never raises.

    Default: the STAGED INDEX (local pre-commit). With ``base``/``head``: a
    COMMIT RANGE, which is what a pull request is — CI has no index.

    ONE evaluator, two diff sources. A second implementation for CI would be
    free to drift out of agreement with the local hook, and a rule enforced in
    one path and dead in the other is the exact shape of the negation-guard
    bug: the guard existed, every test that drove it directly passed, and the
    path real writes took never called it.

    ``skipped`` — an out-param the caller passes to learn how many files the
    ``_MAX_FILES`` cap dropped, so it can say so rather than letting a
    truncated run read as a clean one.
    """
    verdicts: list[PolicyVerdict] = []
    try:
        # A merge commit is exempt only in the local/index case. Over a range
        # there is no "current commit" to inspect, and a PR branch containing
        # a merge must still be enforced.
        if not (base and head) and is_merge_commit(project_root):
            return verdicts
        from mcp_server.engine import register_default_policies
        from mcp_server.engine.runner import dispatch

        register_default_policies()

        files, dropped = staged_files(project_root, base=base, head=head)
        if skipped is not None:
            skipped.append(dropped)

        for rel in files:
            before, after = _versions(project_root, rel, base=base, head=head)
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


def handle(
    project_root: Path | None = None,
    *,
    base: str | None = None,
    head: str | None = None,
) -> int:
    """pre-commit entry point. 0 allows the change, 1 blocks it.

    ``base``/``head`` switch it from the staged index to a commit range, which
    is how CI evaluates a pull request through this same path.
    """
    import sys

    try:
        current = mode()
        if current == "off":
            return 0
        root = project_root or Path.cwd()

        skipped: list[int] = []
        verdicts = evaluate(root, skipped=skipped, base=base, head=head)
        blocking = [v for v in verdicts if v.action == "block"]

        # Say what was NOT looked at. A silent cap makes "we checked
        # everything and found nothing" and "we checked the first 40"
        # produce identical output, which is the more dangerous of the two
        # to be wrong about.
        dropped = skipped[0] if skipped else 0
        if dropped:
            sys.stderr.write(
                f"codevira: {dropped} more staged file(s) were NOT evaluated "
                f"(cap: {_MAX_FILES}). Commit in smaller batches to check "
                f"them, or review them by hand.\n"
            )

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

    # Ask git where the hooks live. `<root>/.git/hooks` is wrong in a linked
    # worktree or submodule (`.git` is a FILE there), and the old `.is_dir()`
    # guard turned that into a flat refusal — "not a git repository" printed
    # inside a directory that plainly is one. `--git-common-dir` is also the
    # correct answer for worktrees specifically: hooks are shared across
    # every worktree of a repo, not per-worktree.
    common = _run(["git", "rev-parse", "--git-common-dir"], root).strip()
    if not common:
        sys.stderr.write(
            f"Error: {root} is not a git repository.\n"
            "  Fix: run this inside a repo, or `git init` first.\n"
        )
        return 1
    git_common = Path(common)
    if not git_common.is_absolute():
        git_common = root / git_common
    hooks_dir = git_common / "hooks"

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
