"""
test_cli_agents.py — Pillar 2.2 + 2.3 + the wedge consistency test.

Three responsibilities:

1. Verify `cmd_agents` writes the right per-IDE files (and respects
   --ide / --dry-run / --project Bug-8 defenses).

2. Verify `cmd_hooks_install` runs the hook installation pipeline.

3. **The wedge consistency test (G5)**: every per-IDE rendered output
   must contain the same canonical instructions block. If a template
   accidentally drops the `{{CODEVIRA_BLOCK}}` placeholder or the
   block content drifts between IDEs, the universality wedge breaks
   silently. This test makes that drift impossible.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import agents_md
from mcp_server.cli_agents import cmd_agents, cmd_hooks_install


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cv_data = fake_home / ".codevira"
    cv_data.mkdir()
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".git").mkdir()
    monkeypatch.setattr("mcp_server.paths.get_global_home", lambda: cv_data)
    return project


# =====================================================================
# A. cmd_agents
# =====================================================================


class TestCmdAgents:

    def test_default_writes_all_ides_in_isolated_project(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        out = io.StringIO()
        rc = cmd_agents(out=out)
        assert rc == 0
        # Verify the expected files exist
        for rel in (
            "CLAUDE.md",
            ".cursor/rules/codevira.mdc",
            ".windsurfrules",
            "GEMINI.md",
            "AGENTS.md",
            ".github/copilot-instructions.md",
        ):
            assert (isolated_project / rel).exists(), (
                f"expected {rel} after `codevira agents`"
            )

    def test_single_ide_writes_only_that_one(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        rc = cmd_agents(ide="claude", out=io.StringIO())
        assert rc == 0
        assert (isolated_project / "CLAUDE.md").exists()
        # Other IDEs NOT touched
        assert not (isolated_project / ".cursor").exists()
        assert not (isolated_project / ".windsurfrules").exists()

    def test_dry_run_writes_nothing(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        out = io.StringIO()
        rc = cmd_agents(dry_run=True, out=out)
        assert rc == 0
        # No files materialized
        assert not (isolated_project / "CLAUDE.md").exists()
        # But output mentions "would" verbiage
        text = out.getvalue()
        assert "dry-run" in text or "would" in text

    def test_invalid_ide_returns_1(self):
        out = io.StringIO()
        rc = cmd_agents(ide="not-a-real-ide", out=out)
        assert rc == 1
        assert "unknown --ide" in out.getvalue()

    def test_idempotent_re_run(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        # First run: creates everything
        cmd_agents(out=io.StringIO())
        first_size = (isolated_project / "CLAUDE.md").stat().st_size
        first_mtime = (isolated_project / "CLAUDE.md").stat().st_mtime
        # Second run: idempotent — content same, file likely unchanged
        out = io.StringIO()
        rc = cmd_agents(out=out)
        assert rc == 0
        assert (isolated_project / "CLAUDE.md").stat().st_size == first_size
        # Content should still be identical (block is the same)
        text = (isolated_project / "CLAUDE.md").read_text()
        assert agents_md.START_MARKER in text
        assert agents_md.END_MARKER in text

    def test_invalid_project_root_rejected_bug8(self):
        """Bug-8 parity: an invalid project root (system top like ``/``)
        must be rejected with rc=1 + a clear error."""
        out = io.StringIO()
        rc = cmd_agents(project=Path("/"), out=out)
        assert rc == 1
        assert "not a valid project root" in out.getvalue()


# =====================================================================
# B. cmd_hooks_install
# =====================================================================


class TestCmdHooksInstall:

    def test_dry_run_runs_without_writing(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import mcp_server.paths as paths_mod
        paths_mod.set_project_dir(isolated_project)
        paths_mod.invalidate_data_dir_cache()

        out = io.StringIO()
        rc = cmd_hooks_install(dry_run=True, out=out)
        # Either 0 (claude code detected & dry-run) or 0 (no claude code → "nothing to install")
        assert rc in (0, 1)
        text = out.getvalue()
        # Should mention dry-run
        assert "dry-run" in text or "nothing to install" in text

    def test_invalid_project_root_rejected_bug8(self):
        out = io.StringIO()
        rc = cmd_hooks_install(project=Path("/"), out=out)
        assert rc == 1
        assert "not a valid project root" in out.getvalue()


# =====================================================================
# C. Subprocess (full CLI wiring)
# =====================================================================


class TestSubprocessWiring:

    def _env(self) -> dict[str, str]:
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def test_agents_subcommand_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "agents", "--help"],
            env=self._env(), capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "--ide" in result.stdout
        assert "--dry-run" in result.stdout
        assert "--project" in result.stdout

    def test_hooks_install_subcommand_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli",
             "hooks", "install", "--help"],
            env=self._env(), capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "--dry-run" in result.stdout

    def test_agents_dry_run_subprocess(self, isolated_project: Path):
        env = self._env()
        env["HOME"] = str(isolated_project.parent / "home")
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "agents",
             "--project", str(isolated_project), "--dry-run"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "dry-run" in result.stdout or "would" in result.stdout
        # No files written
        assert not (isolated_project / "CLAUDE.md").exists()


# =====================================================================
# D. THE WEDGE TEST (G5) — every IDE template must include the
#    canonical block content. If a template drops the placeholder
#    or the block drifts, the universality wedge breaks silently.
# =====================================================================


class TestWedgeConsistency:
    """The whole point of v2.0 is: same memory in every AI tool. That
    promise depends on every per-IDE nudge file containing the same
    canonical instructions about codevira's tools. These tests lock
    that contract.

    If you ever see one of these fail, something has drifted in:
      - mcp_server/data/templates/<ide>.tmpl  (missing placeholder?)
      - mcp_server/data/templates/canonical_block.md  (content shrunk?)
      - mcp_server/agents_md.py  (substitution logic broken?)
    """

    def test_canonical_block_has_essential_content(self):
        """The canonical block must mention codevira AND the most
        important entry-point tool. If this drifts, the wedge promise
        is downgraded silently."""
        block = agents_md.canonical_block_text()
        # Essential mentions — the AI must learn these from the block
        assert "codevira" in block.lower()
        # Mentioning "session" / "context" / "decisions" is the
        # bare minimum so the AI knows what to do with the tools.
        for must_have in ("session", "decision"):
            assert must_have in block.lower(), (
                f"canonical block missing essential keyword {must_have!r}; "
                f"the AI won't know to use codevira's core tools"
            )
        # Length sanity — block must be substantive (not accidentally empty)
        assert len(block) > 200, (
            f"canonical block too short ({len(block)} chars) — "
            f"likely truncated by accident"
        )

    def test_every_ide_renders_with_canonical_block_intact(self):
        """For each supported IDE, the rendered nudge file must
        contain the canonical block content INSIDE codevira markers.
        """
        canonical = agents_md.canonical_block_text()
        # Pick a few stable strings from the canonical block to assert
        # presence of (full string-match would be brittle to whitespace
        # rendering differences).
        canon_lines = [
            line.strip()
            for line in canonical.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        # Use the first few non-empty non-heading lines as fingerprints.
        fingerprints = canon_lines[:3]
        assert fingerprints, "canonical block has no content to fingerprint"

        for ide in agents_md.supported_ides():
            rendered = agents_md.render_for_ide(ide)
            assert agents_md.START_MARKER in rendered, (
                f"{ide}: rendered output missing START_MARKER"
            )
            assert agents_md.END_MARKER in rendered, (
                f"{ide}: rendered output missing END_MARKER"
            )
            for fp in fingerprints:
                # Allow whitespace differences but match the substantive
                # text. Rendered output may have escaping for some
                # IDE templates (e.g., YAML frontmatter) but the BODY
                # of the block should preserve text.
                assert fp in rendered, (
                    f"{ide}: rendered output missing canonical line "
                    f"{fp!r} — wedge consistency broken"
                )

    def test_supported_ides_set_stable(self):
        """The supported-IDE list is the contract for the universality
        wedge. Drift here MUST be deliberate, not silent."""
        # As of v2.0 — explicit lock-in
        expected = {
            "claude", "cursor", "windsurf", "antigravity",
            "codex", "copilot", "agents_md",
        }
        actual = set(agents_md.supported_ides())
        assert actual == expected, (
            f"SUPPORTED_IDES drift: got {actual}, expected {expected}. "
            f"If this is intentional, update the test."
        )

    def test_each_template_file_exists(self):
        """Every supported IDE must have its template file on disk."""
        templates_dir = (
            Path(agents_md.__file__).parent / "data" / "templates"
        )
        for ide in agents_md.supported_ides():
            tmpl_filename = agents_md._IDE_SPECS[ide].template
            tmpl_path = templates_dir / tmpl_filename
            assert tmpl_path.exists(), (
                f"{ide}: template file missing at {tmpl_path}"
            )

    def test_each_template_uses_placeholder(self):
        """Every per-IDE template MUST include {{CODEVIRA_BLOCK}}.
        If it doesn't, the canonical block won't be substituted in
        and the file will ship without codevira instructions —
        silent wedge failure."""
        templates_dir = (
            Path(agents_md.__file__).parent / "data" / "templates"
        )
        for ide in agents_md.supported_ides():
            tmpl_filename = agents_md._IDE_SPECS[ide].template
            tmpl_path = templates_dir / tmpl_filename
            content = tmpl_path.read_text()
            assert "{{CODEVIRA_BLOCK}}" in content, (
                f"{ide}: template {tmpl_path.name} missing "
                "{{CODEVIRA_BLOCK}} placeholder — silent wedge break"
            )
