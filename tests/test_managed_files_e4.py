"""E4 (Phase 22) — managed memory-file writers beyond AGENTS.md.

Pins: default stays AGENTS.md-only; opt-in config writes CLAUDE.md / GEMINI.md
/ .cursor/rules/codevira.mdc; re-runs are idempotent; user content outside the
markers is preserved byte-for-byte; the Cursor .mdc gets valid frontmatter at
byte 0; bad config falls back safely; and E2's scanner never re-ingests the
injected block (echo-safety).
"""

from __future__ import annotations


from mcp_server.ingest import heuristics as H
from mcp_server.storage import agents_md_generator as gen
from mcp_server.storage import paths as store_paths


def _set_config(managed: list[str]) -> None:
    cfg = store_paths.config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = "project:\n  name: t\nmanaged_files:\n" + "".join(
        f"  - {m}\n" for m in managed
    )
    cfg.write_text(body, encoding="utf-8")


def _seed() -> None:
    from mcp_server.storage import decisions_store

    decisions_store.record(decision="adopt the widget queue", do_not_revert=True)


# ─────────────────────────────────────────────────────────────────────
# Target registry
# ─────────────────────────────────────────────────────────────────────


class TestTargets:
    def test_default_is_agents_only(self) -> None:
        # No managed_files in config → AGENTS.md only.
        assert gen._managed_targets() == [("AGENTS.md", "shared_md")]

    def test_config_opt_in(self) -> None:
        _set_config(
            ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursor/rules/codevira.mdc"]
        )
        targets = dict(gen._managed_targets())
        assert targets["AGENTS.md"] == "shared_md"
        assert targets["CLAUDE.md"] == "shared_md"
        assert targets["GEMINI.md"] == "shared_md"
        assert targets[".cursor/rules/codevira.mdc"] == "owned_mdc"

    def test_bad_config_falls_back(self) -> None:
        store_paths.config_path().write_text("{ not valid yaml ::", encoding="utf-8")
        assert gen._managed_targets() == [("AGENTS.md", "shared_md")]


# ─────────────────────────────────────────────────────────────────────
# Multi-file write
# ─────────────────────────────────────────────────────────────────────


class TestRegenerateAll:
    def test_writes_all_configured_files(self) -> None:
        _set_config(
            ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursor/rules/codevira.mdc"]
        )
        _seed()
        res = gen.regenerate_all()
        assert res["count"] == 4 and all(t["ok"] for t in res["targets"])

        root = store_paths.codevira_dir().parent  # project root
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            text = (root / name).read_text(encoding="utf-8")
            assert "<!-- codevira:begin" in text and "<!-- codevira:end -->" in text

        mdc = (root / ".cursor" / "rules" / "codevira.mdc").read_text(encoding="utf-8")
        assert mdc.startswith("---\n")  # frontmatter at byte 0 (Cursor requires)
        assert "alwaysApply: true" in mdc
        assert "<!-- codevira:begin" in mdc

    def test_default_writes_only_agents(self) -> None:
        # No managed_files config → only AGENTS.md is written.
        gen.regenerate_all()
        root = store_paths.codevira_dir().parent
        assert (root / "AGENTS.md").is_file()
        assert not (root / "CLAUDE.md").exists()
        assert not (root / "GEMINI.md").exists()

    def test_idempotent_no_churn(self) -> None:
        _set_config(["AGENTS.md", "CLAUDE.md", ".cursor/rules/codevira.mdc"])
        _seed()
        gen.regenerate_all()
        root = store_paths.codevira_dir().parent
        files = [
            root / "AGENTS.md",
            root / "CLAUDE.md",
            root / ".cursor" / "rules" / "codevira.mdc",
        ]
        before = {f: f.stat().st_mtime_ns for f in files}
        gen.regenerate_all()  # second run — content identical → no rewrite
        after = {f: f.stat().st_mtime_ns for f in files}
        assert before == after, "idempotent re-run must not rewrite unchanged files"

    def test_out_of_marker_content_preserved(self) -> None:
        _set_config(["AGENTS.md", "CLAUDE.md"])
        root = store_paths.codevira_dir().parent
        claude = root / "CLAUDE.md"
        claude.write_text("# My project rules\n\nKeep tests fast.\n", encoding="utf-8")
        _seed()
        gen.regenerate_all()
        text = claude.read_text(encoding="utf-8")
        assert "# My project rules" in text  # user content survives
        assert "Keep tests fast." in text
        assert "<!-- codevira:begin" in text  # our block was added


# ─────────────────────────────────────────────────────────────────────
# Echo-safety (E2 must not re-ingest the injected block)
# ─────────────────────────────────────────────────────────────────────


class TestEchoSafety:
    def test_managed_block_is_not_a_correction(self) -> None:
        echoed = (
            "<!-- codevira:begin (auto-generated; do not edit) -->\n"
            "## Codevira memory\n- do not revert X\n<!-- codevira:end -->"
        )
        assert H.is_managed_block(echoed)
        # Even though it contains 'revert', it must not register as a correction.
        assert not H.looks_like_correction(echoed)

    def test_real_correction_still_detected(self) -> None:
        assert H.looks_like_correction("no, that's wrong — revert it")
