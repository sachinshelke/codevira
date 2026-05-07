"""Hook-script resilience tests — Bug 18 regression guard.

The user reported that Claude Code blocked their UserPromptSubmit with:

    UserPromptSubmit operation blocked by hook:
    [bash /Users/sachin/.claude/hooks/codevira-user_prompt_submit.sh]:
    usage: codevira [-h] [--project-dir PATH] {init,index,...,clean} ...
    codevira: error: argument command: invalid choice: 'engine'
    (choose from init, index, status, report, serve, register, configure, clean)

That happens when an OLDER codevira (pre-v2.0) is on PATH while the
hook script is a v2.0 template. argparse exits nonzero, Claude Code
reads "operation blocked", the user can't even type a prompt.

The fix (rc.4 hardening): hook scripts capture codevira's stdout. If
it's not valid-looking JSON, they emit ``{"continue": true}`` and exit
0 — never block.

These tests verify each of the 5 hook scripts handles three failure
modes correctly:

  A. codevira binary missing entirely → no-op + exit 0
  B. codevira binary exists but is the WRONG binary (printing argparse
     errors to stderr / nothing to stdout) → no-op + exit 0
  C. codevira binary returns garbage on stdout (not JSON) → no-op + exit 0

And one happy path:

  D. codevira binary returns valid JSON → forward verbatim, preserve
     exit code (so legitimate exit-2 blocks still work — critical for
     PreToolUse).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


# Each hook + the event token it dispatches with.
_HOOKS = [
    ("session_start.sh", "SessionStart"),
    ("user_prompt_submit.sh", "UserPromptSubmit"),
    ("pre_tool_use.sh", "PreToolUse"),
    ("post_tool_use.sh", "PostToolUse"),
    ("stop.sh", "Stop"),
]


def _hooks_dir() -> Path:
    return Path(__file__).parent.parent / "mcp_server" / "data" / "hooks"


def _make_fake_codevira(tmp_path: Path, body: str) -> Path:
    """Create a fake `codevira` binary. ``body`` is the bash script
    body (after the shebang). Caller supplies stdout / stderr / exit
    code semantics."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codevira"
    fake.write_text("#!/usr/bin/env bash\n" + body)
    fake.chmod(stat.S_IRWXU)
    return fake


def _run_hook(
    hook_name: str,
    fake_bin_dir: Path,
    *,
    home_override: Path,
    stdin_payload: str = "{}",
) -> subprocess.CompletedProcess:
    """Run a hook script with a controlled PATH + HOME so it picks up
    our fake codevira instead of any system one. We keep /bin and
    /usr/bin on PATH so bash can find sh utilities; we just ensure the
    fake bin dir comes FIRST so any `command -v codevira` resolves
    to our fake (or returns nothing if the fake bin has no codevira)."""
    hook_path = _hooks_dir() / hook_name
    bash_path = shutil.which("bash") or "/bin/bash"
    env = {
        **os.environ,
        # Fake bin first; system dirs after for bash builtins. We
        # explicitly do NOT include the user's $HOME/.local/bin so any
        # real codevira install is hidden from the hook.
        "PATH": f"{fake_bin_dir}:/usr/bin:/bin",
        "HOME": str(home_override),
        # Disable the kill-switch fast-path — we want to exercise the
        # real failure-mode code paths.
        "CODEVIRA_ENGINE": "1",
    }
    return subprocess.run(
        [bash_path, str(hook_path)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Failure mode A — no codevira binary anywhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name,event", _HOOKS)
def test_hook_no_op_when_binary_missing(hook_name, event, tmp_path):
    """A hook with no codevira on PATH and no ~/.local/bin/codevira
    must emit ``{"continue": true}`` and exit 0."""
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    # Ensure no ~/.local/bin/codevira exists in fake home.
    assert not (empty_home / ".local" / "bin" / "codevira").exists()

    result = _run_hook(hook_name, empty_bin, home_override=empty_home)

    assert result.returncode == 0, (
        f"{hook_name}: must exit 0 when codevira missing — got "
        f"{result.returncode} (stderr: {result.stderr!r})"
    )
    payload = json.loads(result.stdout.strip())
    assert payload.get("continue") is True


# ---------------------------------------------------------------------------
# Failure mode B — stale codevira (argparse error to stderr)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name,event", _HOOKS)
def test_hook_no_op_when_binary_is_stale(hook_name, event, tmp_path):
    """A hook whose codevira binary is too old to support `engine`
    (argparse error to stderr, nonzero exit, EMPTY stdout) must NOT
    propagate the failure. Emit no-op + exit 0."""
    fake_bin = _make_fake_codevira(tmp_path, body=(
        # Mimic argparse: write to stderr, exit 2, no stdout.
        'echo "usage: codevira [-h] {init,index,clean} ..." 1>&2\n'
        'echo "codevira: error: argument command: invalid choice: \'engine\'" 1>&2\n'
        'exit 2\n'
    ))

    home = tmp_path / "home"
    home.mkdir()

    result = _run_hook(hook_name, fake_bin.parent, home_override=home)

    assert result.returncode == 0, (
        f"{hook_name}: stale-binary case must exit 0 — got "
        f"{result.returncode} (stdout: {result.stdout!r})"
    )
    payload = json.loads(result.stdout.strip())
    assert payload.get("continue") is True


# ---------------------------------------------------------------------------
# Failure mode C — garbage on stdout (corrupted binary, partial output)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name,event", _HOOKS)
def test_hook_no_op_when_stdout_is_not_json(hook_name, event, tmp_path):
    """A binary that prints something OTHER than JSON to stdout must
    not poison the hook response — fall back to no-op."""
    fake_bin = _make_fake_codevira(tmp_path, body=(
        'echo "this is not json"\n'
        'exit 0\n'
    ))

    home = tmp_path / "home"
    home.mkdir()

    result = _run_hook(hook_name, fake_bin.parent, home_override=home)

    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert payload.get("continue") is True


# ---------------------------------------------------------------------------
# Happy path D — valid JSON forwarded verbatim, exit code preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name,event", _HOOKS)
def test_hook_forwards_valid_json_with_allow_exit(hook_name, event, tmp_path):
    """When codevira returns valid JSON and exits 0, the hook must
    forward the JSON verbatim and exit 0."""
    fake_bin = _make_fake_codevira(tmp_path, body=(
        'printf \'{"continue": true, "engine": "live"}\\n\'\n'
        'exit 0\n'
    ))

    home = tmp_path / "home"
    home.mkdir()

    result = _run_hook(hook_name, fake_bin.parent, home_override=home)

    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert payload.get("continue") is True
    assert payload.get("engine") == "live", (
        "live engine response must reach Claude Code verbatim"
    )


def test_pre_tool_use_preserves_exit_2_block():
    """Critical: PreToolUse uses exit code 2 to block. The hook MUST
    propagate that exit code (not mask it as a no-op) when the engine
    legitimately wants to block a tool use. Otherwise Hero 1
    (Decision Lock) and friends are quietly disabled."""
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        fake_bin = _make_fake_codevira(tmp, body=(
            # Engine wants to block — emits valid JSON + exit 2.
            'printf \'{"continue": false, "reason": "decision locked"}\\n\'\n'
            'exit 2\n'
        ))
        home = tmp / "home"
        home.mkdir()
        result = _run_hook(
            "pre_tool_use.sh",
            fake_bin.parent,
            home_override=home,
        )
        assert result.returncode == 2, (
            "pre_tool_use must propagate engine's exit 2 (block) — "
            f"got {result.returncode}. Without this, every Hero policy "
            "that blocks via exit 2 is silently disabled."
        )
        payload = json.loads(result.stdout.strip())
        assert payload.get("continue") is False
        assert "decision locked" in payload.get("reason", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Kill-switch path (CODEVIRA_ENGINE=0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name,event", _HOOKS)
def test_hook_kill_switch_short_circuits(hook_name, event, tmp_path):
    """When CODEVIRA_ENGINE=0, the hook must short-circuit BEFORE
    looking for the binary — saves Python-startup tax even when codevira
    is installed."""
    # Use a fake binary that would FAIL noisily — the kill switch should
    # mean we never reach it.
    fake_bin = _make_fake_codevira(tmp_path, body=(
        'echo "engine should not have been invoked" 1>&2\n'
        'exit 99\n'
    ))
    home = tmp_path / "home"
    home.mkdir()

    bash_path = shutil.which("bash") or "/bin/bash"
    env = {
        **os.environ,
        "PATH": f"{fake_bin.parent}:/usr/bin:/bin",
        "HOME": str(home),
        "CODEVIRA_ENGINE": "0",
    }
    result = subprocess.run(
        [bash_path, str(_hooks_dir() / hook_name)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0
    assert "engine should not have been invoked" not in result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload.get("continue") is True
