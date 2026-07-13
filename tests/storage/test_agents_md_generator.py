"""
test_agents_md_generator.py — v2.2.0 Phase D verification.

Covers the AGENTS.md generator. Critical invariants:

1. **5 KB cap.** A 100-decision project's AGENTS.md is ≤5 KB.
2. **Marker preservation.** Content outside ``<!-- codevira:begin -->``
   and ``<!-- codevira:end -->`` is preserved byte-for-byte.
3. **Deterministic.** Same input → same output bytes.
4. **do_not_revert priority.** Locked decisions are always rendered;
   unlocked get cut first when over budget.
5. **Sync after write.** ``record_decision`` triggers regen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.storage import agents_md_generator, decisions_store


pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project root + fake HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'agents-md-test'\nversion = '0.0.1'\n"
    )
    from mcp_server import paths as core_paths

    core_paths.set_project_dir(project)
    core_paths.invalidate_data_dir_cache()
    return project


class TestUsageGuard:
    """v3.7.0 (M3): the block carries a branded usage guard telling the agent
    to use the codevira MCP tools, NOT to read .codevira/*.jsonl directly
    (which would burn tens of thousands of tokens)."""

    def test_block_has_branding_and_raw_read_guard(self):
        block = agents_md_generator._render_block([], "myproj")
        assert "Codevira" in block, "branded"
        # The guard must appear and must steer AWAY from raw file reads.
        assert "do **not** open" in block or "don't read" in block
        assert ".codevira/*.jsonl" in block

    def test_block_does_not_tell_agent_to_read_raw_jsonl(self):
        # Regression: the old footer said "see `.codevira/decisions.jsonl`",
        # actively encouraging the token-heavy raw read. It must not return.
        block = agents_md_generator._render_block([], "myproj")
        assert "see `.codevira/decisions.jsonl`" not in block
        assert "`.codevira/decisions.jsonl` or run" not in block

    def test_guard_stays_within_5kb_cap(self):
        block = agents_md_generator._render_block([], "myproj")
        assert len(block.encode("utf-8")) <= agents_md_generator._BLOCK_MAX_BYTES


class TestBasicGeneration:
    def test_regenerate_creates_agents_md(self, isolated_project: Path):
        decisions_store.record(decision="Use bcrypt", file_path="auth.py")
        agents_md_path = isolated_project / "AGENTS.md"
        assert agents_md_path.is_file()
        content = agents_md_path.read_text()
        assert "<!-- codevira:begin" in content
        assert "<!-- codevira:end -->" in content

    def test_empty_project_still_renders(self, isolated_project: Path):
        summary = agents_md_generator.regenerate()
        agents_md_path = isolated_project / "AGENTS.md"
        assert agents_md_path.is_file()
        # Decision count is zero but the block + project name appears.
        assert summary["decisions_in_block"] == 0
        content = agents_md_path.read_text()
        assert "agents-md-test" in content

    def test_decision_appears_in_block(self, isolated_project: Path):
        did = decisions_store.record(
            decision="Use bcrypt over argon2 for password hashing",
            file_path="auth.py",
            do_not_revert=True,
            tags=["security", "auth"],
        )
        content = (isolated_project / "AGENTS.md").read_text()
        assert did in content
        assert "bcrypt" in content
        assert "auth.py" in content


class TestSizeCap:
    def test_5kb_cap_enforced_with_100_decisions(self, isolated_project: Path):
        """The hard 5 KB block cap MUST hold regardless of decision count."""
        # Record 100 decisions, varied vocab, mostly unlocked.
        for i in range(100):
            decisions_store.record(
                decision=(
                    f"Decision number {i} covering some implementation detail "
                    f"about module {i % 10} and its behavior under various conditions"
                ),
                file_path=f"src/module_{i % 10}.py",
                do_not_revert=(i % 20 == 0),  # 5% locked
                tags=[f"tag_{i % 5}"],
            )
        agents_md_path = isolated_project / "AGENTS.md"
        content = agents_md_path.read_text()

        # Extract the codevira block bytes.
        begin = content.find("<!-- codevira:begin")
        end = content.find("<!-- codevira:end -->")
        assert begin != -1 and end != -1
        block = content[begin : end + len("<!-- codevira:end -->")]
        block_bytes = len(block.encode("utf-8"))
        assert (
            block_bytes <= 5 * 1024
        ), f"Block exceeded 5 KB cap: {block_bytes} bytes; content:\n{block[:500]}..."

    def test_dropped_decisions_reported(self, isolated_project: Path):
        for i in range(100):
            decisions_store.record(
                decision=f"Long decision text number {i} that takes a few words to write",
                file_path=f"file_{i}.py",
                tags=[f"category_{i % 3}"],
            )
        summary = agents_md_generator.regenerate()
        assert summary["decisions_dropped"] >= 1, (
            f"100 decisions should overflow 5 KB cap; " f"summary={summary}"
        )
        content = (isolated_project / "AGENTS.md").read_text()
        assert "more decision(s)" in content

    def test_locked_decisions_always_rendered(self, isolated_project: Path):
        """do_not_revert decisions take precedence over unlocked when budget tight."""
        # 5 locked + 100 unlocked. Locked should ALL appear; unlocked may be cut.
        locked_ids = []
        for i in range(5):
            did = decisions_store.record(
                decision=f"Locked rule {i}: critical invariant",
                file_path=f"locked_{i}.py",
                do_not_revert=True,
                tags=["locked"],
            )
            locked_ids.append(did)
        for i in range(100):
            decisions_store.record(
                decision=f"Unlocked decision {i} that may get cut from the slim view",
                file_path=f"u_{i}.py",
            )

        content = (isolated_project / "AGENTS.md").read_text()
        for lid in locked_ids:
            assert lid in content, (
                f"locked decision {lid} should be in AGENTS.md even after "
                f"100 unlocked decisions; content head:\n{content[:1500]}"
            )


class TestMarkerPreservation:
    def test_user_content_outside_markers_preserved(self, isolated_project: Path):
        """The user can hand-edit anything outside the markers; we don't touch it."""
        agents_md = isolated_project / "AGENTS.md"
        user_content_before = (
            "# My Project's AGENTS.md\n"
            "\n"
            "## My team's hand-written conventions\n"
            "\n"
            "- We use Python 3.13 strict mode\n"
            "- All API endpoints must validate inputs via pydantic\n"
            "- Tests live alongside source under `tests/`\n"
            "\n"
        )
        user_content_after = (
            "\n\n"
            "## Hand-written team notes (not codevira-managed)\n"
            "\n"
            "- Code review checklist: type hints, docstrings, tests\n"
        )

        # Write the file with markers + user content surrounding them.
        agents_md.write_text(
            user_content_before
            + "<!-- codevira:begin (auto-generated; do not edit) -->\n"
            "OLD CODEVIRA BLOCK\n"
            "<!-- codevira:end -->" + user_content_after
        )

        decisions_store.record(decision="Use bcrypt", file_path="auth.py")

        # After regen, the user content must be preserved verbatim.
        content = agents_md.read_text()
        assert user_content_before in content
        assert user_content_after in content
        assert "OLD CODEVIRA BLOCK" not in content
        assert "bcrypt" in content
        # And the markers are still there + populated with fresh content.
        assert "<!-- codevira:begin" in content
        assert "<!-- codevira:end -->" in content

    def test_existing_file_without_markers_prepends_block(self, isolated_project: Path):
        agents_md = isolated_project / "AGENTS.md"
        agents_md.write_text(
            "# My Project's AGENTS.md\n"
            "\n"
            "## Hand-written\n"
            "\n"
            "- All endpoints use bearer auth.\n"
        )

        decisions_store.record(decision="Use bcrypt", file_path="auth.py")
        content = agents_md.read_text()
        # User's existing content STILL present.
        assert "All endpoints use bearer auth" in content
        # And the codevira block is also present (prepended).
        assert "<!-- codevira:begin" in content
        assert "bcrypt" in content


class TestDeterminism:
    def test_same_decisions_produce_same_block(self, isolated_project: Path):
        """For prompt caching: same in → same bytes out."""
        decisions_store.record(decision="X", file_path="a.py", do_not_revert=True)
        decisions_store.record(decision="Y", file_path="b.py")

        content_1 = (isolated_project / "AGENTS.md").read_text()

        # Regenerate without changing decisions.
        agents_md_generator.regenerate()
        content_2 = (isolated_project / "AGENTS.md").read_text()

        assert content_1 == content_2, "AGENTS.md not deterministic across runs"

    def test_no_timestamps_in_block(self, isolated_project: Path):
        decisions_store.record(decision="X", file_path="a.py")
        content = (isolated_project / "AGENTS.md").read_text()
        import re

        # ISO 8601 patterns: YYYY-MM-DD or YYYY-MM-DDTHH:MM
        # Inside the codevira block, none should appear.
        block_match = re.search(
            r"<!-- codevira:begin.*?<!-- codevira:end -->",
            content,
            re.DOTALL,
        )
        assert block_match
        block = block_match.group(0)
        assert not re.search(
            r"\d{4}-\d{2}-\d{2}T", block
        ), f"timestamp in cache-stable block:\n{block}"


class TestSyncIntegration:
    def test_record_decision_triggers_agents_md_regen(self, isolated_project: Path):
        """The implicit sync-after-write contract."""
        agents_md = isolated_project / "AGENTS.md"
        # Before: no AGENTS.md
        assert not agents_md.exists()

        decisions_store.record(
            decision="Use bcrypt for hashing",
            file_path="auth.py",
            do_not_revert=True,
        )

        # After: AGENTS.md exists with the decision.
        assert agents_md.is_file()
        content = agents_md.read_text()
        assert "bcrypt" in content

    def test_record_many_triggers_single_regen(self, isolated_project: Path):
        """Batch record should produce a SINGLE AGENTS.md regen (efficient)."""
        agents_md = isolated_project / "AGENTS.md"

        decisions_store.record_many(
            [
                {"decision": "X", "file_path": "a.py"},
                {"decision": "Y", "file_path": "b.py"},
                {"decision": "Z", "file_path": "c.py"},
            ]
        )

        content = agents_md.read_text()
        assert "**D000001**" in content
        assert "**D000003**" in content

    def test_mark_protected_triggers_regen(self, isolated_project: Path):
        did = decisions_store.record(decision="X", file_path="a.py")

        # Verify it shows up unlocked at first.
        content = (isolated_project / "AGENTS.md").read_text()
        assert "Active conventions" in content

        # Mark it protected.
        res = decisions_store.mark_protected(did)
        assert res["success"]

        # After regen, it should appear under Locked.
        content = (isolated_project / "AGENTS.md").read_text()
        # The decision id should be in the Locked section.
        locked_section = content[content.find("Locked") :]
        assert did in locked_section


# ──────────────────────────────────────────────────────────────────────
# v3.1.x — idempotency (P5 churn fix)
# ──────────────────────────────────────────────────────────────────────


class TestRegenerateIdempotent:
    """regenerate() must NOT bump mtime / rewrite the file when the
    computed content equals what's already on disk. Previously every
    sync produced a fresh write, causing perpetual uncommitted drift."""

    def test_second_regen_is_noop_when_content_unchanged(
        self, isolated_project
    ) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(decision="stable decision", tags=["x"])
        agents_md_generator.regenerate()
        target = isolated_project / "AGENTS.md"
        mtime1 = target.stat().st_mtime
        # No new decisions; regenerate should produce identical content.
        import time

        time.sleep(0.05)  # ensure clock would tick if a write occurred
        agents_md_generator.regenerate()
        mtime2 = target.stat().st_mtime
        assert mtime1 == mtime2, (
            f"regenerate() rewrote file despite unchanged content "
            f"(mtime drifted: {mtime1} → {mtime2})"
        )

    def test_real_change_does_write(self, isolated_project) -> None:
        """Sanity: a new decision DOES bump mtime."""
        from mcp_server.storage import decisions_store

        decisions_store.record(decision="first decision", tags=["x"])
        agents_md_generator.regenerate()
        target = isolated_project / "AGENTS.md"
        mtime1 = target.stat().st_mtime
        import time

        time.sleep(0.05)
        decisions_store.record(decision="second decision", tags=["y"])
        agents_md_generator.regenerate()
        mtime2 = target.stat().st_mtime
        assert mtime2 > mtime1
