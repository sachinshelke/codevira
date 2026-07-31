"""4.0 Steps 2.3 + 2.4 — the evidence must reach every reader.

Three surfaces silently dropped reasoning that was already stored:

  * ``decisions_store.search`` never returned ``context``, so even
    ``search_decisions(full=True)`` handed back a decision with no "why".
  * ``digest.digest_record`` emitted {id, summary, tags, file,
    do_not_revert, weight} — and the digest is what feeds prompt
    injection, so injected decisions were bare assertions.
  * ``write_session_log`` never declared ``task_type``, and
    ``cli_induce`` filters on exactly that field — so skill induction was
    structurally guaranteed to yield zero regardless of usage.

RED against pre-4.0 code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.storage import digest


class TestDigestCarriesWhy:
    def test_digest_record_includes_a_clipped_why(self) -> None:
        rec = digest.digest_record(
            {
                "id": "D000001",
                "decision": "Use SQLite over a graph engine.",
                "context": "Measured 6x smaller install and no daemon to supervise.",
                "tags": ["storage"],
                "file_path": "indexer/sqlite_graph.py",
            }
        )
        assert rec["why"] == "Measured 6x smaller install and no daemon to supervise."

    def test_why_is_clipped_on_a_word_boundary(self) -> None:
        """The digest feeds a token-budgeted injection path — an unbounded
        'why' would silently evict other decisions."""
        rec = digest.digest_record(
            {"id": "D1", "decision": "x", "context": "word " * 200}
        )
        assert len(rec["why"]) <= digest._WHY_CAP + 1
        assert rec["why"].endswith("…")

    def test_missing_context_yields_none_not_empty_string(self) -> None:
        assert digest.digest_record({"id": "D1", "decision": "x"})["why"] is None
        assert (
            digest.digest_record({"id": "D1", "decision": "x", "context": "  "})["why"]
            is None
        )


class TestInjectionRendersWhy:
    def test_injected_line_shows_the_reasoning(self) -> None:
        from mcp_server.engine.policies.relevance_inject import RelevanceInject

        line = RelevanceInject()._format_decision_line(
            {
                "id": "D000001",
                "summary": "Use SQLite over a graph engine",
                "file": "indexer/sqlite_graph.py",
                "do_not_revert": True,
                "why": "Measured 6x smaller install and no daemon.",
            }
        )
        assert "D000001" in line
        assert "↳ Measured 6x smaller install" in line

    def test_line_without_why_is_unchanged(self) -> None:
        """Legacy digests have no `why`; the line must render as before."""
        from mcp_server.engine.policies.relevance_inject import RelevanceInject

        line = RelevanceInject()._format_decision_line(
            {"id": "D1", "summary": "s", "file": None, "do_not_revert": False}
        )
        assert "\n" not in line
        assert "↳" not in line


class TestSessionLogAcceptsTaskType:
    def test_task_type_and_skill_ids_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json
        import subprocess

        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))

        from mcp_server.storage import paths
        from mcp_server.tools.search import write_session_log

        paths.ensure_dirs(proj)
        write_session_log(
            session_id="s1",
            task="add retry",
            phase="p1",
            files_changed=["a.py"],
            decisions=[],
            next_steps=[],
            task_type="bug",
            skill_ids=["SK0001"],
        )

        rows = [
            json.loads(x)
            for x in paths.sessions_path(proj).read_text().splitlines()
            if x.strip()
        ]
        assert rows[-1]["task_type"] == "bug", "induction filters on exactly this"
        assert rows[-1]["skill_ids"] == ["SK0001"]

    def test_omitting_them_is_backward_compatible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        proj = tmp_path / "proj2"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))

        from mcp_server.storage import paths
        from mcp_server.tools.search import write_session_log

        paths.ensure_dirs(proj)
        res = write_session_log(
            session_id="s2",
            task="t",
            phase="p",
            files_changed=[],
            decisions=[],
            next_steps=[],
        )
        assert res["session_id"] == "s2"
