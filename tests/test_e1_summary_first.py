"""E1 (Phase 19) — summary-first tool payloads + on-demand expand.

Pins the contract from D0000WQ / D0000WR:

* ``search_decisions`` / ``list_decisions`` default to COMPACT rows
  (one-line decision summary + key fields), dropping the heavy per-row
  snippet/origin and full text.
* ``full=True`` and ``CODEVIRA_DECISION_DETAIL=full`` restore the verbose
  payload; ``summary_only`` is unchanged.
* A new ``expand(ids=[...])`` batch tool fetches full records by ID.
* The compact default is materially smaller (token estimate) than full.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import decisions_store
from mcp_server.storage.token_estimator import estimate_tokens
from mcp_server.tools.search import expand, list_decisions, search_decisions

# A long, multi-sentence decision so summaries genuinely truncate and the
# full/compact token gap is real.
LONG_TEXT = (
    "Adopt the widget pipeline for batch ingestion because it decouples "
    "producers from consumers. We considered a synchronous path but it "
    "coupled latency to the slowest consumer. The widget queue caps memory "
    "and gives us backpressure for free. This sentence pads the body well "
    "past the one-line summary cap so truncation is observable in tests."
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh temp project so decisions land in an isolated .codevira/."""
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    # Default-clean env: no restore flag unless a test sets it.
    monkeypatch.delenv("CODEVIRA_DECISION_DETAIL", raising=False)
    return root


def _seed(n: int = 12) -> list[str]:
    ids = []
    for i in range(n):
        ids.append(
            decisions_store.record(
                f"{LONG_TEXT} (variant {i})",
                file_path=f"src/widget_{i}.py",
                context=f"context body number {i} " * 8,
                tags=["widget", f"v{i}"],
                do_not_revert=(i % 2 == 0),
            )
        )
    return ids


# ─────────────────────────────────────────────────────────────────────
# one_line_summary helper
# ─────────────────────────────────────────────────────────────────────


class TestOneLineSummary:
    def test_short_text_verbatim(self) -> None:
        assert decisions_store.one_line_summary("short text") == "short text"

    def test_none_and_empty(self) -> None:
        assert decisions_store.one_line_summary(None) == ""
        assert decisions_store.one_line_summary("") == ""

    def test_collapses_newlines_to_one_line(self) -> None:
        out = decisions_store.one_line_summary("a\nb\nc   d\t\te" * 30, 120)
        assert "\n" not in out and "\t" not in out
        assert "  " not in out  # runs of whitespace collapsed

    def test_truncates_with_ellipsis_within_cap(self) -> None:
        out = decisions_store.one_line_summary("x " * 200, 140)
        assert out.endswith("…")
        assert len(out) <= 141

    def test_prefers_sentence_boundary(self) -> None:
        # A sentence that ends in the latter half of the budget is preferred
        # over a mid-word cut. (A very short first sentence would instead fill
        # the budget — we don't waste 140 chars to end at char 18.)
        text = (
            "Choose the queue based approach so producers and consumers stay "
            "decoupled across the whole pipeline. " + "tail " * 100
        )
        out = decisions_store.one_line_summary(text, 140)
        assert out.endswith("pipeline.")
        assert "tail" not in out


# ─────────────────────────────────────────────────────────────────────
# search_decisions
# ─────────────────────────────────────────────────────────────────────


class TestSearchDecisionsCompact:
    def test_default_is_compact(self, project: Path) -> None:
        _seed()
        r = search_decisions("widget", limit=10)
        assert r["count"] > 0
        row = r["results"][0]
        assert set(row.keys()) <= {
            "id",
            "decision",
            "file_path",
            "do_not_revert",
            "tags",
            "created_at",
            "score",
        }
        # Heavy fields dropped by default.
        assert "snippet" not in row and "origin" not in row
        # Decision is a one-liner.
        assert "\n" not in (row["decision"] or "")
        assert len(row["decision"]) <= 141

    def test_full_restores_heavy_fields(self, project: Path) -> None:
        _seed()
        r = search_decisions("widget", limit=10, full=True)
        row = r["results"][0]
        assert "snippet" in row  # full rows carry the FTS5 snippet
        assert "origin" in row
        assert r["hint"] == "Showing full untruncated decisions."

    def test_env_flag_restores_full_default(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        monkeypatch.setenv("CODEVIRA_DECISION_DETAIL", "full")
        r = search_decisions("widget", limit=10)  # no full= passed
        assert "snippet" in r["results"][0]

    def test_summary_only_unchanged(self, project: Path) -> None:
        _seed()
        r = search_decisions("widget", limit=5, summary_only=True)
        assert r["mode"] == "summary_only"
        assert set(r["results"][0].keys()) <= {
            "id",
            "summary",
            "score",
            "do_not_revert",
        }


# ─────────────────────────────────────────────────────────────────────
# list_decisions
# ─────────────────────────────────────────────────────────────────────


class TestListDecisionsCompact:
    def test_default_one_lines_decision(self, project: Path) -> None:
        _seed()
        r = list_decisions(limit=10)
        row = r["decisions"][0]
        assert "\n" not in (row["decision"] or "")
        assert len(row["decision"]) <= 141
        # Key fields retained.
        assert {"id", "file_path", "do_not_revert", "tags"} <= set(row.keys())

    def test_full_is_untruncated(self, project: Path) -> None:
        _seed()
        r = list_decisions(limit=10, full=True)
        # At least one full record holds the complete (long) decision text.
        assert any(len(d.get("decision", "")) > 141 for d in r["decisions"])

    def test_env_flag_restores_full(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        monkeypatch.setenv("CODEVIRA_DECISION_DETAIL", "full")
        r = list_decisions(limit=10)
        assert any(len(d.get("decision", "")) > 141 for d in r["decisions"])


# ─────────────────────────────────────────────────────────────────────
# expand
# ─────────────────────────────────────────────────────────────────────


class TestExpand:
    def test_round_trips_known_ids_in_order(self, project: Path) -> None:
        ids = _seed(5)
        want = [ids[3], ids[0]]
        ex = expand(want)
        assert ex["requested"] == 2
        assert ex["count"] == 2
        assert [d["id"] for d in ex["decisions"]] == want
        # Full record — context survives (not present in compact rows).
        assert ex["decisions"][0].get("context")

    def test_unknown_ids_reported_not_raised(self, project: Path) -> None:
        ids = _seed(2)
        ex = expand([ids[0], "D-DOES-NOT-EXIST"])
        assert ex["count"] == 1
        assert ex["not_found"] == ["D-DOES-NOT-EXIST"]

    def test_empty_input(self, project: Path) -> None:
        ex = expand([])
        assert ex == {
            "requested": 0,
            "count": 0,
            "decisions": [],
            "not_found": [],
        }


# ─────────────────────────────────────────────────────────────────────
# token savings — the phase's headline guarantee
# ─────────────────────────────────────────────────────────────────────


class TestTokenSavings:
    def test_compact_default_materially_smaller(self, project: Path) -> None:
        _seed(20)
        compact = search_decisions("widget", limit=20)
        full = search_decisions("widget", limit=20, full=True)
        compact_tok = estimate_tokens(json.dumps(compact["results"]))
        full_tok = estimate_tokens(json.dumps(full["results"]))
        # "materially smaller" — at least a third lighter on a 20-row page.
        assert compact_tok < full_tok * 0.66, (compact_tok, full_tok)
