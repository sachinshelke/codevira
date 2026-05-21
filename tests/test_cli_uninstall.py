"""
test_cli_uninstall.py — v2.2.0 ``codevira uninstall`` command coverage.

Phase 5 of the 2026-05-22 surface-cut audit: every system write made
by ``codevira init`` and ``codevira setup`` must have a reverse path.
Without that, ``pipx uninstall codevira`` leaves ~15 system touch
points behind (claude.json, claude hooks, settings.json registrations,
in-repo .codevira/ dirs, AGENTS.md marker blocks).

Coverage matrix
===============

The unit tests below isolate the helpers so we can verify each kind
of write site is reversed correctly without standing up a full system:

  - _build_uninstall_plan: returns the right set of actions for a
    seeded fake home / fake repo, INCLUDING when --keep-data is set
  - _strip_agents_md_marker: preserves user content outside the
    <!-- codevira:begin --> / <!-- codevira:end --> boundaries
    byte-for-byte; deletes the file when only the codevira block
    existed; leaves a malformed marker alone
  - _remove_claude_hook_entries: drops codevira-tagged hooks from
    settings.json while preserving every unrelated registration
  - cmd_uninstall (dry-run): prints the plan and exits 0 without
    writing anything
  - cmd_uninstall (yes + execute): walks every action in the plan,
    reports successes / failures, and returns the right exit code

The hard e2e check (``codevira uninstall --help`` succeeds) lives
in tests/e2e/test_product_invariants.py::TestP7Reversible — this
file owns the per-helper invariants that prevent the helpers from
regressing once the e2e is green.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mcp_server.cli_uninstall import (
    _build_uninstall_plan,
    _remove_claude_hook_entries,
    _strip_agents_md_marker,
    cmd_uninstall,
)


# ---------------------------------------------------------------------------
# _strip_agents_md_marker
# ---------------------------------------------------------------------------


class TestStripAgentsMdMarker:
    def test_preserves_user_content_outside_marker(self, tmp_path: Path) -> None:
        """Anything outside <!-- codevira:begin --> .. <!-- codevira:end -->
        must survive byte-for-byte. This is the core promise — users will
        not run uninstall if it clobbers their hand-written AGENTS.md."""
        path = tmp_path / "AGENTS.md"
        path.write_text(
            "# My project\n"
            "\n"
            "Some user-written guidance.\n"
            "\n"
            "<!-- codevira:begin (auto-generated) -->\n"
            "Locked decision: D0001\n"
            "<!-- codevira:end -->\n"
            "\n"
            "## More user content\n"
            "\n"
            "Trailing notes the user added.\n",
            encoding="utf-8",
        )
        changed = _strip_agents_md_marker(path)
        assert changed is True
        result = path.read_text(encoding="utf-8")
        assert "Some user-written guidance." in result
        assert "Trailing notes the user added." in result
        assert "Locked decision: D0001" not in result
        assert "<!-- codevira:" not in result

    def test_deletes_file_when_only_codevira_block_existed(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "AGENTS.md"
        path.write_text(
            "<!-- codevira:begin -->\n" "managed content\n" "<!-- codevira:end -->\n",
            encoding="utf-8",
        )
        changed = _strip_agents_md_marker(path)
        assert changed is True
        assert not path.exists()

    def test_leaves_malformed_marker_alone(self, tmp_path: Path) -> None:
        """If we can't find a closing tag we MUST NOT damage the file."""
        path = tmp_path / "AGENTS.md"
        original = "Some content\n" "<!-- codevira:begin -->\n" "this never closes\n"
        path.write_text(original, encoding="utf-8")
        changed = _strip_agents_md_marker(path)
        assert changed is False
        assert path.read_text(encoding="utf-8") == original

    def test_no_marker_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "AGENTS.md"
        path.write_text("# Hand-written\n", encoding="utf-8")
        changed = _strip_agents_md_marker(path)
        assert changed is False
        assert path.read_text(encoding="utf-8") == "# Hand-written\n"


# ---------------------------------------------------------------------------
# _remove_claude_hook_entries
# ---------------------------------------------------------------------------


class TestRemoveClaudeHookEntries:
    def test_drops_codevira_hooks_keeps_others(self, tmp_path: Path) -> None:
        """Strip codevira-tagged hook commands; preserve every unrelated
        entry. Symmetric with the AGENTS.md preservation invariant."""
        path = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "~/.claude/hooks/codevira-pretooluse.sh",
                            },
                            {
                                "type": "command",
                                "command": "/usr/local/bin/my-other-hook.sh",
                            },
                        ]
                    }
                ],
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "~/.claude/hooks/codevira-posttooluse.sh",
                            },
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/local/bin/non-codevira.sh",
                            },
                        ]
                    }
                ],
            },
            "other_setting": "preserved",
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        changed = _remove_claude_hook_entries(path)
        assert changed is True
        out = json.loads(path.read_text(encoding="utf-8"))
        # PreToolUse: codevira matcher dropped; the user's matcher stays.
        cmds_pre = [
            m["command"] for d in out["hooks"]["PreToolUse"] for m in d["hooks"]
        ]
        assert cmds_pre == ["/usr/local/bin/my-other-hook.sh"]
        # PostToolUse was codevira-only — the entire event key is gone.
        assert "PostToolUse" not in out["hooks"]
        # Stop survived untouched.
        cmds_stop = [m["command"] for d in out["hooks"]["Stop"] for m in d["hooks"]]
        assert cmds_stop == ["/usr/local/bin/non-codevira.sh"]
        # Unrelated settings untouched.
        assert out["other_setting"] == "preserved"

    def test_no_codevira_hooks_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "user-hook.sh"}]}
                ]
            }
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        changed = _remove_claude_hook_entries(path)
        assert changed is False
        # File unchanged.
        out = json.loads(path.read_text(encoding="utf-8"))
        assert out == data


# ---------------------------------------------------------------------------
# _build_uninstall_plan
# ---------------------------------------------------------------------------


class TestBuildUninstallPlan:
    def test_empty_home_produces_empty_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no codevira artifacts on disk, the plan must be empty
        (so the command can short-circuit to 'system already clean')."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        plan = _build_uninstall_plan(keep_data=False)
        assert plan["actions"] == []

    def test_plan_includes_global_home_when_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cv_home = tmp_path / ".codevira"
        cv_home.mkdir()
        (cv_home / "global.db").write_text("fake")
        plan = _build_uninstall_plan(keep_data=False)
        ops = [(a["op"], a["path"]) for a in plan["actions"]]
        assert ("delete-dir", str(cv_home)) in ops

    def test_keep_data_skips_global_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--keep-data is the 'uninstall the binary, keep my decisions'
        workflow. Per-user data dir must NOT appear in the plan."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cv_home = tmp_path / ".codevira"
        cv_home.mkdir()
        (cv_home / "global.db").write_text("fake")
        plan = _build_uninstall_plan(keep_data=True)
        paths = [a["path"] for a in plan["actions"]]
        assert str(cv_home) not in paths

    def test_plan_includes_claude_hooks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        hooks_dir = tmp_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        for name in ("codevira-pretooluse.sh", "codevira-stop.sh", "user-hook.sh"):
            (hooks_dir / name).write_text("#!/bin/sh\n")
        plan = _build_uninstall_plan(keep_data=False)
        deleted_files = [
            Path(a["path"]).name for a in plan["actions"] if a["op"] == "delete-file"
        ]
        # codevira-* hooks scheduled for delete; user-hook.sh untouched.
        assert "codevira-pretooluse.sh" in deleted_files
        assert "codevira-stop.sh" in deleted_files
        assert "user-hook.sh" not in deleted_files


# ---------------------------------------------------------------------------
# cmd_uninstall — end-to-end on isolated home
# ---------------------------------------------------------------------------


class TestCmdUninstall:
    def test_dry_run_prints_plan_and_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run must list everything but touch nothing."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cv_home = tmp_path / ".codevira"
        cv_home.mkdir()
        (cv_home / "global.db").write_text("fake")
        out = io.StringIO()
        rc = cmd_uninstall(dry_run=True, yes=True, out=out)
        assert rc == 0
        text = out.getvalue()
        assert "[dry-run]" in text
        assert "delete-dir" in text
        # And critically — nothing was deleted.
        assert cv_home.exists()
        assert (cv_home / "global.db").exists()

    def test_empty_system_reports_clean(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Running uninstall on a fresh machine must not error — it
        reports 'Nothing to remove' and exits 0. P7 invariant."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        out = io.StringIO()
        rc = cmd_uninstall(dry_run=False, yes=True, out=out)
        assert rc == 0
        assert "Nothing to remove" in out.getvalue()

    def test_executes_deletions_when_yes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With --yes, every delete-dir / delete-file action must run."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cv_home = tmp_path / ".codevira"
        cv_home.mkdir()
        (cv_home / "global.db").write_text("fake")
        hooks_dir = tmp_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "codevira-pretooluse.sh").write_text("#!/bin/sh\n")
        (hooks_dir / "codevira-stop.sh").write_text("#!/bin/sh\n")
        # Unrelated hook that must survive
        (hooks_dir / "user-hook.sh").write_text("#!/bin/sh\n")

        out = io.StringIO()
        rc = cmd_uninstall(dry_run=False, yes=True, out=out)
        assert rc == 0
        # codevira artifacts gone
        assert not cv_home.exists()
        assert not (hooks_dir / "codevira-pretooluse.sh").exists()
        assert not (hooks_dir / "codevira-stop.sh").exists()
        # Unrelated artifact preserved
        assert (hooks_dir / "user-hook.sh").exists()


# ---------------------------------------------------------------------------
# CLI integration — `codevira uninstall --help` must succeed.
# This is what the P7 e2e gate (test_product_invariants.py) checks; we
# replicate it here as a unit-level safety net so a `cli.py` regression
# is caught without needing the binary installed.
# ---------------------------------------------------------------------------


class TestCliIntegration:
    def test_uninstall_appears_in_cli_help(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A regression where the subparser is removed must fail FAST.

        ``main()`` reads from ``sys.argv`` directly (not via parameter)
        per the project convention, so we monkeypatch argv instead of
        passing args. Mirrors how the P7 e2e gate exercises the binary."""
        from mcp_server.cli import main

        monkeypatch.setattr("sys.argv", ["codevira", "uninstall", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        # argparse exits 0 for --help.
        assert exc.value.code == 0
