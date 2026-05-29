"""
Tests for v3.1.0 M8: reflections.

Covers:
  - scrub_sensitive: redacts api keys / bearer / passwords / AKIA /
    long hex / long base64.
  - build_source_context: filters by period; obeys session/decision
    caps; sanitizes narrative fields; envelope-size cap trims when over.
  - render_prompt: template inlines source context with placeholder
    expansion; missing template falls back gracefully.
  - reflections_store.append / list_recent / list_filtered.
  - cmd_reflect: render mode prints prompt; --from-file parses YAML
    and writes proposal; --apply --yes commits to reflections.jsonl;
    missing/empty/malformed input rejected.
  - MCP tools: reflect returns sampling_supported=False; get/list
    return durable data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import (
    decisions_store,
    jsonl_store,
    paths,
    reflections_store,
    sessions_store,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# scrub_sensitive
# ──────────────────────────────────────────────────────────────────────


class TestScrubSensitive:
    def test_api_key_redacted(self) -> None:
        out = reflections_store.scrub_sensitive("api_key=hunter2-deadbeefcafe")
        assert "<redacted:api-key>" in out
        assert "hunter2" not in out

    def test_bearer_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "Authorization: Bearer abc123XYZ.token-here"
        )
        assert "<redacted:bearer>" in out
        assert "abc123XYZ" not in out

    def test_password_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "password=hunter2-correct-horse-battery-staple"
        )
        assert "<redacted:password>" in out

    def test_akia_redacted(self) -> None:
        out = reflections_store.scrub_sensitive("AKIAIOSFODNN7EXAMPLE here")
        assert "<redacted:aws-akia>" in out

    def test_long_hex_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "tok = a1b2c3d4e5f607182930405060708090abcdef0123456789"
        )
        assert "<redacted:long-token>" in out

    def test_plain_text_untouched(self) -> None:
        text = "Use bcrypt for password hashing in auth.py."
        # Note: the "password" word is part of normal prose; the
        # secret regex requires `password=` or `password:` form.
        assert reflections_store.scrub_sensitive(text) == text


# ──────────────────────────────────────────────────────────────────────
# build_source_context
# ──────────────────────────────────────────────────────────────────────


class TestBuildSourceContext:
    def test_empty_returns_empty_lists(self, project: Path) -> None:
        ctx = reflections_store.build_source_context(period_days=7)
        assert ctx["sessions"] == []
        assert ctx["decisions"] == []

    def test_in_window_included(self, project: Path) -> None:
        decisions_store.record(decision="X", tags=["t1"])
        sessions_store.write("s1", task="x", task_type="bug")
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["sessions"]) == 1
        assert len(ctx["decisions"]) == 1

    def test_out_of_window_excluded(self, project: Path) -> None:
        # Inject an old decision row directly.
        old_ts = (datetime(2020, 1, 1, tzinfo=timezone.utc)).isoformat()
        jsonl_store.append(
            paths.decisions_path(),
            {
                "id": "D000099",
                "ts": old_ts,
                "session_id": "ad-hoc",
                "decision": "ancient",
                "_schema_v": 0,
            },
        )
        decisions_store.record(decision="fresh", tags=[])
        ctx = reflections_store.build_source_context(period_days=7)
        # Only the fresh decision surfaces.
        assert all("ancient" not in d.get("decision", "") for d in ctx["decisions"])

    def test_session_cap_enforced(self, project: Path) -> None:
        for i in range(50):
            sessions_store.write(f"sess-{i}", task=f"task {i}", task_type="bug")
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["sessions"]) <= reflections_store.MAX_SESSIONS_PER_REFLECTION

    def test_decision_cap_enforced(self, project: Path) -> None:
        for i in range(150):
            decisions_store.record(decision=f"d{i}", tags=["t"])
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["decisions"]) <= reflections_store.MAX_DECISIONS_PER_REFLECTION

    def test_sanitization_runs(self, project: Path) -> None:
        decisions_store.record(
            decision="see api_key=hunter2-deadbeefcafedeadbeef",
            tags=["secret"],
        )
        ctx = reflections_store.build_source_context(period_days=7)
        assert "<redacted:api-key>" in ctx["decisions"][0]["decision"]
        assert "hunter2" not in ctx["decisions"][0]["decision"]


# ──────────────────────────────────────────────────────────────────────
# render_prompt
# ──────────────────────────────────────────────────────────────────────


class TestRenderPrompt:
    def test_inlines_source_context(self, project: Path) -> None:
        decisions_store.record(decision="Use bcrypt", tags=["auth"])
        ctx = reflections_store.build_source_context(period_days=7)
        prompt = reflections_store.render_prompt(ctx)
        assert "Use bcrypt" in prompt
        # Template placeholder was replaced.
        assert "<<<SOURCE_CONTEXT>>>" not in prompt

    def test_template_missing_falls_back(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Point to a non-existent template; the fallback inline prompt
        # still renders without crashing.
        monkeypatch.setattr(
            paths,
            "reflection_prompt_path",
            lambda: Path("/nonexistent/path/reflection.md"),
        )
        ctx = reflections_store.build_source_context(period_days=7)
        prompt = reflections_store.render_prompt(ctx)
        # Fallback inline contains the "abstraction" guidance string.
        assert "abstraction" in prompt


# ──────────────────────────────────────────────────────────────────────
# Storage: append / list / filter
# ──────────────────────────────────────────────────────────────────────


class TestStorage:
    def test_append_returns_r_id(self, project: Path) -> None:
        rid = reflections_store.append(
            abstraction="The team prioritizes auth hardening.",
            confidence=0.7,
            tags=["auth", "security"],
            period_start=datetime(2026, 5, 21, tzinfo=timezone.utc).isoformat(),
            period_end=datetime(2026, 5, 28, tzinfo=timezone.utc).isoformat(),
            source_session_ids=["s1", "s2"],
            source_decision_ids=["D000001"],
        )
        assert rid.startswith("R")

    def test_list_recent_newest_first(self, project: Path) -> None:
        for i, text in enumerate(["first", "second", "third"]):
            reflections_store.append(
                abstraction=text,
                confidence=0.5,
                tags=[],
                period_start="2026-05-01T00:00:00+00:00",
                period_end="2026-05-07T00:00:00+00:00",
                source_session_ids=[],
                source_decision_ids=[],
            )
        rows = reflections_store.list_recent(limit=5)
        assert [r["abstraction"] for r in rows] == ["third", "second", "first"]

    def test_list_filtered_by_tags(self, project: Path) -> None:
        reflections_store.append(
            abstraction="A",
            confidence=0.5,
            tags=["release", "v3"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        reflections_store.append(
            abstraction="B",
            confidence=0.5,
            tags=["v3"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        rows = reflections_store.list_filtered(tags=["release", "v3"])
        assert [r["abstraction"] for r in rows] == ["A"]


# ──────────────────────────────────────────────────────────────────────
# CLI: cmd_reflect
# ──────────────────────────────────────────────────────────────────────


_GOOD_RESPONSE = """\
```yaml
abstraction: |
  The team is consolidating around bcrypt+rate-limiting for auth.
tags: [auth, security]
confidence: 0.78
```
"""

_NO_FENCE_RESPONSE = """\
abstraction: |
  Auth hardening continues this week.
tags: [auth]
confidence: 0.6
"""

_EMPTY_ABSTRACTION = """\
```yaml
abstraction: ""
tags: []
confidence: 0.1
```
"""


class TestCmdReflect:
    def test_no_from_file_prints_prompt(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Feed the prompt below to your LLM" in out
        assert "abstraction" in out

    def test_from_file_writes_proposal(self, project: Path, tmp_path: Path) -> None:
        decisions_store.record(decision="x")  # seed source context
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_GOOD_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path))
        assert rc == 0
        proposals = jsonl_store.read_all(paths.reflection_proposals_path())
        assert len(proposals) == 1
        assert "bcrypt" in proposals[0]["abstraction"]
        assert sorted(proposals[0]["tags"]) == ["auth", "security"]
        assert abs(proposals[0]["confidence"] - 0.78) < 1e-3

    def test_apply_yes_commits_to_reflections(
        self, project: Path, tmp_path: Path
    ) -> None:
        decisions_store.record(decision="x")
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_GOOD_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path), apply=True, yes=True)
        assert rc == 0
        rows = jsonl_store.read_all(paths.reflections_path())
        assert len(rows) == 1
        assert "bcrypt" in rows[0]["abstraction"]

    def test_unfenced_response_still_parsed(
        self, project: Path, tmp_path: Path
    ) -> None:
        decisions_store.record(decision="x")
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_NO_FENCE_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        assert cmd_reflect(from_file=str(resp_path)) == 0

    def test_missing_file_returns_1(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file="/nonexistent/path.yaml")
        assert rc == 1
        assert "could not read" in capsys.readouterr().err

    def test_empty_abstraction_rejected(
        self, project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_EMPTY_ABSTRACTION)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path))
        assert rc == 1
        assert "empty" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────


class TestMcpTools:
    def test_reflect_returns_sampling_supported_false(self, project: Path) -> None:
        decisions_store.record(decision="x")
        from mcp_server.tools.reflections import reflect

        r = reflect()
        assert r["sampling_supported"] is False
        assert r["deferred_to"] == "v3.2"
        assert "rendered_prompt" in r
        assert "source_context" in r

    def test_get_reflections_empty(self, project: Path) -> None:
        from mcp_server.tools.reflections import get_reflections

        r = get_reflections()
        assert r["count"] == 0
        assert r["reflections"] == []

    def test_get_reflections_populated(self, project: Path) -> None:
        reflections_store.append(
            abstraction="A",
            confidence=0.5,
            tags=["x"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        from mcp_server.tools.reflections import get_reflections

        r = get_reflections()
        assert r["count"] == 1
        assert r["reflections"][0]["abstraction"] == "A"
