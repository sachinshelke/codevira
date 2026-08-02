"""Tests for ``.claude/hooks/pre-release-block.sh`` — the PreToolUse hard
wall against unverified releases.

Two properties matter, and they pull in opposite directions:

  1. It must BLOCK a real release attempt that has no gauntlet evidence.
  2. It must NOT block a command that merely *mentions* a release in
     quoted text.

Property 2 was broken: the matcher substring-searched the entire Bash
command string, so on 2026-08-02 this was refused —

    git commit -m "$(cat <<'EOF'
    docs: explain why the hook blocks `twine upload`
    EOF
    )"

— because the prose inside the heredoc contained the blocked token. That
is worse than an annoyance: the block message advises
``CODEVIRA_RELEASE_OVERRIDE=1``, so every false positive trains the
maintainer to reach for the override. A guard that cries wolf gets
bypassed reflexively, and then it isn't a wall any more.

The fix matches on the *command being executed* (heredoc bodies stripped,
then adjacent shell tokens) instead of on raw text. These tests pin both
directions.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _REPO_ROOT / ".claude" / "hooks" / "pre-release-block.sh"

# Exit codes in the Claude Code hook contract.
_ALLOW = 0
_BLOCK = 2


pytestmark = pytest.mark.skipif(
    not _HOOK.exists(),
    reason=".claude/hooks/ is not shipped in the sdist — repo-only test",
)


def _run(command, tmp_path, *, tool_name="Bash", env_extra=None):
    """Pipe a tool-call payload to the hook the way Claude Code does.

    ``CLAUDE_PROJECT_DIR`` points at ``tmp_path`` so the block/allow paths
    read a throwaway pyproject.toml and write their audit logs there —
    never into the real repo.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')

    env = {k: v for k, v in os.environ.items() if k != "CODEVIRA_RELEASE_OVERRIDE"}
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env.update(env_extra or {})

    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


# ---------------------------------------------------------------------------
# False positives — a command that only *talks* about a release
# ---------------------------------------------------------------------------

# The exact shape that was wrongly blocked: release tokens living in a
# heredoc that becomes a git commit message.
_COMMIT_WITH_RELEASE_PROSE = """git commit -m "$(cat <<'EOF'
docs(hooks): explain the pre-release wall

The hook refuses `twine upload` and `make release-publish` until the
gauntlet has left evidence in .release-evidence/.
EOF
)"
"""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_COMMIT_WITH_RELEASE_PROSE, id="heredoc-commit-message"),
        pytest.param(
            'git commit -m "docs: explain why twine upload is blocked"',
            id="inline-quoted-message",
        ),
        pytest.param(
            "git commit -m 'chore: wire make release-publish into CI'",
            id="single-quoted-message",
        ),
        pytest.param(
            'echo "run twine upload only after the gauntlet" > NOTES.md',
            id="quoted-echo",
        ),
        pytest.param(
            "grep -rn 'twine upload' .claude/hooks/",
            id="grep-for-the-token",
        ),
        pytest.param("ls -la", id="unrelated-command"),
    ],
)
def test_allows_commands_that_only_mention_a_release(command, tmp_path):
    result = _run(command, tmp_path)
    assert result.returncode == _ALLOW, (
        "hook blocked a command that only mentions a release in quoted "
        f"text:\n  command: {command!r}\n  stderr: {result.stderr}"
    )


def test_allows_non_bash_tool_calls(tmp_path):
    result = _run("twine upload dist/*", tmp_path, tool_name="Write")
    assert result.returncode == _ALLOW


# ---------------------------------------------------------------------------
# True positives — an actual release attempt, no evidence on disk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("twine upload dist/*", id="twine"),
        pytest.param("python3 -m twine upload dist/*", id="python-m-twine"),
        pytest.param("uv run twine upload dist/*", id="runner-prefixed-twine"),
        pytest.param('bash -c "twine upload dist/*"', id="nested-shell"),
        pytest.param("make release-publish", id="make-target"),
        pytest.param("pipx publish", id="pipx"),
        pytest.param("gh release create v9.9.9 --draft=false", id="gh-release-create"),
        pytest.param("gh release edit v9.9.9 --draft=false", id="gh-release-edit"),
        # Prose AND a real release in one command: the prose must not
        # launder the release.
        pytest.param(
            'git commit -m "docs: note the twine upload wall" && twine upload dist/*',
            id="prose-then-real-release",
        ),
    ],
)
def test_blocks_real_release_without_evidence(command, tmp_path):
    result = _run(command, tmp_path)
    assert (
        result.returncode == _BLOCK
    ), f"hook let a release through: {command!r}\n  stdout: {result.stdout}"
    assert "RELEASE BLOCKED" in result.stderr


# ---------------------------------------------------------------------------
# `#` — a comment to bash, a word character mid-word
# ---------------------------------------------------------------------------
#
# Python's shlex ends a token at ANY unquoted `#` and discards the rest of
# the line. bash only starts a comment when `#` begins a word, so `a#b` is
# a literal word. Taking shlex at its word meant `echo a#b && twine upload`
# tokenized to ['echo', 'a'] — the release silently dropped off the end.
# The hook strips comments itself, quote-aware, and turns shlex's own
# comment handling off.


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "echo a#b && twine upload dist/*",
            id="hash-inside-a-word-does-not-hide-what-follows",
        ),
        pytest.param(
            "curl 'https://example.com/p#frag' && twine upload dist/*",
            id="hash-inside-quotes",
        ),
        pytest.param(
            "# build first, then publish\ntwine upload dist/*",
            id="comment-line-above-the-release",
        ),
        pytest.param(
            "echo done;# inline\ntwine upload dist/*",
            id="comment-after-a-separator",
        ),
    ],
)
def test_hash_does_not_hide_a_release(command, tmp_path):
    result = _run(command, tmp_path)
    assert result.returncode == _BLOCK, (
        f"a `#` let a release slip past the hook: {command!r}\n"
        f"  stdout: {result.stdout}"
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "git push  # then run twine upload dist/*",
            id="release-named-in-a-real-comment",
        ),
        pytest.param(
            "# reminder: make release-publish needs evidence\nls -la",
            id="whole-line-comment",
        ),
        pytest.param(
            'git commit -m "fix #123: document the twine upload wall"',
            id="hash-in-a-commit-message",
        ),
    ],
)
def test_a_release_named_in_a_comment_is_still_only_prose(command, tmp_path):
    result = _run(command, tmp_path)
    assert result.returncode == _ALLOW, (
        "hook blocked a command whose only mention of a release is a "
        f"comment:\n  command: {command!r}\n  stderr: {result.stderr}"
    )


def test_gh_release_create_without_draft_false_is_allowed(tmp_path):
    """A draft release is not a publish — matches codevira.discipline.yaml,
    which blocks `gh release create` only when --draft=false is passed."""
    result = _run("gh release create v9.9.9 --draft", tmp_path)
    assert result.returncode == _ALLOW


# ---------------------------------------------------------------------------
# The wall still opens for a verified release
# ---------------------------------------------------------------------------


def test_allows_release_when_evidence_shows_all_gates_pass(tmp_path):
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "9.9.9.json").write_text(
        json.dumps(
            {
                "G1_unit_tests": True,
                "G2_first_contact": True,
                "G3_real_ide_smoke": "skipped",
                "G4_crash_log_clean": "warn",
                "G5_human_confirmed": True,
            }
        )
    )

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _ALLOW, result.stderr
    assert (evidence_dir / "audit.log").exists(), "allowed release must be audited"


def test_blocks_release_when_g5_not_confirmed(tmp_path):
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "9.9.9.json").write_text(
        json.dumps(
            {
                "G1_unit_tests": True,
                "G2_first_contact": True,
                "G3_real_ide_smoke": True,
                "G4_crash_log_clean": True,
                "G5_human_confirmed": False,
            }
        )
    )

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK
    assert "G5" in result.stderr


def test_override_is_allowed_and_logged(tmp_path):
    result = _run(
        "twine upload dist/*",
        tmp_path,
        env_extra={"CODEVIRA_RELEASE_OVERRIDE": "1"},
    )

    assert result.returncode == _ALLOW, result.stderr
    log = tmp_path / ".release-evidence" / "overrides.log"
    assert log.exists(), "override must leave an audit trail"
    assert "twine upload" in log.read_text()


# ---------------------------------------------------------------------------
# Keep the hook and codevira.discipline.yaml in sync (they are deliberately
# duplicated for defense-in-depth, so drift is silent by construction).
# ---------------------------------------------------------------------------


def test_every_blocked_command_in_yaml_is_blocked_by_the_hook(tmp_path):
    """Each `block_commands:` entry, run as a real command, must be
    refused. Catches the case where someone adds a command to the YAML
    and forgets the hook's hardcoded list (or vice versa)."""
    yaml_path = _REPO_ROOT / "codevira.discipline.yaml"
    yaml = pytest.importorskip("yaml")
    block_commands = yaml.safe_load(yaml_path.read_text())["block_commands"]

    for entry in block_commands:
        # `gh release create` is documented as blocked only alongside
        # --draft=false; make the entry a complete command.
        command = entry
        if command.startswith("gh release") and "--draft" not in command:
            command += " v9.9.9 --draft=false"

        result = _run(command, tmp_path)
        assert result.returncode == _BLOCK, (
            f"codevira.discipline.yaml lists {entry!r} under block_commands, "
            "but .claude/hooks/pre-release-block.sh does not block it"
        )
