"""
test_setup_wizard.py — Pillar 1 acceptance tests.

The 10 scenarios listed in docs/heroes/pillar-1-setup.md "Acceptance
test list", plus a few directly-targeted unit tests for the plan
data structures.

Tests use ``tmp_path`` plus monkey-patched ``Path.home()`` to keep
the real ~/.claude / ~/.cursor / etc. untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from mcp_server import setup_wizard
from mcp_server.setup_wizard import (
    SetupPlan,
    SetupStep,
    build_setup_plan,
    detect_targets,
    execute_plan,
    resolve_setup_target,
)


# =====================================================================
# Test fixture: redirect HOME and project_root to tmp_path
# =====================================================================


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run with HOME and project_root pinned under tmp_path.

    Yields the project_root directory.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    project = tmp_path / "myproject"
    project.mkdir()
    # Plant a project marker so get_project_root finds it (in case
    # any code path falls through to that helper).
    (project / "pyproject.toml").write_text("")

    # Pin the cwd to the project so that any unscoped get_project_root
    # call resolves there.
    monkeypatch.chdir(project)

    yield project


# =====================================================================
# Acceptance scenario #2: Idempotent re-run
# =====================================================================


class TestIdempotency:
    def test_second_run_reports_no_change(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Pretend Claude Code is installed
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: ["claude"],
        )

        plan1 = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,  # nudge files only — simpler
        )
        result1 = execute_plan(plan1)
        assert result1.all_succeeded
        assert any(r.action == "created" for r in result1.steps)

        plan2 = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,
        )
        result2 = execute_plan(plan2)
        assert result2.all_succeeded
        # Every step should be no_change — file exists, content matches
        for r in result2.steps:
            assert r.action == "no_change", (
                f"step {r.step.preview} reported {r.action} on idempotent re-run"
            )


# =====================================================================
# Acceptance scenario #3: Partial detect
# =====================================================================


class TestPartialDetect:
    def test_only_claude_detected_only_claude_configured(
        self, isolated: Path
    ):
        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,
        )
        # Should have exactly 1 nudge step (for claude) + maybe 1 AGENTS.md
        # fallback. No cursor/windsurf/antigravity steps.
        nudge_steps = [s for s in plan.steps if s.kind == "nudge_file"]
        ides_targeted = {s.ide for s in nudge_steps}
        assert "claude" in ides_targeted
        assert "cursor" not in ides_targeted
        assert "windsurf" not in ides_targeted


# =====================================================================
# Acceptance scenario #4: --dry-run produces no writes
# =====================================================================


class TestDryRun:
    def test_dry_run_touches_nothing(self, isolated: Path):
        plan = build_setup_plan(
            isolated, detected_ides=("claude", "cursor"),
            install_mcp=False, install_hooks=False,
        )
        # Snapshot the project tree
        before = self._tree_snapshot(isolated)
        result = execute_plan(plan, dry_run=True)
        after = self._tree_snapshot(isolated)
        # All steps marked "would_*"
        for r in result.steps:
            assert r.action.startswith("would_"), r.action
        # No files changed
        assert before == after

    def _tree_snapshot(self, root: Path) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for p in root.rglob("*"):
            if p.is_file():
                out[str(p.relative_to(root))] = p.read_bytes()
        return out


# =====================================================================
# Acceptance scenario #5: --no-hooks skips hook installation
# =====================================================================


class TestNoHooks:
    def test_no_hooks_skips_hook_steps(self, isolated: Path):
        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_hooks=False,
            install_mcp=False,
        )
        kinds = {s.kind for s in plan.steps}
        assert "hook" not in kinds

    def test_install_hooks_includes_all_5_events(self, isolated: Path):
        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_hooks=True,
            install_mcp=False,
            write_nudge_files=False,
        )
        hook_steps = [s for s in plan.steps if s.kind == "hook"]
        # 5 script-install steps + 1 settings.json registration step = 6
        assert len(hook_steps) == 6


# =====================================================================
# Acceptance scenario #6: Malformed config doesn't crash setup
# =====================================================================


class TestMalformedConfig:
    def test_broken_settings_json_does_not_crash(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Plant a broken settings.json
        claude_dir = Path.home() / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.json").write_text("{ this is not json")

        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=True, write_nudge_files=False,
        )
        result = execute_plan(plan)
        # The hook script copies should still succeed; only the
        # settings.json merge step should fail.
        script_results = [
            r for r in result.steps
            if r.step.target_path.name != "settings.json"
        ]
        assert all(r.succeeded for r in script_results), (
            "script-install steps must succeed even when settings.json is broken"
        )
        settings_results = [
            r for r in result.steps
            if r.step.target_path.name == "settings.json"
        ]
        assert len(settings_results) == 1
        assert not settings_results[0].succeeded
        assert "json" in (settings_results[0].error or "").lower()


# =====================================================================
# Acceptance scenario #7: Existing CLAUDE.md content preserved
# =====================================================================


class TestPreservesUserContent:
    def test_existing_claude_md_user_content_preserved(self, isolated: Path):
        # Plant a CLAUDE.md with user content (no codevira markers)
        user_md = isolated / "CLAUDE.md"
        custom = (
            "# My personal project notes\n\n"
            "When editing this codebase, always run `make lint` before committing.\n"
            "We use 4-space indentation.\n"
        )
        user_md.write_text(custom)

        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,
        )
        result = execute_plan(plan)
        assert result.all_succeeded

        final = user_md.read_text()
        # User content is preserved verbatim
        assert "My personal project notes" in final
        assert "always run `make lint`" in final
        assert "4-space indentation" in final
        # Codevira block was appended
        assert "<!-- codevira:start -->" in final
        assert "<!-- codevira:end -->" in final


# =====================================================================
# Acceptance scenario #8: --ide cursor only touches Cursor
# =====================================================================


class TestSelectiveIDE:
    def test_only_ides_filter_excludes_others(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Mock detection so the wizard sees claude+cursor as installed
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: ["claude", "cursor"],
        )
        detected = detect_targets(isolated, only_ides=("cursor",))
        assert detected == ("cursor",)

        plan = build_setup_plan(
            isolated, detected_ides=detected,
            install_mcp=False, install_hooks=False,
        )
        ides_in_plan = {s.ide for s in plan.steps if s.kind == "nudge_file"}
        # Only cursor's file (no AGENTS.md fallback because cursor
        # is the only detected IDE and we don't have codex)
        assert "cursor" in ides_in_plan
        assert "claude" not in ides_in_plan

    def test_unknown_ide_in_filter_raises(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: ["claude"],
        )
        with pytest.raises(ValueError, match="unknown IDE"):
            detect_targets(isolated, only_ides=("not-a-real-ide",))


# =====================================================================
# Acceptance scenario #9: Bad project_root rejected
# =====================================================================


class TestProjectRootGuard:
    def test_resolve_setup_target_rejects_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # cwd = home (which is the v1.8.1 forbidden case)
        monkeypatch.chdir(home)

        with pytest.raises(SystemExit) as exc_info:
            resolve_setup_target()
        assert exc_info.value.code == 1


# =====================================================================
# Acceptance scenario #10: Wall-clock budget
# =====================================================================


class TestPerformance:
    def test_plan_under_50ms(self, isolated: Path):
        """Plan-building must be fast — the spec budgets <50ms p95."""
        import time
        durations = []
        for _ in range(20):
            t = time.perf_counter()
            build_setup_plan(
                isolated, detected_ides=("claude", "cursor", "windsurf",
                                         "antigravity", "codex"),
            )
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[18]
        assert p95 < 50.0, f"build_setup_plan p95 = {p95:.1f}ms"

    def test_full_execute_under_5s(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Full execute on a 4-IDE detect must finish in < 5s.

        Skip MCP config steps (those touch external IDE config files
        outside our test isolation); just verify the hook + nudge file
        path is fast.
        """
        import time
        plan = build_setup_plan(
            isolated, detected_ides=("claude", "cursor", "windsurf", "codex"),
            install_mcp=False,  # avoids real ~/.codeium etc.
        )
        t = time.perf_counter()
        result = execute_plan(plan)
        elapsed = time.perf_counter() - t
        assert elapsed < 5.0, f"execute_plan took {elapsed:.2f}s"
        assert result.all_succeeded


# =====================================================================
# Plan data-structure unit tests (covers dataclass invariants)
# =====================================================================


class TestPlanDataStructures:
    def test_setup_step_is_frozen(self, isolated: Path):
        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,
        )
        step = plan.steps[0]
        with pytest.raises((AttributeError, TypeError)):
            step.kind = "different"  # type: ignore[misc]

    def test_setup_plan_steps_is_tuple_not_list(self, isolated: Path):
        plan = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_mcp=False, install_hooks=False,
        )
        assert isinstance(plan.steps, tuple)


# =====================================================================
# Acceptance scenario #1: Cold install via cmd_setup --yes (full flow)
# =====================================================================


class TestExternalSchema:
    """Week-3 R8 findings: per-IDE schema verification.

    These tests assert structural details that, if changed, would
    silently break codevira's integration with the target IDE — the
    same class of bug the Week-1 R5 round caught (4 schema mismatches
    with Claude Code).
    """

    def test_pre_post_tool_use_have_matcher_field(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Hook registration for PreToolUse + PostToolUse must include
        a ``matcher`` field scoping to Edit/Write/MultiEdit. Without it,
        codevira's hooks fire on every Read/Bash/Glob call, costing
        ~50 ms shell startup per invocation. (Week-3 R8 finding.)
        """
        from mcp_server.setup_wizard import _install_hook_registrations, SetupStep

        # Build a fake step pointing at a settings.json under the
        # isolated home, then run the registration installer.
        settings_path = Path.home() / ".claude" / "settings.json"
        step = SetupStep(
            kind="hook", ide="claude", target_path=settings_path,
            target_path_existed=False, will_merge=False,
            preview="Register codevira hooks",
        )
        result = _install_hook_registrations(step, dry_run=False)
        assert result.succeeded, result.error
        assert result.action in ("created", "merged")

        data = json.loads(settings_path.read_text())
        for event in ("PreToolUse", "PostToolUse"):
            entries = data["hooks"][event]
            assert entries, f"no entries for {event}"
            entry = entries[0]  # codevira always prepends
            assert "matcher" in entry, (
                f"{event} entry missing matcher — codevira would fire on "
                f"every tool call, not just file modifications"
            )
            # The matcher is a regex; verify it matches our edit tools
            import re
            for tool in ("Edit", "Write", "MultiEdit"):
                assert re.search(entry["matcher"], tool), (
                    f"{event} matcher {entry['matcher']!r} doesn't match {tool}"
                )
            # Negative: should NOT match Read/Bash
            for tool in ("Read", "Bash", "Glob"):
                assert not re.fullmatch(entry["matcher"], tool), (
                    f"{event} matcher {entry['matcher']!r} unexpectedly matches {tool}"
                )

    def test_session_lifecycle_events_have_no_matcher(
        self, isolated: Path
    ):
        """SessionStart / UserPromptSubmit / Stop have no tool name —
        a matcher would never match. They must be registered without one.
        """
        from mcp_server.setup_wizard import _install_hook_registrations, SetupStep

        settings_path = Path.home() / ".claude" / "settings.json"
        step = SetupStep(
            kind="hook", ide="claude", target_path=settings_path,
            target_path_existed=False, will_merge=False, preview="...",
        )
        _install_hook_registrations(step, dry_run=False)

        data = json.loads(settings_path.read_text())
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            entries = data["hooks"][event]
            assert entries, f"no entries for {event}"
            entry = entries[0]
            assert "matcher" not in entry, (
                f"{event} should not have a matcher (no tool name in event)"
            )

    def test_canonical_block_under_windsurf_12k_cap(self):
        """Windsurf enforces a 12,000-character workspace-rules limit.
        The canonical block (which goes into .windsurfrules) must stay
        under it with headroom for the IDE-specific wrapper. (Week-3 R8
        external-schema finding.)"""
        from mcp_server.agents_md import canonical_block_text, render_for_ide
        block = canonical_block_text()
        windsurf_text = render_for_ide("windsurf")
        assert len(block) < 11000, (
            f"canonical block grew to {len(block)} chars, leaves <1K "
            f"headroom under Windsurf's 12K cap"
        )
        assert len(windsurf_text) < 12000, (
            f"rendered .windsurfrules content {len(windsurf_text)} chars "
            f"exceeds Windsurf's 12,000-char limit"
        )


class TestCLIVisibility:
    """Week-3 R2 finding: `register --help` must surface the
    deprecation notice, not just the parent help.

    Help is shown via the subparser's ``description`` field; without
    this, users running `codevira register --help` see only the
    --claude-desktop / --http-url flags and not that the whole
    command is deprecated.
    """

    def test_register_help_shows_deprecation(self):
        import subprocess
        from pathlib import Path
        venv_py = (
            Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
        )
        if not venv_py.exists():
            import sys
            venv_py = Path(sys.executable)
        result = subprocess.run(
            [str(venv_py), "-m", "mcp_server.cli", "register", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        # The deprecation must be in the help output (subparser
        # description), not only in the parent command's help= line.
        assert "DEPRECATED" in result.stdout, (
            f"`codevira register --help` does not surface deprecation: "
            f"{result.stdout!r}"
        )
        assert "codevira setup" in result.stdout


class TestSecurityHardening:
    """Week-3 R1 QA findings: marker-spoofing + symlink traversal."""

    def test_inline_marker_in_user_prose_does_not_match(
        self, isolated: Path
    ):
        """User content with '<!-- codevira:start -->' inside a sentence
        must NOT trigger our regex replace (markers are line-anchored).
        """
        from mcp_server.agents_md import write_nudge_file
        target = isolated / "CLAUDE.md"
        # Plant content with the marker substring INLINE, not on its
        # own line. Regex must skip and append a fresh block.
        target.write_text(
            "I once mentioned <!-- codevira:start --> in a comment but "
            "it wasn't a real marker. Same for <!-- codevira:end -->.\n"
        )

        result = write_nudge_file("claude", isolated)
        # Because no real (line-anchored) markers exist, we should
        # APPEND a new block — not replace mid-prose.
        assert result.action == "block_appended", result.action
        final = target.read_text()
        # User content preserved verbatim — including the inline-marker
        # words. Block was added, not in place of the user's text.
        assert "I once mentioned" in final
        assert "it wasn't a real marker" in final
        # The real codevira block is now present (line-anchored markers
        # at the end of file).
        assert "\n<!-- codevira:start -->\n" in final
        assert "\n<!-- codevira:end -->\n" in final or final.endswith(
            "<!-- codevira:end -->\n"
        )

    def test_symlink_at_target_refused(
        self, isolated: Path
    ):
        """If CLAUDE.md is a symlink (potentially pointing outside
        the project), refuse to write through it.
        """
        from mcp_server.agents_md import write_nudge_file

        # Create a target file outside the project, then symlink to it.
        outside = isolated.parent / "outside_target.md"
        outside.write_text("don't touch me")
        symlink_target = isolated / "CLAUDE.md"
        try:
            symlink_target.symlink_to(outside)
        except OSError:
            pytest.skip("filesystem doesn't support symlinks")

        with pytest.raises(ValueError, match="symlink"):
            write_nudge_file("claude", isolated)
        # The outside file MUST be untouched
        assert outside.read_text() == "don't touch me"

    def test_symlink_in_parent_dir_refused(
        self, isolated: Path
    ):
        """If a parent directory in the path is a symlink that escapes
        the project, refuse to write.
        """
        from mcp_server.agents_md import write_nudge_file

        outside_dir = isolated.parent / "outside_dir"
        outside_dir.mkdir()

        cursor_parent = isolated / ".cursor"
        try:
            cursor_parent.symlink_to(outside_dir)
        except OSError:
            pytest.skip("filesystem doesn't support symlinks")

        with pytest.raises(ValueError, match="symlink"):
            write_nudge_file("cursor", isolated)


class TestIntegrationFindings:
    """Week-1-through-4 integration round (I1) findings.

    These tests guard against regressions in cross-module data flow
    that per-module unit tests didn't catch.
    """

    def test_mcp_config_path_matches_inject_helper(
        self, isolated: Path
    ):
        """The wizard's preview path for each IDE must match the path
        the underlying ``inject_global_*`` helper actually writes to.
        Mismatch = misleading preview AND broken idempotency check.
        (I1: caught Antigravity preview was ~/.gemini/settings.json
        but inject wrote to ~/.gemini/antigravity/mcp_config.json.)
        """
        from mcp_server.setup_wizard import _mcp_config_path_for
        from mcp_server.ide_inject import (
            _claude_global_config_path, _claude_desktop_config_path,
            _cursor_global_config_path,
            _windsurf_global_config_path, _antigravity_config_path,
        )
        assert _mcp_config_path_for("claude") == _claude_global_config_path()
        assert _mcp_config_path_for("claude_desktop") == _claude_desktop_config_path()
        assert _mcp_config_path_for("cursor") == _cursor_global_config_path()
        assert _mcp_config_path_for("windsurf") == _windsurf_global_config_path()
        assert _mcp_config_path_for("antigravity") == _antigravity_config_path()

    def test_claude_desktop_step_is_planned_when_detected(self, isolated: Path):
        """Bug 6b regression: when claude_desktop is in the detected list,
        setup_wizard MUST plan a step for it (with the correct desktop
        config path) — previously it was silently skipped because
        ``_mcp_config_path_for()`` had no claude_desktop branch.
        """
        from mcp_server.setup_wizard import _mcp_config_path_for
        from mcp_server.ide_inject import _claude_desktop_config_path
        result = _mcp_config_path_for("claude_desktop")
        assert result is not None, (
            "claude_desktop must produce a planned MCP-config path; "
            "regression of Bug 6b (silently skipped in setup wizard)"
        )
        assert result == _claude_desktop_config_path()

    def test_setup_wizard_dispatcher_includes_claude_desktop(self):
        """Verify the _execute_mcp_config dispatcher routes ``claude_desktop``
        to ``inject_global_claude_desktop`` (not silently skipped). We
        check the dispatcher key directly to avoid wiring a full setup
        run — this is a contract test, not an integration test.
        """
        from mcp_server import setup_wizard
        # The dispatcher dict is constructed inside _execute_mcp_config.
        # Read the source to ensure both keys are present (cheap regex
        # check is fine — change-detector but pinned to the bug shape).
        import inspect
        src = inspect.getsource(setup_wizard._execute_mcp_config)
        assert '"claude_desktop"' in src, (
            "Bug 6b regression: _execute_mcp_config dispatcher must "
            "include a 'claude_desktop' handler"
        )
        assert "inject_global_claude_desktop" in src, (
            "Bug 6b regression: _execute_mcp_config must import and call "
            "inject_global_claude_desktop"
        )

    def test_nudge_write_is_atomic_no_temp_files_on_success(
        self, isolated: Path
    ):
        """I7 finding C.2: nudge writes use temp-then-rename so a
        Ctrl-C mid-write doesn't corrupt the target. Verify the
        success path leaves NO leftover temp file (the rename
        transferred ownership) and the target has the right content.
        """
        from mcp_server.agents_md import write_nudge_file
        result = write_nudge_file("claude", isolated)
        assert result.action == "created"

        # Target exists with codevira content
        target = isolated / "CLAUDE.md"
        assert target.exists()
        assert "Codevira" in target.read_text()

        # No leftover .CLAUDE.md.* temp files (atomic rename succeeded)
        leftovers = [
            p for p in isolated.iterdir()
            if p.name.startswith(".CLAUDE.md.") and p.name.endswith(".tmp")
        ]
        assert not leftovers, (
            f"atomic write left temp files behind: {[p.name for p in leftovers]}"
        )

    def test_atomic_write_helper_writes_correctly(
        self, tmp_path: Path
    ):
        """Direct test of the _atomic_write_text helper: writes the
        right bytes, leaves no temp residue, returns correct byte count.
        """
        from mcp_server.agents_md import _atomic_write_text
        target = tmp_path / "subdir" / "out.txt"  # parent doesn't exist yet
        content = "héllo, wörld\n" * 100
        n = _atomic_write_text(target, content)
        assert n == len(content.encode("utf-8"))
        assert target.read_text(encoding="utf-8") == content
        # No temp leftovers
        leftovers = list(target.parent.glob(".out.txt.*.tmp"))
        assert not leftovers, f"temp files: {leftovers}"

    def test_atomic_write_uses_os_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """I8 mutation finding: output-only tests can't catch the
        atomicity contract — a plain ``write_text`` produces the same
        output as a temp-file-then-rename, so neither byte content
        nor "no temp leftovers" discriminates.

        Behavioral test: spy on ``os.replace`` and assert it's called
        exactly once during a successful _atomic_write_text. A reverted
        helper that calls ``write_text`` directly will skip ``os.replace``
        entirely → test fails.
        """
        import os
        from mcp_server.agents_md import _atomic_write_text

        replace_calls: list[tuple[str, str]] = []
        original_replace = os.replace

        def spy_replace(src, dst, *args, **kwargs):
            replace_calls.append((str(src), str(dst)))
            return original_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", spy_replace)

        target = tmp_path / "out.txt"
        _atomic_write_text(target, "content\n")
        assert target.read_text() == "content\n"

        # The atomic-write contract REQUIRES exactly one os.replace call
        # per successful write — that's the atomicity guarantee. A plain
        # ``write_text`` skips this entirely.
        assert len(replace_calls) == 1, (
            f"_atomic_write_text must use os.replace for atomicity; got "
            f"{len(replace_calls)} calls. If 0, the helper degraded to "
            f"plain write_text and lost atomicity."
        )
        # The replace must have moved a temp file (not the target itself)
        # into the target.
        src, dst = replace_calls[0]
        assert dst == str(target)
        assert src != str(target), (
            "atomic write must rename a temp file INTO target, not "
            "replace target with itself"
        )

    def test_atomic_write_cleans_up_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If os.replace fails (e.g. cross-filesystem, permission), the
        atomic-write helper must NOT leave temp-file litter behind.
        Verify the failure path's cleanup runs.
        """
        import os
        from mcp_server.agents_md import _atomic_write_text

        def failing_replace(src, dst, *args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)

        target = tmp_path / "out.txt"
        with pytest.raises(OSError):
            _atomic_write_text(target, "content\n")

        # Target was never created
        assert not target.exists()
        # No temp leftovers
        leftovers = list(tmp_path.glob(".out.txt.*.tmp"))
        assert not leftovers, (
            f"failed atomic write left temp files: {leftovers}"
        )

    def test_idempotent_rerun_reports_no_change_on_mcp_config(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Two consecutive `setup --yes` runs must report "no_change"
        on every MCP-config step the second time. The wizard used to
        report "merged" purely based on file existence (not actual
        content change) → idempotent re-runs falsely showed N changes.
        (I1 finding.)
        """
        from mcp_server.setup_wizard import build_setup_plan, execute_plan

        # Stub out detect to claim claude is installed
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: ["claude"],
        )

        plan1 = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_hooks=False, write_nudge_files=False,
        )
        result1 = execute_plan(plan1)
        # First run: at least one step that's not no_change/skipped
        assert any(
            r.action not in ("no_change", "skipped")
            for r in result1.steps
        ), f"first run did nothing: {[r.action for r in result1.steps]}"

        plan2 = build_setup_plan(
            isolated, detected_ides=("claude",),
            install_hooks=False, write_nudge_files=False,
        )
        result2 = execute_plan(plan2)
        # Second run: every MCP config step must be no_change
        for r in result2.steps:
            if r.step.kind == "mcp_config":
                assert r.action == "no_change", (
                    f"idempotent re-run reported {r.action!r} on "
                    f"{r.step.preview} — wizard isn't detecting "
                    f"already-current state"
                )


class TestColdInstall:
    def test_cmd_setup_yes_succeeds_end_to_end(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """Cold-install scenario from the spec: --yes returns 0
        and the project ends up with codevira nudge files.

        We mock IDE detection to claim claude+cursor are present and
        skip MCP config (the global ~/.claude/settings.json merge
        path is exercised in TestMalformedConfig already).
        """
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: ["claude"],
        )

        rc = setup_wizard.cmd_setup(
            yes=True,
            install_mcp=False,
            install_hooks=False,
        )
        assert rc == 0

        # CLAUDE.md created
        assert (isolated / "CLAUDE.md").exists()
        content = (isolated / "CLAUDE.md").read_text()
        assert "Codevira — persistent project memory" in content

    def test_cmd_setup_no_ides_detected_returns_zero(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If no AI tools detected, exit 0 cleanly with a message."""
        monkeypatch.setattr(
            "mcp_server.ide_inject.detect_installed_ides",
            lambda _root: [],
        )
        rc = setup_wizard.cmd_setup(yes=True)
        assert rc == 0
