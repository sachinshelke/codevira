"""Regression: the enforcement path must never read another project's memory.

Found during the Claude Code verification gate for 4.0 Step 2 (D00012Q).
``SignalContext`` carries a ``project_root``, but ``decisions()`` resolved
the store path ambiently — ``store_paths.decisions_path()`` with no
argument — so it read whichever project the PROCESS resolved from cwd/env.

Verified live before the fix: a ``SignalContext`` built for a fresh
one-decision project returned 10 of agent-mcp's decisions, including its
``do_not_revert`` locks. That is the enforcement-side twin of the
D00011U / D00011V cross-project bleed — a locked decision in one project
could block an edit in a different project, and the block message would
cite a decision the user had never made about that code.

These tests are RED against the pre-fix resolution.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.engine.signals import SignalContext


def _project(root: Path, decisions: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    store = root / ".codevira"
    store.mkdir(exist_ok=True)
    (store / "decisions.jsonl").write_text(
        "\n".join(json.dumps(d) for d in decisions) + "\n"
    )
    return root


@pytest.fixture
def two_projects(tmp_path: Path) -> tuple[Path, Path]:
    a = _project(
        tmp_path / "alpha",
        [
            {
                "id": "A000001",
                "ts": "2026-01-01T00:00:00+00:00",
                "decision": "alpha: keep the retry budget at 3",
                "file_path": "src/a.py",
                "do_not_revert": True,
            }
        ],
    )
    b = _project(
        tmp_path / "beta",
        [
            {
                "id": "B000001",
                "ts": "2026-01-01T00:00:00+00:00",
                "decision": "beta: never cache the invalidation path",
                "file_path": "src/b.py",
                "do_not_revert": True,
            },
            {
                "id": "B000002",
                "ts": "2026-01-02T00:00:00+00:00",
                "decision": "beta: second locked decision",
                "file_path": "src/b2.py",
                "do_not_revert": True,
            },
        ],
    )
    return a, b


class TestNoCrossProjectBleed:
    def test_context_reads_its_own_project_not_the_ambient_one(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alpha, beta = two_projects
        # Ambient resolution points at BETA...
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(beta))
        monkeypatch.chdir(beta)

        # ...but the context was built for ALPHA.
        found = SignalContext(project_root=alpha).decisions(locked_only=True)

        ids = {d["id"] for d in found}
        assert ids == {"A000001"}, f"leaked from another project: {ids}"
        assert not any(i.startswith("B") for i in ids)

    def test_the_reverse_direction_too(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alpha, beta = two_projects
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(alpha))
        monkeypatch.chdir(alpha)

        found = SignalContext(project_root=beta).decisions(locked_only=True)
        assert {d["id"] for d in found} == {"B000001", "B000002"}

    def test_file_filter_is_scoped_to_the_right_project(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blocking path filters by file. Beta owns src/b.py; asking
        alpha's context for it must return nothing, not beta's lock."""
        alpha, beta = two_projects
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(beta))
        monkeypatch.chdir(beta)

        found = SignalContext(project_root=alpha).decisions(
            file="src/b.py", locked_only=True
        )
        assert found == []

    def test_search_is_scoped_too(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alpha, beta = two_projects
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(beta))
        monkeypatch.chdir(beta)

        hits = SignalContext(project_root=alpha).search_decisions("retry budget")
        assert all(not h.get("id", "").startswith("B") for h in hits)


class TestInjectionIsScopedToo:
    """The injection path had the same ambient-resolution bug as
    signals.decisions(), and it is the one users actually notice: a
    UserPromptSubmit in project A was injected with project B's
    decisions. Verified live before the fix (D00012O)."""

    def test_inject_reads_only_its_own_project(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.engine.policies.relevance_inject import RelevanceInject

        alpha, beta = two_projects
        # Give BOTH projects a manifest+digest so either could be loaded.
        for root, did, text in (
            (alpha, "A000001", "alpha: keep the retry budget at 3"),
            (beta, "B000001", "beta: never cache the invalidation path"),
        ):
            (root / ".codevira" / "manifest.yaml").write_text(
                f"active_decisions: 1\ntags:\n  retry: [{did}]\n"
            )
            (root / ".codevira" / "digest.jsonl").write_text(
                json.dumps(
                    {
                        "id": did,
                        "summary": text,
                        "tags": ["retry"],
                        "file": None,
                        "do_not_revert": True,
                        "weight": 1.0,
                        "why": None,
                    }
                )
                + "\n"
            )

        # Ambient points at BETA; ask for ALPHA's indexes.
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(beta))
        monkeypatch.chdir(beta)

        manifest, digest_records = RelevanceInject()._load_indexes(alpha)
        ids = {r["id"] for r in digest_records}
        assert ids == {"A000001"}, f"injected another project's decisions: {ids}"

    def test_config_is_scoped(
        self, two_projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One project must not inherit another's injection budget."""
        from mcp_server.engine.policies.relevance_inject import RelevanceInject

        alpha, beta = two_projects
        (alpha / ".codevira" / "config.yaml").write_text("inject_max_decisions: 7\n")
        (beta / ".codevira" / "config.yaml").write_text("inject_max_decisions: 2\n")

        monkeypatch.delenv("CODEVIRA_INJECT_MAX_DECISIONS", raising=False)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(beta))
        monkeypatch.chdir(beta)

        assert RelevanceInject()._config(alpha)["max_decisions"] == 7
        assert RelevanceInject()._config(beta)["max_decisions"] == 2
