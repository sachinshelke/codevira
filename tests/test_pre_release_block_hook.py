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
    assert result.returncode == _BLOCK, (
        f"hook let a release through: {command!r}\n  stdout: {result.stdout}"
    )
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


def _evidence(tmp_path, **overrides):
    """Write an all-passing evidence file, with fields overridden."""
    gates = {
        "G1_unit_tests": True,
        "G2_first_contact": True,
        "G3_real_ide_smoke": True,
        "G4_crash_log_clean": True,
        "G5_human_confirmed": True,
    }
    gates.update(overrides)
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "9.9.9.json").write_text(json.dumps(gates))
    return evidence_dir


def test_allows_release_when_evidence_shows_all_gates_pass(tmp_path):
    # This test used to set G3 to "skipped" and assert ALLOW, under a name
    # claiming all gates passed. It was pinning the defect: a gate that did
    # not run recorded the same verdict as one that ran and passed.
    evidence_dir = _evidence(tmp_path, G4_crash_log_clean="warn")

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _ALLOW, result.stderr
    assert (evidence_dir / "audit.log").exists(), "allowed release must be audited"


# ---------------------------------------------------------------------------
# A gate that could not run is not a gate that passed.
#
# G3 read "skipped" in every evidence file from 2.0.0 through 3.0.0 — the
# real-IDE smoke script was a stub returning exit 2 — and the hook allowed
# all six uploads. The Makefile printed "NOT a release-ready state" next to
# it each time. Nothing enforced that sentence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gate", ["G1_unit_tests", "G2_first_contact", "G3_real_ide_smoke"]
)
def test_a_skipped_gate_is_not_a_pass(gate, tmp_path):
    _evidence(tmp_path, **{gate: "skipped"})

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK, f"{gate}=skipped was allowed through"
    assert gate in result.stderr, result.stderr


def test_g4_warn_is_tolerated_but_g4_skipped_is_not(tmp_path):
    """The one documented exception, and its boundary.

    A crash log with entries does not block: the release may be what fixes
    them. A crash log that could not be READ is a different thing — it is
    the could-not-check state, and it blocks like any other.
    """
    _evidence(tmp_path, G4_crash_log_clean="warn")
    assert _run("twine upload dist/*", tmp_path).returncode == _ALLOW

    _evidence(tmp_path, G4_crash_log_clean="skipped")
    result = _run("twine upload dist/*", tmp_path)
    assert result.returncode == _BLOCK
    assert "G4_crash_log_clean" in result.stderr


def test_a_gate_the_hook_never_heard_of_is_still_enforced(tmp_path):
    """The anti-rot property, and the reason the check enumerates.

    The hook named five gates. Four more were added to the gauntlet in
    2.1.2 — G1.5, G1.6, G1.7, G2.5 — and were never added here, so seven
    releases went out with the wall enforcing a subset of itself. A gate
    the hook has no special knowledge of must still be able to fail it.
    """
    _evidence(tmp_path, G6_a_gate_added_after_this_test_was_written=False)

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK, (
        "a failing gate the hook does not know by name was allowed through — "
        "the hardcoded-list rot is back"
    )
    assert "G6_a_gate_added_after_this_test_was_written" in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        "G1_unit_tests",
        "G2_first_contact",
        "G3_real_ide_smoke",
        "G4_crash_log_clean",
        "G5_human_confirmed",
    ],
)
def test_a_gate_absent_from_the_evidence_blocks(missing, tmp_path):
    """Enumerating only what is present would let {} pass with zero
    failures. Absence is the strongest form of could-not-check."""
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir(exist_ok=True)
    gates = {
        "G1_unit_tests": True,
        "G2_first_contact": True,
        "G3_real_ide_smoke": True,
        "G4_crash_log_clean": True,
        "G5_human_confirmed": True,
    }
    del gates[missing]
    (evidence_dir / "9.9.9.json").write_text(json.dumps(gates))

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK
    assert missing in result.stderr and "absent" in result.stderr, result.stderr


def test_an_empty_evidence_file_blocks(tmp_path):
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "9.9.9.json").write_text("{}")

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK


def test_the_block_message_names_the_gate_that_actually_failed(tmp_path):
    """It used to say "Specifically, G5 ..." whatever had failed, sending
    the maintainer to confirm a gate that was already true."""
    _evidence(tmp_path, G2_first_contact=False)

    result = _run("twine upload dist/*", tmp_path)

    assert result.returncode == _BLOCK
    assert "G2_first_contact" in result.stderr, result.stderr


def test_provenance_fields_are_not_mistaken_for_gates(tmp_path):
    """2.1.2 evidence carries G5_confirmed_at / G5_confirmed_by strings.
    They start with G5 but are not verdicts; reading them as gates would
    block every release that records who confirmed it."""
    evidence_dir = tmp_path / ".release-evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "9.9.9.json").write_text(
        json.dumps(
            {
                "G1_unit_tests": True,
                "G2_first_contact": True,
                "G3_real_ide_smoke": True,
                "G4_crash_log_clean": True,
                "G5_human_confirmed": True,
                "G5_confirmed_at": "2026-09-10T00:00:00Z",
                "G5_confirmed_by": "sachin",
            }
        )
    )

    assert _run("twine upload dist/*", tmp_path).returncode == _ALLOW


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
